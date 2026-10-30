"""Optional independent distributed YCT worker.

Run as a separate Python process with YCT_DISPATCH_URL and YCT_DISPATCH_TOKEN.
Each worker claims a leased page range, assembles and uploads its PDF.
Requires the dispatcher to be reachable and configured with the same secret.
"""
import asyncio
import os
import socket
import tempfile
import uuid
from pathlib import Path
import httpx
import scraper
import pdfbuilder
import telegram

URL = os.getenv("YCT_DISPATCH_URL", "").rstrip("/")
TOKEN = os.getenv("YCT_DISPATCH_TOKEN", "")
OWNER = socket.gethostname() + "-" + uuid.uuid4().hex[:8]

async def api(client, method, path, payload=None):
    r = await client.request(method, URL + path, json=payload)
    r.raise_for_status()
    return r.json()

async def run_job(client, job):
    ident = job["id"]
    async def heartbeat():
        while True:
            await asyncio.sleep(45)
            await api(client, "POST", f"/jobs/{ident}/heartbeat", {"owner": OWNER})
    task = asyncio.create_task(heartbeat())
    try:
        book_id = int(job["book_id"])
        first, last = int(job["start_page"]), int(job["end_page"])
        with tempfile.TemporaryDirectory(prefix="yct-dist-") as folder:
            image_dir = str(Path(folder) / "pages")
            os.makedirs(image_dir)
            pages = list(range(first, last + 1))
            ok, skipped, failed = await scraper.download_pages(book_id, pages, image_dir)
            if failed:
                _, _, failed = await scraper.download_pages(book_id, failed, image_dir)
            if failed:
                raise RuntimeError(f"Missing pages: {failed}")
            pdf_path = str(Path(folder) / "book.pdf")
            await asyncio.to_thread(pdfbuilder.build, image_dir, pdf_path, False)
            from pypdf import PdfReader
            if len(PdfReader(pdf_path).pages) != len(pages):
                raise RuntimeError("PDF page count mismatch")
            parts = await asyncio.to_thread(pdfbuilder.split_for_telegram, pdf_path)
            if not parts:
                raise RuntimeError("PDF produced no uploadable parts")
            for index, part in enumerate(parts, 1):
                caption = f"{job['title']} | pages {first}-{last} | part {index}/{len(parts)}"
                result = await telegram.send_document(part, caption=caption)
                if not result:
                    raise RuntimeError("Telegram upload failed")
        await api(client, "POST", f"/jobs/{ident}/complete", {"owner": OWNER, "result": {"pages": len(pages), "parts": len(parts)}})
    except Exception as exc:
        await api(client, "POST", f"/jobs/{ident}/fail", {"owner": OWNER, "result": {"error": str(exc)[:400]}})
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

async def main():
    if not URL or not TOKEN:
        raise SystemExit("Set YCT_DISPATCH_URL and YCT_DISPATCH_TOKEN")
    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + TOKEN}, timeout=45) as client:
        while True:
            try:
                response = await api(client, "POST", "/claim", {"owner": OWNER})
                if response.get("job"):
                    await run_job(client, response["job"])
                else:
                    await asyncio.sleep(4)
            except Exception as exc:
                print("Dispatcher unavailable:", str(exc)[:150], flush=True)
                await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
