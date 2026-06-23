"""Job queue + single worker.

One book processed at a time to stay within Render's 512 MB RAM.

Browser-download strategy — sequential gated parts:
  - Parts are built one at a time.
  - After each part is built, the worker PAUSES and waits for the user
    to click "Done — next part" in the UI (confirms they saved it).
  - Only then is the part deleted and the next part starts building.
  - At most 1 PDF exists on disk at any moment → zero accumulation.
  - A safety timeout (GATE_TIMEOUT_SECONDS) auto-advances if the user
    disappears, so the job never hangs forever.
"""
import os
import re
import uuid
import shutil
import asyncio
import time

GATE_TIMEOUT_SECONDS = 30 * 60   # auto-advance after 30 min if user vanishes

import config
import scraper
import pdfbuilder
import telegram as tg


def _safe_filename(name: str, fallback: str) -> str:
    name = (name or "").strip()
    if not name:
        return fallback
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name[:80].strip()
    return name or fallback


JOBS: dict[str, dict] = {}
_queue: asyncio.Queue | None = None
_worker_started = False

DOWNLOAD_DIR = os.path.join(config.WORK_DIR, "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def _q() -> asyncio.Queue:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    return _queue


def new_job(book_id: int, title: str, cover: str, start: int, end: int,
            do_ocr: bool, total_pages: int | None,
            notify_chat: str | None = None, detail_id: int | None = None,
            browser_download: bool = False) -> str:
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {
        "id": jid, "status": "queued", "book_id": book_id, "title": title,
        "cover": cover, "start": start, "end": end, "do_ocr": do_ocr,
        "total_pages": total_pages, "done": 0, "total": 0, "ok": 0,
        "failed": 0, "error": None, "ts": int(time.time()),
        "notify_chat": notify_chat, "detail_id": detail_id,
        "browser_download": browser_download,
        # current part ready for download
        "current_part": None,   # {idx, name, path, total_parts}
        "parts_done": 0,        # how many parts fully confirmed by user
        "total_parts": 0,
        "_gate": None,          # asyncio.Event — set when user clicks Done
    }
    _q().put_nowait(jid)
    return jid


def get_job(jid: str) -> dict | None:
    return JOBS.get(jid)


def all_jobs() -> list[dict]:
    return sorted(JOBS.values(), key=lambda j: j["ts"], reverse=True)


def confirm_part(jid: str) -> bool:
    """Called when user clicks 'Done — get next part'. Releases the gate."""
    job = JOBS.get(jid)
    if not job:
        return False
    gate: asyncio.Event | None = job.get("_gate")
    if gate:
        gate.set()
        return True
    return False


def _delete_current_part(job: dict):
    """Delete the file for the current part from disk."""
    cp = job.get("current_part")
    if not cp:
        return
    path = cp.get("path", "")
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
    cp["path"] = ""
    cp["consumed"] = True


async def _process(jid: str):
    job = JOBS[jid]
    chat = job.get("notify_chat")
    book_id = job["book_id"]
    title = job["title"]
    browser_download = job.get("browser_download", False)

    try:
        job["status"] = "logging_in"
        if not await scraper.login():
            raise RuntimeError("login failed")

        if (not title or title.startswith("Book ")) and job.get("detail_id"):
            try:
                real_name, _ = await scraper.get_book_details(job["detail_id"])
                if real_name and not real_name.startswith("Book "):
                    title = real_name
                    job["title"] = real_name
            except Exception:
                pass

        start = max(job["start"], 1)
        explicit_end = job["end"]
        total_pages = job["total_pages"]

        if explicit_end:
            end = explicit_end
            if not total_pages or total_pages < end:
                total_pages = end
            job["total_pages"] = total_pages
        else:
            if not total_pages:
                job["status"] = "detecting_pages"
                total_pages = await scraper.get_total_pages(book_id)
                if not total_pages:
                    raise RuntimeError("could not detect total pages")
                job["total_pages"] = total_pages
            end = total_pages

        pages = list(range(start, end + 1))
        job["total"] = len(pages)

        if not await scraper.has_access(book_id):
            raise RuntimeError("no access to this book (check account)")

        chunk = max(1, config.CHUNK_PAGES)
        batches = [pages[i:i + chunk] for i in range(0, len(pages), chunk)]
        multi = len(batches) > 1
        job["total_parts"] = len(batches)

        if chat:
            extra = (f"\nProcessing in {len(batches)} parts of up to {chunk} pages."
                     if multi else "")
            await tg.send_message(
                f"<b>Download started</b>\n{title}\nPages {start}-{end} of {total_pages}{extra}",
                chat)

        safe = _safe_filename(title, f"yct_{book_id}")
        quality = "max-quality lossless"
        total_done = 0
        total_failed = 0

        for bi, batch in enumerate(batches, 1):
            b_start, b_end = batch[0], batch[-1]
            folder = os.path.join(config.WORK_DIR, f"book_{book_id}_p{b_start}_{b_end}")
            os.makedirs(folder, exist_ok=True)

            job["status"] = f"downloading part {bi}/{len(batches)}" if multi else "downloading"

            def on_progress(done, total, ok, skip, failed, _base=total_done):
                job["done"] = _base + done
                job["ok"] = _base + ok
                job["failed"] = total_failed + failed

            ok, skip, failed = await scraper.download_pages(book_id, batch, folder, on_progress)
            if failed:
                _, _, failed = await scraper.download_pages(book_id, failed, folder)
            total_failed += len(failed)
            total_done += len(batch) - len(failed)
            job["done"] = total_done
            job["failed"] = total_failed

            job["status"] = f"building part {bi}/{len(batches)}" if multi else "building_pdf"
            if not multi and start == 1 and end >= total_pages:
                display_name = f"{safe}.pdf"
            elif multi:
                display_name = f"{safe} (part {bi} of {len(batches)}, p{b_start}-{b_end}).pdf"
            else:
                display_name = f"{safe} (p{b_start}-{b_end}).pdf"
            pdf_path = os.path.join(config.WORK_DIR, display_name)

            mb, ocr_used = pdfbuilder.build(folder, pdf_path, job["do_ocr"])
            if ocr_used:
                quality = "searchable OCR"

            shutil.rmtree(folder, ignore_errors=True)

            if mb > 0 and os.path.exists(pdf_path):
                # ── Telegram upload ──
                if chat:
                    job["status"] = f"uploading part {bi}/{len(batches)}" if multi else "uploading"
                    tg_parts = pdfbuilder.split_for_telegram(pdf_path)
                    for pj, part in enumerate(tg_parts, 1):
                        cap = f"<b>{title}</b> | pages {b_start}-{b_end} | {quality}"
                        if multi:
                            cap += f" | part {bi}/{len(batches)}"
                        if len(tg_parts) > 1:
                            cap += f" [{pj}/{len(tg_parts)}]"
                        await tg.send_document(part, cap, chat)
                        if part != pdf_path:
                            try: os.remove(part)
                            except: pass

                # ── Browser download gate ──
                if browser_download:
                    dl_path = os.path.join(DOWNLOAD_DIR, f"{jid}_{bi}_{display_name}")
                    shutil.copy2(pdf_path, dl_path)

                    # Expose to UI
                    gate = asyncio.Event()
                    job["_gate"] = gate
                    job["current_part"] = {
                        "idx": bi - 1,   # 0-based for API URL
                        "part_num": bi,
                        "total_parts": len(batches),
                        "name": display_name,
                        "path": dl_path,
                        "consumed": False,
                        "pages": f"{b_start}–{b_end}",
                    }
                    job["status"] = f"waiting_user part {bi}/{len(batches)}"

                    # Wait for user confirmation OR timeout
                    try:
                        await asyncio.wait_for(gate.wait(), timeout=GATE_TIMEOUT_SECONDS)
                    except asyncio.TimeoutError:
                        pass   # auto-advance

                    # Delete the part now that user confirmed (or timed out)
                    _delete_current_part(job)
                    job["_gate"] = None
                    job["parts_done"] = bi

                # Delete original build file
                try: os.remove(pdf_path)
                except: pass

        job["failed"] = total_failed
        job["status"] = "done"
        job["current_part"] = None
        if chat:
            await tg.send_message(
                f"<b>✓ Done</b>\n{title}\n{total_done} pages | {quality}"
                + (f" | {len(batches)} parts" if multi else ""), chat)

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["current_part"] = None
        if chat:
            await tg.send_message(f"<b>✗ Failed</b>\n{title}\n{str(e)[:200]}", chat)


async def _worker():
    while True:
        jid = await _q().get()
        try:
            await _process(jid)
        finally:
            _q().task_done()


def start_worker():
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    asyncio.create_task(_worker())
