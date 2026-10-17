"""FastAPI app — web UI + REST API + Telegram bot webhook, single process.

Routes:
  GET  /                            -> web UI (password-gated if SITE_PASSWORD set)
  POST /login                       -> submit site password, sets auth cookie
  GET  /api/categories              -> list categories
  GET  /api/books?cat=ID            -> list books in a category
  GET  /api/pages?book_id=ID        -> total page count for a book
  GET  /api/books/{id}/page/{n}     -> stream one page image (in-browser preview, no disk)
  POST /api/download                -> queue a download job
  GET  /api/jobs                    -> all jobs
  GET  /api/jobs/{id}               -> one job's status
  GET  /api/jobs/{id}/download/{n}  -> download a finished PDF (browser)
  GET  /api/jobs/{id}/read          -> open the ready PDF inline (in-browser reader)
  POST /api/jobs/{id}/confirm       -> confirm a downloaded part, advance to next
  DELETE /api/jobs/{id}             -> cancel/delete a job + wipe its files on disk
  POST /tg/{secret}                 -> Telegram webhook
  GET  /healthz                     -> health check (used by Render itself)
  GET  /ping                        -> lightweight always-200 check (UptimeRobot etc.)
"""
import os
import hmac
import hashlib
import asyncio
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse, Response
from pydantic import BaseModel

import config
import scraper
import jobs
import telegram as tg

app = FastAPI(title="YCT Downloader")

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------- Site password gate ----------------
# Set SITE_PASSWORD in env to require a password before anyone can use the
# site. Leave it blank/unset to disable the gate entirely. Paths needed by
# Render's health check, UptimeRobot, and the Telegram webhook are always
# left open so the bot and uptime monitor keep working.
COOKIE_NAME = "yct_auth"
_OPEN_PATHS = {"/healthz", "/ping", "/login"}

