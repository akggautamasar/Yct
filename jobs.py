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
import gc
import html

GATE_TIMEOUT_SECONDS = 30 * 60   # auto-advance after 30 min if user vanishes

import config
import scraper
import pdfbuilder
import telegram as tg
import checkpoint


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
        "cancelled": False,     # user requested cancel/delete
        "_started": False,      # worker has picked this job up
        "_cur_folder": None,    # in-progress page-image folder (for cleanup)
        "_cur_pdf": None,       # in-progress built PDF (for cleanup)
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


def _cleanup_job_files(job: dict):
    """Best-effort removal of every file this job may have left on disk."""
    folder = job.get("_cur_folder")
    if folder:
        shutil.rmtree(folder, ignore_errors=True)
    pdf = job.get("_cur_pdf")
    if pdf and os.path.exists(pdf):
        try:
            os.remove(pdf)
        except OSError:
            pass
    _delete_current_part(job)


def cancel_job(jid: str) -> bool:
    """Cancel a queued/running job (or delete a finished/errored one),
    freeing any storage it used on the way out. Safe to call at any
    point in the job's lifecycle — never leaves orphaned files."""
    job = JOBS.get(jid)
    if not job:
        return False

    job["cancelled"] = True

    # Unblock a worker that's paused waiting for the user to confirm a part.
    gate: asyncio.Event | None = job.get("_gate")
    if gate and not gate.is_set():
        gate.set()

    if not job.get("_started") or job.get("status") in ("done", "error", "cancelled"):
        # Either still sitting in the queue (nothing written to disk yet)
        # or already finished/errored — safe to clean up and drop now.
        _cleanup_job_files(job)
        JOBS.pop(jid, None)
        return True

    # Actively running — flip the flag, the worker checks it at every safe
    # checkpoint, cleans up its own in-progress files, and removes the
    # job entry itself once it unwinds.
    job["status"] = "cancelling"
    return True


async def _process(jid: str):
    job = JOBS[jid]
    job["_started"] = True
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
            if job.get("cancelled"):
                _cleanup_job_files(job)
                JOBS.pop(jid, None)
                return

            b_start, b_end = batch[0], batch[-1]
            folder = os.path.join(config.WORK_DIR, f"book_{book_id}_p{b_start}_{b_end}")
            os.makedirs(folder, exist_ok=True)
            job["_cur_folder"] = folder

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

            if job.get("cancelled"):
                _cleanup_job_files(job)
                JOBS.pop(jid, None)
                return

            job["status"] = f"building part {bi}/{len(batches)}" if multi else "building_pdf"
            if not multi and start == 1 and end >= total_pages:
                display_name = f"{safe}.pdf"
            elif multi:
                display_name = f"{safe} (part {bi} of {len(batches)}, p{b_start}-{b_end}).pdf"
            else:
                display_name = f"{safe} (p{b_start}-{b_end}).pdf"
            pdf_path = os.path.join(config.WORK_DIR, display_name)
            job["_cur_pdf"] = pdf_path

            mb, ocr_used = pdfbuilder.build(folder, pdf_path, job["do_ocr"])
            if ocr_used:
                quality = "searchable OCR"

            shutil.rmtree(folder, ignore_errors=True)
            job["_cur_folder"] = None

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

                    if job.get("cancelled"):
                        _cleanup_job_files(job)
                        JOBS.pop(jid, None)
                        return

                    # Delete the part now that user confirmed (or timed out)
                    _delete_current_part(job)
                    job["_gate"] = None
                    job["parts_done"] = bi

                # Delete original build file
                try: os.remove(pdf_path)
                except: pass
                job["_cur_pdf"] = None
                # Release the page-image/PDF memory for this batch right now —
                # don't wait for the next batch or job to trigger a GC cycle.
                gc.collect()

        if job.get("cancelled"):
            _cleanup_job_files(job)
            JOBS.pop(jid, None)
            return

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



# C# Category downloads use Telegram as the destination instead of building a giant ZIP.
# A single shared lock prevents category exports from competing with normal PDF jobs
# for Render's limited CPU, RAM, and ephemeral disk.
BATCH_JOBS: dict[str, dict] = {}
_resource_lock = asyncio.Lock()


