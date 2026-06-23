"""FastAPI app — web UI + REST API + Telegram bot webhook, single process.

Routes:
  GET  /                            -> web UI
  GET  /api/categories              -> list categories
  GET  /api/books?cat=ID            -> list books in a category
  POST /api/download                -> queue a download job
  GET  /api/jobs                    -> all jobs
  GET  /api/jobs/{id}               -> one job's status
  GET  /api/jobs/{id}/download/{n}  -> download a finished PDF (browser)
  POST /tg/{secret}                 -> Telegram webhook
  GET  /healthz                     -> health check
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
    if config.PUBLIC_URL and config.TG_TOKEN:
        try:
            await tg.set_webhook()
        except Exception:
            pass


@app.api_route("/healthz", methods=["GET", "HEAD"])
async def healthz():
    missing = config.validate()
    return {"ok": not missing, "missing_env": missing}


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def index(request: Request):
    if request.method == "HEAD":
        return HTMLResponse(content="", status_code=200)
    fpath = os.path.join(HERE, "static", "index.html")
    if os.path.exists(fpath):
        return FileResponse(fpath)
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
    sem = asyncio.Semaphore(5)

    async def resolve(b):
        async with sem:
            name, book_id = await scraper.get_book_details(b["detail_id"])
        return {"detail_id": b["detail_id"], "book_id": book_id,
                "name": name, "cover": b.get("cover", "")}

    out = await asyncio.gather(*(resolve(b) for b in raw))
    return [b for b in out if b["book_id"]]


@app.get("/api/pages")
async def api_pages(book_id: int):
    if not await scraper.login():
        raise HTTPException(502, "login failed")
    total = await scraper.get_total_pages(book_id)
    return {"book_id": book_id, "total_pages": total}


class DownloadReq(BaseModel):
    book_id: int
    title: str = "Untitled"
    cover: str = ""
    start: int = 1
    end: int | None = None
    do_ocr: bool = False
    total_pages: int | None = None
    notify_chat: str | None = None
    browser_download: bool = False


@app.post("/api/download")
async def api_download(req: DownloadReq):
    jid = jobs.new_job(
        book_id=req.book_id, title=req.title, cover=req.cover,
        start=req.start, end=req.end, do_ocr=req.do_ocr,
        total_pages=req.total_pages,
        notify_chat=req.notify_chat if not req.browser_download else (req.notify_chat or config.TG_CHAT_ID),
        browser_download=req.browser_download,
    )
    return {"job_id": jid}


def _safe_job(j: dict) -> dict:
    """Return a job dict safe to send to the browser (no server paths, no asyncio objects)."""
    safe = {k: v for k, v in j.items() if not k.startswith("_")}
    # Sanitise current_part — strip path
    cp = j.get("current_part")
    if cp:
        safe["current_part"] = {k: v for k, v in cp.items() if k != "path"}
    else:
        safe["current_part"] = None
    return safe


@app.get("/api/jobs")
async def api_jobs():
    return [_safe_job(j) for j in jobs.all_jobs()]


@app.get("/api/jobs/{jid}")
async def api_job(jid: str):
    j = jobs.get_job(jid)
    if not j:
        raise HTTPException(404, "no such job")
    return _safe_job(j)


@app.get("/api/jobs/{jid}/download")
async def api_job_download(jid: str):
    """Serve the current ready part as a browser download."""
    from starlette.background import BackgroundTask
    j = jobs.get_job(jid)
    if not j:
        raise HTTPException(404, "job not found")
    cp = j.get("current_part")
    if not cp:
        raise HTTPException(404, "no part ready yet")
    if cp.get("consumed"):
        raise HTTPException(410, "already downloaded — click 'Done' to get the next part")
    path = cp.get("path", "")
    if not path or not os.path.exists(path):
        raise HTTPException(410, "file not available")
    filename = cp["name"]
    # Mark consumed immediately to block double-downloads while streaming
    cp["consumed"] = True
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/jobs/{jid}/confirm")
async def api_job_confirm(jid: str):
    """User clicked 'Done — get next part'. Releases the gate in the worker."""
    ok = jobs.confirm_part(jid)
    if not ok:
        raise HTTPException(404, "job not found or no part waiting")
    return {"ok": True}


# ---------------- Telegram bot webhook ----------------

_CHAT_STATE: dict[str, dict] = {}
_CATS_CACHE: dict[int, str] = {}
_BOOKS_CACHE: dict[int, list] = {}

CATS_PER_PAGE = 12
BOOKS_PER_PAGE = 8


async def _bot_reply(chat_id, text, buttons=None):
    return await tg.send_message(text, str(chat_id), buttons=buttons)


def _chunk_buttons(items, per_row=2):
    rows, row = [], []
    for it in items:
        row.append(it)
        if len(row) == per_row:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return rows


async def _ensure_cats():
    global _CATS_CACHE
    if not _CATS_CACHE:
        if await scraper.login():
            _CATS_CACHE = await scraper.get_categories()
    return _CATS_CACHE


async def _show_categories(chat_id, page=0, edit=None):
    cats = await _ensure_cats()
    if not cats:
        await _bot_reply(chat_id, "Login failed — check credentials.")
        return
    items = sorted(cats.items())
    total_pages = (len(items) + CATS_PER_PAGE - 1) // CATS_PER_PAGE
    page = max(0, min(page, total_pages - 1))
    chunk = items[page * CATS_PER_PAGE:(page + 1) * CATS_PER_PAGE]
    btns = [(f"{name[:28]}", f"cat:{cid}:0") for cid, name in chunk]
    rows = _chunk_buttons(btns, per_row=1)
    nav = []
    if page > 0:
        nav.append(("« Prev", f"catpage:{page-1}"))
    if page < total_pages - 1:
        nav.append(("Next »", f"catpage:{page+1}"))
    if nav:
        rows.append(nav)
    text = f"<b>Categories</b> (page {page+1}/{total_pages})\nPick one:"
    if edit:
        await tg.edit_message(chat_id, edit, text, rows)
    else:
        await _bot_reply(chat_id, text, rows)


async def _show_books(chat_id, cat_id, page=0, edit=None):
    global _BOOKS_CACHE
    if cat_id not in _BOOKS_CACHE:
        await _ensure_cats()
        raw = await scraper.get_books_in_category(cat_id)
        resolved = []
        for b in raw[:60]:
            name, bid = await scraper.get_book_details(b["detail_id"])
            if bid:
                resolved.append({"detail_id": b["detail_id"], "book_id": bid,
                                 "name": name, "cover": b.get("cover", "")})
        _BOOKS_CACHE[cat_id] = resolved
    books = _BOOKS_CACHE[cat_id]
    if not books:
        text = "No books found in this category."
        if edit: await tg.edit_message(chat_id, edit, text)
        else: await _bot_reply(chat_id, text)
        return
    total_pages = (len(books) + BOOKS_PER_PAGE - 1) // BOOKS_PER_PAGE
    page = max(0, min(page, total_pages - 1))
    chunk = books[page * BOOKS_PER_PAGE:(page + 1) * BOOKS_PER_PAGE]
    btns = []
    for b in chunk:
        idx = books.index(b)
        btns.append((b["name"][:32], f"book:{cat_id}:{idx}"))
    rows = _chunk_buttons(btns, per_row=1)
    nav = []
    if page > 0:
        nav.append(("« Prev", f"bookpage:{cat_id}:{page-1}"))
    nav.append(("⌂ Categories", "home"))
    if page < total_pages - 1:
        nav.append(("Next »", f"bookpage:{cat_id}:{page+1}"))
    rows.append(nav)
    cat_name = _CATS_CACHE.get(cat_id, f"Category {cat_id}")
    text = f"<b>{cat_name}</b> (page {page+1}/{total_pages})\nPick a book:"
    if edit:
        await tg.edit_message(chat_id, edit, text, rows)
    else:
        await _bot_reply(chat_id, text, rows)


async def _show_book_detail(chat_id, cat_id, idx, edit=None):
    books = _BOOKS_CACHE.get(cat_id, [])
    if idx >= len(books):
        await _bot_reply(chat_id, "Book not found — /start again."); return
    b = books[idx]
    if edit:
        await tg.edit_message(chat_id, edit, f"Checking pages for <b>{b['name']}</b>…")
    total = await scraper.get_total_pages(b["book_id"])
    total_txt = str(total) if total else "unknown"
    _CHAT_STATE[str(chat_id)] = {
        "book_id": b["book_id"], "detail_id": b["detail_id"],
        "title": b["name"], "total": total, "cat_id": cat_id, "idx": idx,
    }
    rows = [
        [("⬇ Whole book", f"dl:whole"), ("First 10", f"dl:10")],
        [("First 50", f"dl:50"), ("Custom range", f"dl:custom")],
        [("« Back", f"cat:{cat_id}:0"), ("⌂ Categories", "home")],
    ]
    text = (f"<b>{b['name']}</b>\n"
            f"Book ID: <code>{b['book_id']}</code>\n"
            f"Total pages: <b>{total_txt}</b>\n\n"
            f"Choose what to download:")
    if edit:
        await tg.edit_message(chat_id, edit, text, rows)
    else:
        await _bot_reply(chat_id, text, rows)


async def _queue_from_state(chat_id, start, end):
    st = _CHAT_STATE.get(str(chat_id))
    if not st:
        await _bot_reply(chat_id, "Session expired — /start again."); return
    jid = jobs.new_job(
        book_id=st["book_id"], title=st["title"], cover="",
        start=start, end=end, do_ocr=False, total_pages=st.get("total"),
        notify_chat=str(chat_id), detail_id=st.get("detail_id"),
    )
    rng = f"pages {start}-{end}" if end else "whole book"
    await _bot_reply(chat_id,
        f"✅ Queued <b>{st['title']}</b> ({rng}).\n"
        f"I'll send the PDF here when it's ready.")


async def handle_callback(cb: dict):
    cb_id = cb["id"]
    data = cb.get("data", "")
    msg = cb.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    msg_id = msg.get("message_id")
    await tg.answer_callback(cb_id)

    if data == "home":
        await _show_categories(chat_id, 0, edit=msg_id); return
    if data.startswith("catpage:"):
        await _show_categories(chat_id, int(data.split(":")[1]), edit=msg_id); return
    if data.startswith("cat:"):
        _, cid, pg = data.split(":")
        await _show_books(chat_id, int(cid), int(pg), edit=msg_id); return
    if data.startswith("bookpage:"):
        _, cid, pg = data.split(":")
        await _show_books(chat_id, int(cid), int(pg), edit=msg_id); return
    if data.startswith("book:"):
        _, cid, idx = data.split(":")
        await _show_book_detail(chat_id, int(cid), int(idx), edit=msg_id); return
    if data.startswith("dl:"):
        what = data.split(":")[1]
        st = _CHAT_STATE.get(str(chat_id))
        if not st:
            await _bot_reply(chat_id, "Session expired — /start again."); return
        if what == "whole":
            await _queue_from_state(chat_id, 1, None)
        elif what == "10":
            await _queue_from_state(chat_id, 1, 10)
        elif what == "50":
            await _queue_from_state(chat_id, 1, 50)
        elif what == "custom":
            st["awaiting"] = "range"
            await _bot_reply(chat_id,
                "Send the page range as <code>start end</code> "
                "(e.g. <code>5 40</code>), or a single number for first-N.")
        return


async def handle_update(update: dict):
    if "callback_query" in update:
        await handle_callback(update["callback_query"])
        return

    msg = update.get("message") or update.get("channel_post")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    if not text:
        return

    st = _CHAT_STATE.get(str(chat_id))
    if st and st.get("awaiting") == "range":
        st.pop("awaiting", None)
        nums = [int(x) for x in text.replace(",", " ").split() if x.isdigit()]
        if not nums:
            await _bot_reply(chat_id, "Couldn't read numbers. /start to retry."); return
        if len(nums) == 1:
            await _queue_from_state(chat_id, 1, nums[0])
        else:
            await _queue_from_state(chat_id, nums[0], nums[1])
        return

    parts = text.split()
    cmd = parts[0].lower().lstrip("/").split("@")[0]

    if cmd in ("start", "help", "menu"):
        await _bot_reply(chat_id,
            "📚 <b>YCT Downloader</b>\nBrowse and download books with buttons below.")
        await _show_categories(chat_id, 0)
        return

    if cmd == "status":
        js = jobs.all_jobs()[:8]
        if not js:
            await _bot_reply(chat_id, "No jobs yet."); return
        lines = [f"• {j['title'][:30]} — {j['status']} ({j['done']}/{j['total'] or '?'})"
                 for j in js]
        await _bot_reply(chat_id, "<b>Recent jobs</b>\n" + "\n".join(lines))
        return

    if cmd == "get":
        if len(parts) < 2 or not parts[1].isdigit():
            await _bot_reply(chat_id, "Usage: /get &lt;book_id&gt; [start] [end]"); return
        book_id = int(parts[1])
        start = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        end = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
        jid = jobs.new_job(book_id, f"Book {book_id}", "", start, end,
                           False, None, notify_chat=str(chat_id))
        await _bot_reply(chat_id, f"✅ Queued book {book_id}.")
        return

    await _bot_reply(chat_id, "Send /start to browse with buttons.")


@app.post("/tg/{secret}")
async def tg_webhook(secret: str, request: Request):
    if secret != config.TG_WEBHOOK_SECRET:
        raise HTTPException(403, "forbidden")
    update = await request.json()
    asyncio.create_task(handle_update(update))
    return JSONResponse({"ok": True})