def _auth_token() -> str:
    return hmac.new(config.SITE_PASSWORD.encode(), b"yct-auth-ok", hashlib.sha256).hexdigest()

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>YCT Downloader — Locked</title>
<style>
:root{--bg:#05060f;--acc:#00f0ff;--acc2:#ff2bd6;--txt:#eafcff;--card:rgba(13,18,38,.7);--border:rgba(0,240,255,.3)}
*{box-sizing:border-box;margin:0;padding:0}
body{min-height:100dvh;display:flex;align-items:center;justify-content:center;background:radial-gradient(circle at 50% 20%,#0d0b29,#05060f 70%);
font:15px/1.6 'Space Grotesk',system-ui,sans-serif;color:var(--txt);padding:20px}
.box{width:100%;max-width:360px;background:var(--card);border:1px solid var(--border);border-radius:16px;
padding:32px 28px;backdrop-filter:blur(16px);box-shadow:0 0 40px rgba(0,240,255,.15);text-align:center}
.icon{font-size:38px;margin-bottom:14px;filter:drop-shadow(0 0 10px var(--acc))}
h1{font-size:15px;letter-spacing:.08em;text-transform:uppercase;margin-bottom:6px;font-weight:800;
background:linear-gradient(90deg,var(--acc),var(--acc2));-webkit-background-clip:text;background-clip:text;color:transparent}
p{font-size:13px;color:#a9c4d8;margin-bottom:22px}
input{width:100%;background:#05060f;border:1px solid var(--border);border-radius:8px;color:var(--txt);
padding:13px 14px;font:inherit;outline:none;margin-bottom:14px;text-align:center;letter-spacing:.1em}
input:focus{border-color:var(--acc);box-shadow:0 0 0 3px rgba(0,240,255,.15)}
button{width:100%;border:none;border-radius:8px;padding:13px;font:inherit;font-weight:700;cursor:pointer;
background:linear-gradient(135deg,var(--acc),var(--acc2));color:#03060d;letter-spacing:.04em}
button:hover{filter:brightness(1.1)}
.err{color:#ff3860;font-size:13px;margin-bottom:14px}
</style></head>
<body>
<div class="box">
  <div class="icon">&#128274;</div>
  <h1>Access Restricted</h1>
  <p>Enter the password to use YCT Downloader.</p>
  __ERROR__
  <form method="POST" action="/login">
    <input type="password" name="password" placeholder="PASSWORD" autofocus required/>
    <button type="submit">Unlock</button>
  </form>
</div>
</body></html>"""


@app.middleware("http")
async def password_gate(request: Request, call_next):
    if not config.SITE_PASSWORD:
        return await call_next(request)
    path = request.url.path
    if path in _OPEN_PATHS or path.startswith("/tg/"):
        return await call_next(request)
    if request.cookies.get(COOKIE_NAME) == _auth_token():
        return await call_next(request)
    if request.method == "GET" and "text/html" in (request.headers.get("accept") or ""):
        return HTMLResponse(LOGIN_PAGE.replace("__ERROR__", ""), status_code=401, headers=_NO_CACHE_HEADERS)
    return JSONResponse({"detail": "unauthorized"}, status_code=401)


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    pw = (form.get("password") or "").strip()
    if config.SITE_PASSWORD and pw == config.SITE_PASSWORD:
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie(COOKIE_NAME, _auth_token(), max_age=60 * 60 * 24 * 30,
                         httponly=True, samesite="lax")
        return resp
    err = '<div class="err">Incorrect password — try again.</div>'
    return HTMLResponse(LOGIN_PAGE.replace("__ERROR__", err), status_code=401, headers=_NO_CACHE_HEADERS)


@app.on_event("shutdown")
async def shutdown():
    await scraper.close_client()


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
    # Only YCT credentials are required for core functionality.
    # Telegram vars are optional (browser-download works without them).
    missing_critical = [k for k, v in {
        "YCT_EMAIL": config.EMAIL,
        "YCT_PASSWORD": config.PASSWORD,
    }.items() if not v]
    missing_all = config.validate()
    return {"ok": not missing_critical, "missing_env": missing_all}


@app.api_route("/ping", methods=["GET", "HEAD"])
async def ping():
    """Ultra-light always-200 endpoint for uptime monitors (UptimeRobot etc.)
    — does no scraping/env checks, just proves the process is alive so the
    free-tier Render instance doesn't spin down."""
    return {"status": "alive"}


_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
}


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def index(request: Request):
    if request.method == "HEAD":
        return HTMLResponse(content="", status_code=200, headers=_NO_CACHE_HEADERS)
    fpath = os.path.join(HERE, "static", "index.html")
    if os.path.exists(fpath):
        return FileResponse(fpath, headers=_NO_CACHE_HEADERS)
    return HTMLResponse("<h1>YCT Downloader</h1><p>UI file missing.</p>", headers=_NO_CACHE_HEADERS)


# ---------------- REST API ----------------
# Small process-local TTL cache. Render restarts discard it safely; no paid
# database or additional always-on service is needed for these optimizations.
_CACHE_TTL = {"categories": 15 * 60, "books": 30 * 60, "pages": 30 * 60}
_API_CACHE: dict[tuple, tuple[float, object]] = {}
_API_INFLIGHT: dict[tuple, asyncio.Task] = {}
_API_CACHE_MAX = 64
_API_CACHE_LOCK = asyncio.Lock()


def _api_cache_get(key):
    entry = _API_CACHE.get(key)
    if not entry:
        return None
    expiry, value = entry
    if expiry <= asyncio.get_running_loop().time():
        _API_CACHE.pop(key, None)
        return None
    return value


def _api_cache_set(key, value, ttl):
    now = asyncio.get_running_loop().time()
    if ttl > 0:
        _API_CACHE[key] = (now + ttl, value)
    if len(_API_CACHE) > _API_CACHE_MAX:
        for old_key, (expiry, _) in list(_API_CACHE.items()):
            if expiry <= now:
                _API_CACHE.pop(old_key, None)
        while len(_API_CACHE) > _API_CACHE_MAX:
            _API_CACHE.pop(next(iter(_API_CACHE)))


async def _cached_api_value(key, ttl, producer):
    cached = _api_cache_get(key)
    if cached is not None:
        return cached
    async with _API_CACHE_LOCK:
        cached = _api_cache_get(key)
        if cached is not None:
            return cached
        task = _API_INFLIGHT.get(key)
        if task is None:
            task = asyncio.create_task(producer())
            _API_INFLIGHT[key] = task
    try:
        value = await task
    finally:
        async with _API_CACHE_LOCK:
            if _API_INFLIGHT.get(key) is task:
                _API_INFLIGHT.pop(key, None)
    _api_cache_set(key, value, ttl)
    return value


async def _ensure_upstream_login():
    try:
        ok = await asyncio.wait_for(scraper.login(), timeout=30)
    except asyncio.TimeoutError:
        raise HTTPException(504, "YCT login timed out — please try again shortly")
    if not ok:
        raise HTTPException(502, "YCT login failed — check YCT_EMAIL / YCT_PASSWORD")
    return True


@app.get("/api/categories")
async def api_categories(refresh: bool = False):
    async def produce():
        await _ensure_upstream_login()
        categories = await scraper.get_categories()
        return [{"id": key, "name": name} for key, name in sorted(categories.items())]
    value = await _cached_api_value(("categories",), 0 if refresh else _CACHE_TTL["categories"], produce)
    return [dict(row) for row in value]


@app.get("/api/books")
async def api_books(cat: int | None = None, refresh: bool = False, all: bool = False):
    async def resolve_category(category_id: int):
        async def produce():
            await _ensure_upstream_login()
            raw_books = await scraper.get_books_in_category(category_id)
            semaphore = asyncio.Semaphore(4)

            async def resolve(book):
                async with semaphore:
                    name, book_id = await scraper.get_book_details(book["detail_id"])
                if not book_id:
                    return None
                return {"detail_id": book["detail_id"], "book_id": book_id,
                        "name": name, "cover": book.get("cover", ""),
                        "category_id": category_id}
            rows = await asyncio.gather(*(resolve(book) for book in raw_books))
            return [row for row in rows if row]
        value = await _cached_api_value(("books", int(category_id)), 0 if refresh else _CACHE_TTL["books"], produce)
        return [dict(row) for row in value]

    await _ensure_upstream_login()
    if all or cat is None:
        categories = await _cached_api_value(
            ("category_map",), 0 if refresh else _CACHE_TTL["categories"],
            lambda: scraper.get_categories()
        )
        merged = []
        seen = set()
        for category_id in sorted(categories):
            for book in await resolve_category(int(category_id)):
                if book["book_id"] not in seen:
                    seen.add(book["book_id"])
                    merged.append(book)
        return merged
    return await resolve_category(int(cat))


@app.get("/api/pages")
async def api_pages(book_id: int, refresh: bool = False):
    async def produce():
        await _ensure_upstream_login()
        total = await scraper.get_total_pages(book_id)
        return {"book_id": book_id, "total_pages": total}
    value = await _cached_api_value(("pages", int(book_id)), 0 if refresh else _CACHE_TTL["pages"], produce)
    return dict(value)


# ---------------- In-browser page preview (no disk write, no full job) ----------------
# Strictly bounded LRU/TTL preview cache; book images never touch disk.
_PAGE_CACHE: dict[tuple[int, int], tuple[float, bytes, str]] = {}
_PAGE_CACHE_ORDER: list[tuple[int, int]] = []
_PAGE_CACHE_MAX = 8
_PAGE_CACHE_BYTES_MAX = 8 * 1024 * 1024
_PAGE_CACHE_BYTES = 0
_PAGE_CACHE_TTL = 15 * 60
_PAGE_INFLIGHT: dict[tuple[int, int], asyncio.Task] = {}
_PAGE_CACHE_LOCK = asyncio.Lock()


def _get_cached_page(key):
    global _PAGE_CACHE_BYTES
    entry = _PAGE_CACHE.get(key)
    if not entry:
        return None
    expiry, data, ext = entry
    if expiry <= asyncio.get_running_loop().time():
        _PAGE_CACHE.pop(key, None)
        try:
            _PAGE_CACHE_ORDER.remove(key)
        except ValueError:
            pass
        _PAGE_CACHE_BYTES = max(0, _PAGE_CACHE_BYTES - len(data))
        return None
    try:
        _PAGE_CACHE_ORDER.remove(key)
    except ValueError:
        pass
    _PAGE_CACHE_ORDER.append(key)
    return data, ext


def _cache_page(key, value):
    global _PAGE_CACHE_BYTES
    data, ext = value
    if len(data) > _PAGE_CACHE_BYTES_MAX:
        return
    previous = _PAGE_CACHE.pop(key, None)
    if previous:
        _PAGE_CACHE_BYTES = max(0, _PAGE_CACHE_BYTES - len(previous[1]))
        try:
            _PAGE_CACHE_ORDER.remove(key)
        except ValueError:
            pass
    _PAGE_CACHE[key] = (asyncio.get_running_loop().time() + _PAGE_CACHE_TTL, data, ext)
    _PAGE_CACHE_BYTES += len(data)
    _PAGE_CACHE_ORDER.append(key)
    while _PAGE_CACHE_ORDER and (len(_PAGE_CACHE_ORDER) > _PAGE_CACHE_MAX or _PAGE_CACHE_BYTES > _PAGE_CACHE_BYTES_MAX):
        old_key = _PAGE_CACHE_ORDER.pop(0)
        removed = _PAGE_CACHE.pop(old_key, None)
        if removed:
            _PAGE_CACHE_BYTES = max(0, _PAGE_CACHE_BYTES - len(removed[1]))


async def _fetch_preview_page(book_id: int, page: int):
    await _ensure_upstream_login()
    return await scraper.fetch_page_bytes(book_id, page)


@app.get("/api/books/{book_id}/page/{page}")
async def api_book_page(book_id: int, page: int, request: Request):
    if book_id < 1 or page < 1:
        raise HTTPException(400, "book_id and page must be positive")
    key = (book_id, page)
    cached = _get_cached_page(key)
    if cached:
        data, ext = cached
    else:
        async with _PAGE_CACHE_LOCK:
            cached = _get_cached_page(key)
            task = _PAGE_INFLIGHT.get(key)
            if cached:
                data, ext = cached
                task = None
            elif task is None:
                task = asyncio.create_task(_fetch_preview_page(book_id, page))
                _PAGE_INFLIGHT[key] = task
        if task is not None:
            try:
                result = await task
            finally:
                async with _PAGE_CACHE_LOCK:
                    if _PAGE_INFLIGHT.get(key) is task:
                        _PAGE_INFLIGHT.pop(key, None)
            if not result:
                raise HTTPException(404, "page not available")
            data, ext = result
            _cache_page(key, result)
    etag = hashlib.sha256(data).hexdigest()[:24]
    headers = {"Cache-Control": "private, max-age=900, stale-while-revalidate=60", "ETag": f'"{etag}"'}
    if request.headers.get("if-none-match", "").strip('"') == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type="image/png" if ext == "png" else "image/jpeg", headers=headers)


@app.get("/api/jobs/{jid}/read")
async def api_job_read(jid: str):
    """Open the currently-ready PDF inline (for the in-browser reader)
    instead of forcing a download. Doesn't touch the consumed/_downloading
    flags — reading is independent of the download/confirm flow, and the
    file is only ever deleted when the user taps 'Done'."""
    j = jobs.get_job(jid)
    if not j:
        raise HTTPException(404, "job not found")
    cp = j.get("current_part")
    if not cp:
        raise HTTPException(404, "no part ready yet")
    path = cp.get("path", "")
    if not path or not os.path.exists(path):
        raise HTTPException(410, "file not available")
    filename = cp["name"]
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )



class CategoryArchiveReq(BaseModel):
    category_name: str = "Selected category"
    books: list[dict]
    max_books: int = 5000


@app.post("/api/category-download")
async def api_category_download(req: CategoryArchiveReq):
    if not req.books:
        raise HTTPException(400, "No books are loaded for this category")
    if len(req.books) > min(req.max_books, 5000):
        raise HTTPException(413, "Too many books in one archive; choose a smaller category")
    books = []
    seen = set()
    for item in req.books:
        try:
            book_id = int(item.get("book_id") or 0)
        except (TypeError, ValueError):
            continue
        if book_id > 0 and book_id not in seen:
            seen.add(book_id)
            books.append({"book_id": book_id, "name": str(item.get("name") or f"Book {book_id}")[:180],
                          "total_pages": item.get("total_pages")})
    if not books:
        raise HTTPException(400, "No valid books were provided")
    batch_id = jobs.start_category_archive(req.category_name[:120], books)
    return {"job_id": batch_id, "total_books": len(books)}


@app.get("/api/category-download/{batch_id}")
async def api_category_download_status(batch_id: str):
    status = jobs.get_category_archive(batch_id)
    if not status:
        raise HTTPException(404, "Category download job not found")
    return status


@app.get("/api/category-download/{batch_id}/file")
async def api_category_download_file(batch_id: str):
    path = jobs.category_archive_path(batch_id)
    if not path:
        raise HTTPException(409, "Archive is not ready yet")
    filename = _safe_job_filename(f"YCT_{batch_id}.zip")
    return FileResponse(path, media_type="application/zip", filename=filename)


@app.delete("/api/category-download/{batch_id}")
async def api_category_download_cancel(batch_id: str):
    if not jobs.cancel_category_archive(batch_id):
        raise HTTPException(404, "Category download job not found")
    return {"ok": True}


def _safe_job_filename(value: str) -> str:
    import re
    return re.sub(r'[^A-Za-z0-9._ -]+', '_', value)[:120] or "YCT_books.zip"


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
    telegram: bool = False


@app.post("/api/download")
async def api_download(req: DownloadReq):
    jid = jobs.new_job(
        book_id=req.book_id, title=req.title, cover=req.cover,
        start=req.start, end=req.end, do_ocr=req.do_ocr,
        total_pages=req.total_pages,
        notify_chat=(req.notify_chat or config.TG_CHAT_ID) if req.telegram else None,
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
    # Mark _downloading to block concurrent duplicate requests (NOT consumed —
    # consumed is only set when the user clicks Done, so the buttons stay visible)
    if cp.get("_downloading"):
        raise HTTPException(409, "download already in progress")
    cp["_downloading"] = True

    def _reset_downloading():
        cp["_downloading"] = False

    return FileResponse(
        path,
        media_type="application/pdf",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        background=BackgroundTask(_reset_downloading),
    )


@app.post("/api/jobs/{jid}/confirm")
async def api_job_confirm(jid: str):
    """User clicked 'Done — get next part'. Releases the gate in the worker."""
    ok = jobs.confirm_part(jid)
    if not ok:
        raise HTTPException(404, "job not found or no part waiting")
    return {"ok": True}


@app.delete("/api/jobs/{jid}")
async def api_job_delete(jid: str):
    """Cancel a queued/running job or remove a finished one — and free
    whatever it had on disk (no leftover pages/PDFs on Render's storage)."""
    ok = jobs.cancel_job(jid)
    if not ok:
        raise HTTPException(404, "no such job")
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