def start_category_archive(category_name: str, books: list[dict], resume_book: int = 1, resume_page: int = 1) -> str:
    batch_id = uuid.uuid4().hex[:10]
    job = {
        "id": batch_id, "title": category_name or "All categories",
        "status": "queued", "done_books": 0, "total_books": len(books),
        "done_pages": 0, "total_pages": 0, "uploaded_parts": 0,
        "current_book": "", "current_pages": "", "skipped_books": 0,
        "skipped_details": [], "failed_parts": 0, "error": None,
        "created_at": int(time.time()), "cancelled": False,\n        "resume_book": resume_book, "resume_page": resume_page,
    }
    BATCH_JOBS[batch_id] = job
    asyncio.create_task(_process_category_archive(batch_id, [dict(b) for b in books]))
    return batch_id


def get_category_archive(batch_id: str) -> dict | None:
    job = BATCH_JOBS.get(batch_id)
    if not job:
        return None
    return {k: v for k, v in job.items() if k != "cancelled"}


def cancel_category_archive(batch_id: str) -> bool:
    job = BATCH_JOBS.get(batch_id)
    if not job or job.get("status") in ("done", "error", "cancelled"):
        return False
    job["cancelled"] = True
    job["status"] = "cancelling"
    return True


def _category_skip(job: dict, detail: str):
    job["skipped_books"] += 1
    if len(job["skipped_details"]) < 30:
        job["skipped_details"].append(detail[:180])


async def _process_category_archive(batch_id: str, books: list[dict]):
    job = BATCH_JOBS[batch_id]
    async with _resource_lock:
        if job.get("cancelled"):
            job["status"] = "cancelled"
            return
        await _run_category_telegram_export(batch_id, books)


