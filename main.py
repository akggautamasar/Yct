"""FastAPI app — web UI + REST API + Telegram bot webhook, single process.

Routes:
  GET  /                       -> web UI
  GET  /api/categories         -> list categories
  GET  /api/books?cat=ID       -> list books in a category (with names + page IDs)
  POST /api/download           -> queue a download job
  GET  /api/jobs               -> all jobs
  GET  /api/jobs/{id}          -> one job's status
  POST /tg/{secret}            -> Telegram webhook
  GET  /healthz                -> health check
"""
import os
import asyncio
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel

import config
import scraper
import jobs
import telegram as tg

app = FastAPI(title="YCT Downloader")

HERE = os.path.dirname(os.path.abspath(__file__))


@app.on_event("startup")
async def startup():
    jobs.start_worker()
    # register the Telegram webhook if we know our public URL
    if config.PUBLIC_URL and config.TG_TOKEN:
        try:
            await tg.set_webhook()
        except Exception:
            pass


@app.get("/healthz")
async def healthz():
    missing = config.validate()
    return {"ok": not missing, "missing_env": missing}


@app.get("/", response_class=HTMLResponse)
async def index():
    path = os.path.join(HERE, "static", "index.html")
    if os.path.exists(path):
        return FileResponse(path)
    return HTMLResponse("<h1>YCT Downloader</h1><p>UI file missing.</p>")


# ---------------- REST API ----------------

@app.get("/api/categories")
async def api_categories():
    if not await scraper.login():
        raise HTTPException(502, "login failed")
    cats = await scraper.get_categories()
    return [{"id": k, "name": v} for k, v in sorted(cats.items())]


@app.get("/api/books")
async def api_books(cat: int):
    if not await scraper.login():
        raise HTTPException(502, "login failed")
    raw = await scraper.get_books_in_category(cat)
    out = []
    # resolve names concurrently but politely
    sem = asyncio.Semaphore(5)

    async def resolve(b):
        async with sem:
            name, book_id = await scraper.get_book_details(b["detail_id"])
        return {"detail_id": b["detail_id"], "book_id": book_id,
                "name": name, "cover": b.get("cover", "")}

    out = await asyncio.gather(*(resolve(b) for b in raw))
    return [b for b in out if b["book_id"]]


class DownloadReq(BaseModel):
    book_id: int
    title: str = "Untitled"
    cover: str = ""
    start: int = 1
    end: int | None = None
    do_ocr: bool = False
    total_pages: int | None = None
    notify_chat: str | None = None  # optional override


@app.post("/api/download")
async def api_download(req: DownloadReq):
    jid = jobs.new_job(
        book_id=req.book_id, title=req.title, cover=req.cover,
        start=req.start, end=req.end, do_ocr=req.do_ocr,
        total_pages=req.total_pages,
        notify_chat=req.notify_chat or config.TG_CHAT_ID,
    )
    return {"job_id": jid}


@app.get("/api/jobs")
async def api_jobs():
    return jobs.all_jobs()


@app.get("/api/jobs/{jid}")
async def api_job(jid: str):
    j = jobs.get_job(jid)
    if not j:
        raise HTTPException(404, "no such job")
    return j


# ---------------- Telegram bot webhook ----------------
# Minimal command bot:
#   /start, /help
#   /cats                       -> list categories
#   /books <cat_id>             -> list books in a category
#   /get <book_id> [start] [end] [ocr]
#   /status                     -> recent jobs

async def _bot_reply(chat_id, text):
    await tg.send_message(text, str(chat_id))


async def handle_update(update: dict):
    msg = update.get("message") or update.get("channel_post")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    if not text:
        return

    parts = text.split()
    cmd = parts[0].lower().lstrip("/")
    cmd = cmd.split("@")[0]  # strip @botname

    if cmd in ("start", "help"):
        await _bot_reply(chat_id,
            "<b>YCT Downloader</b>\n"
            "/cats — list categories\n"
            "/books &lt;cat_id&gt; — list books\n"
            "/get &lt;book_id&gt; [start] [end] [ocr] — download\n"
            "/status — recent jobs")
        return

    if cmd == "cats":
        if not await scraper.login():
            await _bot_reply(chat_id, "Login failed."); return
        cats = await scraper.get_categories()
        lines = [f"<code>{k}</code> — {v}" for k, v in sorted(cats.items())]
        await _bot_reply(chat_id, "<b>Categories</b>\n" + "\n".join(lines[:60]))
        return

    if cmd == "books":
        if len(parts) < 2 or not parts[1].isdigit():
            await _bot_reply(chat_id, "Usage: /books &lt;cat_id&gt;"); return
        if not await scraper.login():
            await _bot_reply(chat_id, "Login failed."); return
        raw = await scraper.get_books_in_category(int(parts[1]))
        lines = []
        for b in raw[:40]:
            name, bid = await scraper.get_book_details(b["detail_id"])
            if bid:
                lines.append(f"<code>{bid}</code> — {name[:50]}")
        await _bot_reply(chat_id, "<b>Books</b>\n" + ("\n".join(lines) or "none"))
        return

    if cmd == "get":
        if len(parts) < 2 or not parts[1].isdigit():
            await _bot_reply(chat_id, "Usage: /get &lt;book_id&gt; [start] [end] [ocr]"); return
        book_id = int(parts[1])
        start = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        end = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
        do_ocr = "ocr" in [p.lower() for p in parts[2:]]
        jid = jobs.new_job(book_id, f"Book {book_id}", "", start, end,
                           do_ocr, None, notify_chat=str(chat_id))
        await _bot_reply(chat_id, f"Queued job <code>{jid}</code> for book {book_id}.")
        return

    if cmd == "status":
        js = jobs.all_jobs()[:8]
        if not js:
            await _bot_reply(chat_id, "No jobs yet."); return
        lines = [f"<code>{j['id']}</code> {j['status']} — {j['title'][:30]} "
                 f"({j['done']}/{j['total']})" for j in js]
        await _bot_reply(chat_id, "<b>Jobs</b>\n" + "\n".join(lines))
        return

    await _bot_reply(chat_id, "Unknown command. /help")


@app.post("/tg/{secret}")
async def tg_webhook(secret: str, request: Request):
    if secret != config.TG_WEBHOOK_SECRET:
        raise HTTPException(403, "forbidden")
    update = await request.json()
    # handle in background so we ACK Telegram fast
    asyncio.create_task(handle_update(update))
    return JSONResponse({"ok": True})
