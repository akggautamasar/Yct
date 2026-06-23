"""Job queue + single worker.

One book processed at a time to stay within Render's 512MB RAM.
Jobs live in memory; lost on restart (acceptable for a download tool).

Browser-download strategy (Render free tier safe):
  - After a PDF is built it is copied to DOWNLOAD_DIR so the user can
    download it directly from the browser.
  - The file is deleted as soon as it is served (one-time link).
  - A background cleanup task also deletes any file older than
    DOWNLOAD_TTL_SECONDS (5 min) that was never fetched, ensuring we
    never accumulate stale PDFs on the 512 MB disk.
"""
import os
import re
import uuid
import shutil
import asyncio
import time

DOWNLOAD_TTL_SECONDS = 5 * 60   # files older than this get auto-purged

import config
import scraper
import pdfbuilder
import telegram as tg


def _safe_filename(name: str, fallback: str) -> str:
    """Make a filesystem- and Telegram-safe filename from a book title."""
    name = (name or "").strip()
    if not name:
        return fallback
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name[:80].strip()
    return name or fallback

# job_id -> dict(status, title, book_id, pages, done, total, ok, failed, ts, error, pdf_links)
JOBS: dict[str, dict] = {}
_queue: asyncio.Queue | None = None
_worker_started = False

# Directory to keep finished PDFs for browser download
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
        "notify_chat": notify_chat, "pdf_parts": 0, "detail_id": detail_id,
        "browser_download": browser_download,
        "download_files": [],   # list of {name, path} for browser download
    }
    _q().put_nowait(jid)
    return jid


def get_job(jid: str) -> dict | None:
    return JOBS.get(jid)


def all_jobs() -> list[dict]:
    return sorted(JOBS.values(), key=lambda j: j["ts"], reverse=True)


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
        download_files = []

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

            if mb > 0 and os.path.exists(pdf_path):
                # Always keep a copy for browser download
                dl_path = os.path.join(DOWNLOAD_DIR, f"{jid}_{bi}_{display_name}")
                shutil.copy2(pdf_path, dl_path)
                download_files.append({"name": display_name, "path": dl_path})
                job["download_files"] = download_files

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

                try: os.remove(pdf_path)
                except: pass

            shutil.rmtree(folder, ignore_errors=True)

        job["failed"] = total_failed
        job["status"] = "done"
        if chat:
            await tg.send_message(
                f"<b>✓ Done</b>\n{title}\n{total_done} pages | {quality}"
                + (f" | {len(batches)} parts" if multi else ""), chat)

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        if chat:
            await tg.send_message(f"<b>✗ Failed</b>\n{title}\n{str(e)[:200]}", chat)


async def _worker():
    while True:
        jid = await _q().get()
        try:
            await _process(jid)
        finally:
            _q().task_done()


def delete_download_file(jid: str, idx: int):
    """Delete a download file after it has been served (called by BackgroundTask,
    so it runs only AFTER the full response is streamed to the browser).
    Updates the entry in-place so the UI shows the consumed state."""
    job = JOBS.get(jid)
    if not job:
        return
    files = job.get("download_files", [])
    if idx < 0 or idx >= len(files):
        return
    entry = files[idx]
    path = entry.get("path", "")
    # Remove the file from disk
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
    # Update in-place (don't replace the dict — other code may hold a ref)
    entry["path"] = ""
    entry["consumed"] = True


async def _cleanup_loop():
    """Every 60 s, purge any download file older than DOWNLOAD_TTL_SECONDS.
    This catches files that were never fetched (tab closed, etc.)."""
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for job in list(JOBS.values()):
            for entry in job.get("download_files", []):
                path = entry.get("path", "")
                if not path or entry.get("consumed"):
                    continue
                try:
                    age = now - os.path.getmtime(path)
                    if age > DOWNLOAD_TTL_SECONDS:
                        os.remove(path)
                        entry["path"] = ""
                        entry["consumed"] = True
                except OSError:
                    pass


def start_worker():
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    asyncio.create_task(_worker())
    asyncio.create_task(_cleanup_loop())
