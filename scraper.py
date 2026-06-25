"""Scraper — login + fetch categories, books, page images.

Uses httpx.AsyncClient so it integrates cleanly with FastAPI's event loop.
A single shared client holds the ci_session cookie after login.
"""
import re
import base64
import asyncio
import httpx
from bs4 import BeautifulSoup

import config

_client: httpx.AsyncClient | None = None
_login_lock = asyncio.Lock()
_session_cookie: str | None = None


async def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            headers=config.HEADERS,
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=config.MAX_WORKERS + 4),
        )
    return _client


async def login() -> bool:
    """Log in and cache the ci_session cookie. Idempotent + concurrency-safe."""
    global _session_cookie
    async with _login_lock:
        if _session_cookie:
            return True
        c = await get_client()
        await c.get(f"{config.BASE_URL}/login")
        r = await c.post(
            f"{config.BASE_URL}/LoginController/login1",
            data={"email": config.EMAIL, "password": config.PASSWORD},
            headers={**config.HEADERS,
                     "Referer": f"{config.BASE_URL}/login",
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
        if "login" in str(r.url).lower():
            return False
        _session_cookie = c.cookies.get("ci_session")
        return True


def _is_img(b: bytes) -> bool:
    return b[:4] == b"\x89PNG" or b[:2] == b"\xff\xd8"


async def get_categories() -> dict[int, str]:
    c = await get_client()
    page = await c.get(f"{config.BASE_URL}/ebook")
    soup = BeautifulSoup(page.text, "html.parser")
    cats: dict[int, str] = {}
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        text = a.get_text(strip=True)
        m = re.match(r".*/ebook/(\d+)$", href)
        if m and text:
            cats[int(m.group(1))] = text
    return cats


async def get_books_in_category(cat_id: int) -> list[dict]:
    c = await get_client()
    page = await c.get(f"{config.BASE_URL}/ebook/{cat_id}")
    soup = BeautifulSoup(page.text, "html.parser")
    imgs = [i.get("src", "") for i in soup.find_all("img")
            if "cover_image" in i.get("src", "")]
    links = [a.get("href", "") for a in soup.find_all("a", href=True)
             if "ebook-detail" in a.get("href", "")]
    books = []
    for idx, href in enumerate(links):
        m = re.search(r"/ebook-detail/(\d+)", href)
        if not m:
            continue
        books.append({"detail_id": int(m.group(1)),
                      "cover": imgs[idx] if idx < len(imgs) else ""})
    return books


async def get_book_details(detail_id: int) -> tuple[str, int | None]:
    c = await get_client()
    page = await c.get(f"{config.BASE_URL}/ebook-detail/{detail_id}")
    soup = BeautifulSoup(page.text, "html.parser")
    name = ""
    for h3 in soup.find_all("h3"):
        if ("font-weight" in h3.get("style", "") and h3.get_text(strip=True)
                and "About Item" not in h3.get_text()):
            name = h3.get_text(strip=True)
            break
    if not name:
        all_h3 = soup.find_all("h3")
        for i, h3 in enumerate(all_h3):
            if "About Item" in h3.get_text() and i > 0:
                name = all_h3[i - 1].get_text(strip=True)
                break
    if not name:
        name = f"Book {detail_id}"
    book_id = None
    for a in soup.find_all("a", href=True):
        m = re.search(r"/readbook/(\d+)", a.get("href", ""))
        if m:
            book_id = int(m.group(1))
            break
    return name, book_id


async def get_total_pages(book_id: int) -> int | None:
    c = await get_client()
    try:
        rb = await c.get(f"{config.BASE_URL}/readbook/{book_id}/1",
                         headers={**config.HEADERS, "Referer": f"{config.BASE_URL}/ebook"})
        for pat in [r"of\s+(\d+)", r"maxPage\s*=\s*(\d+)",
                    r"max_page\s*[=:]\s*(\d+)", r"totalPages?\s*[=:]\s*(\d+)",
                    r'"pages"\s*:\s*(\d+)', r"pageCount\s*[=:]\s*(\d+)"]:
            m = re.search(pat, rb.text, re.IGNORECASE)
            if m:
                val = int(m.group(1))
                if 10 < val < 10000:
                    return val
    except Exception:
        pass
    # binary search fallback — use _page_exists (handles base64 text pages)
    try:
        if not await _page_exists(book_id, 1):
            return None
        lo, hi = 1, 100
        while hi < 5000:
            if await _page_exists(book_id, hi):
                lo = hi; hi *= 2
            else:
                break
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if await _page_exists(book_id, mid):
                lo = mid
            else:
                hi = mid
            await asyncio.sleep(0.05)
        return lo
    except Exception:
        return None


async def _page_exists(book_id: int, page: int) -> bool:
    """True if the page endpoint returns a real image (raw OR base64 text)."""
    c = await get_client()
    try:
        r = await c.get(f"{config.BASE_URL}/getPage/{book_id}/{page}",
                        headers={**config.HEADERS,
                                 "Referer": f"{config.BASE_URL}/readbook/{book_id}/{page}"})
        if r.status_code != 200:
            return False
        if _is_img(r.content):
            return True
        # base64 / data-uri responses come back as text
        txt = r.text.strip() if r.content[:1] not in (b"\x89", b"\xff") else ""
        return _decode_page(r.content, txt) is not None
    except Exception:
        return False


async def has_access(book_id: int) -> bool:
    return await _page_exists(book_id, 1)


def _decode_page(content: bytes, text: str) -> tuple[bytes, str] | None:
    """Return (bytes, ext) for a page response, or None."""
    if content[:4] == b"\x89PNG":
        return content, "png"
    if content[:2] == b"\xff\xd8":
        return content, "jpg"
    for pfx, ext in [("data:image/png;base64,", "png"),
                     ("data:image/jpeg;base64,", "jpg")]:
        if pfx in text:
            b64 = text.split(pfx, 1)[1].split('"')[0].split("'")[0].split("<")[0].strip()
            try:
                return base64.b64decode(b64), ext
            except Exception:
                return None
    return None


async def fetch_page_bytes(book_id: int, page: int) -> tuple[bytes, str] | None:
    """Fetch one page straight into memory — never touches disk. Used by the
    in-browser preview reader so users can flip through a book before
    committing to a full download (keeps Render's free-tier storage/RAM
    untouched: only the single page currently being viewed is ever held)."""
    c = await get_client()
    url = f"{config.BASE_URL}/getPage/{book_id}/{page}"
    ref = {"Referer": f"{config.BASE_URL}/readbook/{book_id}/{page}"}
    for attempt in range(1, config.PAGE_RETRY + 1):
        try:
            resp = await c.get(url, headers={**config.HEADERS, **ref})
            if resp.status_code in (429, 503):
                await asyncio.sleep(min(15 * attempt, 90))
                continue
            if resp.status_code != 200:
                await asyncio.sleep(min(2 ** attempt, 20))
                continue
            decoded = _decode_page(resp.content, resp.text.strip() if resp.content[:1] not in (b"\x89", b"\xff") else "")
            if decoded:
                return decoded
            await asyncio.sleep(min(2 ** attempt, 10))
        except Exception:
            await asyncio.sleep(min(2 ** attempt, 15))
    return None


async def download_page(book_id: int, page: int, folder: str) -> tuple[int, str]:
    """Download one page to folder. Returns (page, status)."""
    import os
    png = os.path.join(folder, f"page_{page:04d}.png")
    jpg = os.path.join(folder, f"page_{page:04d}.jpg")
    if os.path.exists(png) or os.path.exists(jpg):
        return page, "skipped"

    c = await get_client()
    url = f"{config.BASE_URL}/getPage/{book_id}/{page}"
    ref = {"Referer": f"{config.BASE_URL}/readbook/{book_id}/{page}"}

    for attempt in range(1, config.PAGE_RETRY + 1):
        try:
            resp = await c.get(url, headers={**config.HEADERS, **ref})
            if resp.status_code in (429, 503):
                await asyncio.sleep(min(15 * attempt, 90))
                continue
            if resp.status_code != 200:
                await asyncio.sleep(min(2 ** attempt, 20))
                continue
            decoded = _decode_page(resp.content, resp.text.strip() if resp.content[:1] not in (b"\x89", b"\xff") else "")
            if decoded:
                data, ext = decoded
                out = png if ext == "png" else jpg
                with open(out, "wb") as f:
                    f.write(data)
                await asyncio.sleep(config.REQ_DELAY)
                return page, "ok"
            await asyncio.sleep(min(2 ** attempt, 10))
        except Exception:
            await asyncio.sleep(min(2 ** attempt, 15))
    return page, "failed"


async def download_pages(book_id: int, pages: list[int], folder: str,
                         on_progress=None) -> tuple[int, int, list[int]]:
    """Download pages with bounded concurrency. Returns (ok, skip, failed_list)."""
    sem = asyncio.Semaphore(config.MAX_WORKERS)
    ok = skip = 0
    failed: list[int] = []
    done = 0

    async def worker(p):
        nonlocal ok, skip, done
        async with sem:
            _, status = await download_page(book_id, p, folder)
        if status == "ok":
            ok += 1
        elif status == "skipped":
            skip += 1
        else:
            failed.append(p)
        done += 1
        if on_progress:
            on_progress(done, len(pages), ok, skip, len(failed))

    await asyncio.gather(*(worker(p) for p in pages))
    return ok, skip, sorted(failed)
