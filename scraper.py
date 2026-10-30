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
            limits=httpx.Limits(max_connections=config.MAX_WORKERS + 4, max_keepalive_connections=config.MAX_WORKERS + 2),
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
    """Find book detail links without depending on a particular card layout."""
    from urllib.parse import urljoin, urlparse
    c = await get_client()
    page = await c.get(f"{config.BASE_URL}/ebook/{cat_id}")
    page.raise_for_status()
    if "/login" in urlparse(str(page.url)).path.lower():
        raise RuntimeError("YCT redirected category request to login")
    soup = BeautifulSoup(page.text, "html.parser")
    books = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(str(page.url), a["href"])
        path = urlparse(href).path.rstrip("/")
        m = re.search(r"/ebook-detail/(\d+)$", path, re.I)
        if not m:
            continue
        detail_id = int(m.group(1))
        if detail_id in seen:
            continue
        seen.add(detail_id)
        img = a.find("img") or (a.parent.find("img") if a.parent else None)
        cover = urljoin(str(page.url), img.get("src", "")) if img and img.get("src") else ""
        books.append({"detail_id": detail_id, "cover": cover})
    if not books:
        raise RuntimeError(f"No book detail links found for category {cat_id}; upstream layout, access or category may have changed")
    return books


async def get_book_details(detail_id: int) -> tuple[str, int | None]:
    from urllib.parse import urljoin, urlparse
    c = await get_client()
    page = await c.get(f"{config.BASE_URL}/ebook-detail/{detail_id}")
    page.raise_for_status()
    if "/login" in urlparse(str(page.url)).path.lower():
        raise RuntimeError("YCT redirected book detail request to login")
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
        name = (soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else "") or f"Book {detail_id}"
    book_id = None
    for a in soup.find_all("a", href=True):
        path = urlparse(urljoin(str(page.url), a["href"])).path
        m = re.search(r"/readbook/(\d+)", path, re.I)
        if m:
            book_id = int(m.group(1))
            break
    if book_id is None:
        m = re.search(r'["\']?(?:readbook|book_id|bookId)["\']?\s*[:=/]\s*["\']?(\d+)', page.text, re.I)
        if m:
            book_id = int(m.group(1))
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


async def check_page_access(book_id: int, page: int = 1) -> tuple[bool, str]:
    """Check a real page image and distinguish upstream errors from access denial."""
    client = await get_client()
    url = f"{config.BASE_URL}/getPage/{book_id}/{page}"
    headers = {**config.HEADERS, "Referer": f"{config.BASE_URL}/readbook/{book_id}/{page}"}
    for attempt in range(2):
        try:
            response = await client.get(url, headers=headers)
            payload = response.text.strip() if response.content[:1] not in (bytes([137]), bytes([255])) else ""
            if response.status_code == 200 and _decode_page(response.content, payload):
                return True, "ok"
            login_page = "/login" in str(response.url).lower() or (
                "<form" in payload.lower() and "password" in payload.lower()
            )
            if response.status_code in (401, 403) or login_page:
                if attempt == 0:
                    global _session_cookie
                    _session_cookie = None
                    if await login():
                        continue
                return False, "YCT rejected the page request; verify account access or session"
            if response.status_code == 404:
                return False, "YCT page endpoint returned 404; check book ID and reader endpoint"
            if response.status_code in (429, 500, 502, 503, 504):
                return False, f"YCT page endpoint unavailable (HTTP {response.status_code})"
            return False, f"YCT returned HTTP {response.status_code} without a recognized page image"
        except httpx.HTTPError as exc:
            return False, f"YCT page request failed ({type(exc).__name__})"
    return False, "YCT page access failed after session refresh"


async def has_access(book_id: int) -> bool:
    ok, _ = await check_page_access(book_id)
    return ok


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
    # Some reader endpoints return a bare base64 image rather than a data URI.
    # Decode only plausible image payloads, and validate the magic bytes to
    # avoid treating an HTML error/login page as a valid book page.
    candidate = text.strip().strip('"').strip("'")
    if candidate and len(candidate) > 100 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", candidate):
        try:
            raw = base64.b64decode("".join(candidate.split()), validate=True)
            if raw[:4] == b"\x89PNG":
                return raw, "png"
            if raw[:2] == b"\xff\xd8":
                return raw, "jpg"
        except (ValueError, base64.binascii.Error):
            pass
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
                await asyncio.sleep(min(2 ** attempt, 12))
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
                await asyncio.sleep(min(2 ** attempt, 12))
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
                return page, "ok"
            await asyncio.sleep(min(2 ** attempt, 10))
        except Exception:
            await asyncio.sleep(min(2 ** attempt, 15))
    return page, "failed"


async def close_client():
    """Close the shared async client during graceful shutdown/redeploy."""
    global _client, _session_cookie
    if _client is not None:
        await _client.aclose()
        _client = None
    _session_cookie = None


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
