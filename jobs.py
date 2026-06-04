"""Job queue + single worker.

One book processed at a time to stay within Render's 512MB RAM.
Jobs live in memory; lost on restart (acceptable for a download tool).
Finished PDFs are pushed to the Telegram storage channel, then /tmp is cleaned.
"""
import os
import uuid
import shutil
import asyncio
import time

import config
import scraper
import pdfbuilder
import telegram as tg

# job_id -> dict(status, title, book_id, pages, done, total, ok, failed, ts, error, pdf_links)
JOBS: dict[str, dict] = {}
_queue: asyncio.Queue | None = None
_worker_started = False


def _q() -> asyncio.Queue:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    return _queue


def new_job(book_id: int, title: str, cover: str, start: int, end: int,
            do_ocr: bool, total_pages: int | None,
            notify_chat: str | None = None) -> str:
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {
        "id": jid, "status": "queued", "book_id": book_id, "title": title,
        "cover": cover, "start": start, "end": end, "do_ocr": do_ocr,
        "total_pages": total_pages, "done": 0, "total": 0, "ok": 0,
        "failed": 0, "error": None, "ts": int(time.time()),
        "notify_chat": notify_chat, "pdf_parts": 0,
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

    try:
        job["status"] = "logging_in"
        if not await scraper.login():
            raise RuntimeError("login failed")

        # resolve total pages if unknown
        total_pages = job["total_pages"]
        if not total_pages:
            job["status"] = "detecting_pages"
            total_pages = await scraper.get_total_pages(book_id)
            if not total_pages:
                raise RuntimeError("could not detect total pages")
            job["total_pages"] = total_pages

        start = max(job["start"], 1)
        end = min(job["end"] or total_pages, total_pages)
        pages = list(range(start, end + 1))
        job["total"] = len(pages)

        if not await scraper.has_access(book_id):
            raise RuntimeError("no access to this book (check account)")

        if chat:
            await tg.send_message(
                f"<b>Download started</b>\n{title}\nPages {start}-{end} of {total_pages}",
                chat)

        folder = os.path.join(config.WORK_DIR, f"book_{book_id}_p{start}_{end}")
        os.makedirs(folder, exist_ok=True)

        job["status"] = "downloading"

        def on_progress(done, total, ok, skip, failed):
            job["done"] = done
            job["ok"] = ok
            job["failed"] = failed

        ok, skip, failed = await scraper.download_pages(book_id, pages, folder, on_progress)

        # one retry pass for failures
        if failed:
            job["status"] = "retrying"
            ok2, skip2, failed = await scraper.download_pages(book_id, failed, folder)
        job["failed"] = len(failed)

        job["status"] = "building_pdf"
        pdf_path = os.path.join(config.WORK_DIR, f"yct_{book_id}_p{start}-{end}.pdf")
        mb, ocr_used = pdfbuilder.build(folder, pdf_path, job["do_ocr"])
        if mb <= 0:
            raise RuntimeError("PDF build produced no output")

        job["status"] = "uploading"
        parts = pdfbuilder.split_for_telegram(pdf_path)
        job["pdf_parts"] = len(parts)
        quality = "searchable OCR" if ocr_used else "max-quality lossless"
        for i, part in enumerate(parts, 1):
            cap = f"<b>{title}</b> | pages {start}-{end} | {quality}"
            if len(parts) > 1:
                cap += f" [part {i}/{len(parts)}]"
            await tg.send_document(part, cap, chat)

        # cleanup ephemeral files
        shutil.rmtree(folder, ignore_errors=True)
        for p in parts:
            try: os.remove(p)
            except: pass
        try:
            if os.path.exists(pdf_path):
                os.remove(pdf_path)
        except: pass

        job["status"] = "done"
        if chat:
            await tg.send_message(
                f"<b>✓ Done</b>\n{title}\n{job['ok']} pages | {quality}", chat)

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


def start_worker():
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    asyncio.create_task(_worker())