async def _run_category_telegram_export(batch_id: str, books: list[dict]):
    job = BATCH_JOBS[batch_id]
    chat = config.TG_CHAT_ID
    try:
        job["status"] = "logging_in"
        if not config.TG_TOKEN or not chat:
            raise RuntimeError("Telegram storage is not configured. Set TG_TOKEN and TG_CHAT_ID on Render.")
        if not await scraper.login():
            raise RuntimeError("YCT login failed; check YCT_EMAIL / YCT_PASSWORD")

        await tg.send_message(
            f"<b>YCT category export started</b>\n{len(books)} books\n"
            "Books will be uploaded one at a time in page-sized PDF parts. "
            "Files already uploaded stay in Telegram if the Render service restarts.",
            chat,
        )

        for index, book in enumerate(books, 1):\n            if index < job["resume_book"]:\n                job["done_books"] = index\n                continue
            if job.get("cancelled"):
                job["status"] = "cancelled"
                await tg.send_message(
                    f"<b>Category export cancelled</b>\nUploaded {job['uploaded_parts']} PDF part(s); "
                    f"{job['done_books']}/{job['total_books']} books processed.", chat)
                return

            try:
                book_id = int(book.get("book_id") or 0)
            except (TypeError, ValueError):
                book_id = 0
            if book_id < 1:
                _category_skip(job, str(book.get("name") or "Invalid book ID"))
                job["done_books"] = index
                continue

            title = _safe_filename(str(book.get("name") or f"Book {book_id}"), f"Book {book_id}")
            job["current_book"] = title
            job["status"] = f"checking book {index}/{len(books)}"
            total_pages = book.get("total_pages")
            try:
                total_pages = int(total_pages) if total_pages else 0
            except (TypeError, ValueError):
                total_pages = 0
            if total_pages < 1:
                total_pages = await scraper.get_total_pages(book_id)
            if not total_pages or total_pages < 1:
                _category_skip(job, f"{title}: page count unavailable")
                job["done_books"] = index
                continue
            job["total_pages"] += int(total_pages)

            if not await scraper.has_access(book_id):
                _category_skip(job, f"{title}: no access")
                job["done_books"] = index
                continue

            chunk = max(1, min(config.CHUNK_PAGES, 40))
            book_had_upload_failure = False
            for start in range(1, int(total_pages) + 1, chunk):\n                if index == job["resume_book"] and start + chunk - 1 < job["resume_page"]:\n                    continue
                if job.get("cancelled"):
                    job["status"] = "cancelled"
                    await tg.send_message(
                        f"<b>Category export cancelled</b>\nUploaded {job['uploaded_parts']} PDF part(s).", chat)
                    return
                end = min(int(total_pages), start + chunk - 1)
                job["current_pages"] = f"{start}-{end} of {total_pages}"
                job["status"] = f"downloading {index}/{len(books)} · pages {start}-{end}"
                folder = os.path.join(config.WORK_DIR, f"cat_{batch_id}_{book_id}_{start}_{end}")
                pdf_path = os.path.join(config.WORK_DIR, f"cat_{batch_id}_{book_id}_{start}_{end}.pdf")
                pdf_parts = []
                os.makedirs(folder, exist_ok=True)
                try:
                    page_numbers = list(range(start, end + 1))
                    ok, skipped, failed = await scraper.download_pages(book_id, page_numbers, folder)
                    if failed:
                        retry_ok, retry_skipped, failed = await scraper.download_pages(book_id, failed, folder)
                        ok += retry_ok
                        skipped += retry_skipped
                    job["done_pages"] += ok + skipped
                    if not ok and failed:
                        job["failed_parts"] += 1
                        if len(job["skipped_details"]) < 30:
                            job["skipped_details"].append(f"{title}: pages {start}-{end} could not be downloaded")
                        continue

                    job["status"] = f"building {index}/{len(books)} · pages {start}-{end}"
                    size_mb, _ = pdfbuilder.build(folder, pdf_path, False)
                    if size_mb <= 0 or not os.path.isfile(pdf_path):
                        job["failed_parts"] += 1
                        if len(job["skipped_details"]) < 30:
                            job["skipped_details"].append(f"{title}: PDF build failed for pages {start}-{end}")
                        continue

                    pdf_parts = pdfbuilder.split_for_telegram(pdf_path)
                    for part_number, part_path in enumerate(pdf_parts, 1):
                        if job.get("cancelled"):
                            job["status"] = "cancelled"
                            return
                        job["status"] = f"uploading {index}/{len(books)} · PDF part {part_number}/{len(pdf_parts)}"
                        caption = f"<b>{html.escape(title)}</b>\nBook {index}/{len(books)} · pages {start}-{end}"
                        if len(pdf_parts) > 1:
                            caption += f" · file {part_number}/{len(pdf_parts)}"
                        key = checkpoint.part_key(chat, book_id, start, end, part_number)
                        if await checkpoint.has_part(key):
                            job["uploaded_parts"] += 1
                            continue
                        sent = await tg.send_document(part_path, caption, chat)
                        if not sent:
                            job["failed_parts"] += 1
                            book_had_upload_failure = True
                            raise RuntimeError(
                                f"Telegram upload failed for {title}, pages {start}-{end}. "
                                "Check bot permissions, chat ID, and Telegram file-size limits."
                            )
                        if checkpoint.enabled():
                            await checkpoint.mark_part(key, {
                                "book_id": book_id, "title": title,
                                "start_page": start, "end_page": end,
                                "part_number": part_number,
                                "uploaded_at": int(time.time()),
                                "chat_id": str(chat),
                            })
                        job["uploaded_parts"] += 1
                        if part_path != pdf_path:
                            try:
                                os.remove(part_path)
                            except OSError:
                                pass
                finally:
                    # Also remove any remaining split files if an upload fails midway.
                    for leftover in pdf_parts:
                        if leftover != pdf_path:
                            try:
                                if os.path.exists(leftover):
                                    os.remove(leftover)
                            except OSError:
                                pass
                    shutil.rmtree(folder, ignore_errors=True)
                    try:
                        if os.path.exists(pdf_path):
                            os.remove(pdf_path)
                    except OSError:
                        pass
                    gc.collect()

            if not book_had_upload_failure:
                job["done_books"] = index

        if job.get("cancelled"):
            job["status"] = "cancelled"
            return
        job["status"] = "done"
        job["current_book"] = ""
        job["current_pages"] = ""
        await tg.send_message(
            f"<b>✓ Category export complete</b>\n{job['title']}\n"
            f"Books processed: {job['done_books']}/{job['total_books']}\n"
            f"PDF files uploaded: {job['uploaded_parts']}\n"
            f"Pages processed: {job['done_pages']}/{job['total_pages']}\n"
            f"Skipped books: {job['skipped_books']}\n"
            f"Failed page batches/uploads: {job['failed_parts']}",
            chat,
        )
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)[:400]
        try:
            await tg.send_message(
                f"<b>✗ Category export stopped</b>\n{job['title']}\n"
                f"Uploaded files remain in Telegram.\n{str(exc)[:250]}",
                chat,
            )
        except Exception:
            pass


async def _worker():
    while True:
        jid = await _q().get()
        try:
            if jid in JOBS:
                async with _resource_lock:
                    await _process(jid)
        finally:
            _q().task_done()


def start_worker():
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    asyncio.create_task(_worker())
