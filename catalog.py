"""Persistent, incremental catalog for AirYCT.

Only metadata is indexed here. Page images and PDFs continue to use the
existing download/Telegram pipeline. Existing entries are never deleted
because a remote category scan failed.
"""
import asyncio
import logging
import os
import sqlite3
import time

import config
import scraper

log = logging.getLogger(__name__)
DB_PATH = os.environ.get(
    "CATALOG_DB_PATH", os.path.join(config.WORK_DIR, "catalog.sqlite3")
)
AUTO_SYNC = os.environ.get("CATALOG_AUTO_SYNC", "false").lower() in ("1", "true", "yes")
REFRESH_HOURS = max(6, int(os.environ.get("CATALOG_REFRESH_HOURS", "24")))
_sync_task = None
_scheduler_task = None
_state = {"running": False, "phase": "idle", "current_category": "",
          "processed": 0, "total": 0, "added": 0, "errors": []}


def _connect():
    connection = sqlite3.connect(DB_PATH, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=20000")
    return connection


def _initialize():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with _connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                indexed_at INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS books (
                category_id INTEGER NOT NULL,
                detail_id INTEGER NOT NULL,
                book_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                cover TEXT NOT NULL DEFAULT '',
                first_seen INTEGER NOT NULL,
                is_new INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (category_id, detail_id)
            );
            CREATE INDEX IF NOT EXISTS idx_books_reader ON books(book_id);
            CREATE INDEX IF NOT EXISTS idx_books_category ON books(category_id);
            CREATE TABLE IF NOT EXISTS catalog_meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
        """)


_initialize()


def categories():
    with _connect() as db:
        return [{"id": int(r["id"]), "name": r["name"]}
                for r in db.execute("SELECT id,name FROM categories ORDER BY name COLLATE NOCASE")]


def _upsert_categories(rows):
    with _connect() as db:
        db.executemany(
            "INSERT INTO categories(id,name) VALUES(?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
            [(int(item["id"]), str(item["name"])) for item in rows]
        )


def _indexed(category_id):
    with _connect() as db:
        row = db.execute("SELECT indexed_at FROM categories WHERE id=?", (category_id,)).fetchone()
        return bool(row and row["indexed_at"])


def _existing_ids(category_id):
    with _connect() as db:
        return {int(row[0]) for row in db.execute(
            "SELECT detail_id FROM books WHERE category_id=?", (category_id,)
        )}


def books(category_id=None):
    with _connect() as db:
        query = ("SELECT b.category_id,b.detail_id,b.book_id,b.name,b.cover,b.is_new,"
                 "b.first_seen,c.name AS category_name FROM books b JOIN categories c "
                 "ON c.id=b.category_id")
        args = ()
        if category_id is not None:
            query += " WHERE b.category_id=?"
            args = (int(category_id),)
        query += " ORDER BY b.name COLLATE NOCASE"
        rows = [dict(row) for row in db.execute(query, args)]
    if category_id is None:
        distinct = {}
        for row in rows:
            distinct.setdefault(row["book_id"], row)
        rows = list(distinct.values())
    cutoff = int(time.time()) - 30 * 86400
    for row in rows:
        row["is_new"] = bool(row["is_new"] and row.pop("first_seen") >= cutoff)
    return rows


def _save_books(category_id, found):
    """Insert only new books; first index isn't advertised as an update."""
    now = int(time.time())
    with _connect() as db:
        row = db.execute(
            "SELECT indexed_at FROM categories WHERE id=?", (category_id,)
        ).fetchone()
        initial = not row or not int(row["indexed_at"])
        added = 0
        for item in found:
            cur = db.execute(
                "INSERT OR IGNORE INTO books"
                "(category_id,detail_id,book_id,name,cover,first_seen,is_new)"
                " VALUES(?,?,?,?,?,?,?)",
                (category_id, int(item["detail_id"]), int(item["book_id"]),
                 str(item["name"])[:240], str(item.get("cover", ""))[:1000],
                 now, 0 if initial else 1),
            )
            if cur.rowcount:
                added += 1
        db.execute("UPDATE categories SET indexed_at=? WHERE id=?", (now, category_id))
    return 0 if initial else added


def _get_meta(key):
    with _connect() as db:
        row = db.execute("SELECT value FROM catalog_meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def _set_meta(key, value):
    with _connect() as db:
        db.execute(
            "INSERT INTO catalog_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value))
        )


def status():
    with _connect() as db:
        row = db.execute(
            "SELECT COUNT(*) AS categories,"
            "SUM(CASE WHEN indexed_at>0 THEN 1 ELSE 0 END) AS indexed "
            "FROM categories"
        ).fetchone()
        total_books = db.execute("SELECT COUNT(DISTINCT book_id) FROM books").fetchone()[0]
        new_books = db.execute(
            "SELECT COUNT(DISTINCT book_id) FROM books WHERE is_new=1 AND first_seen>=?",
            (int(time.time()) - 30 * 86400,)
        ).fetchone()[0]
    return {
        **_state,
        "categories": int(row["categories"]),
        "indexed_categories": int(row["indexed"] or 0),
        "books": int(total_books),
        "new_books": int(new_books),
        "last_full_sync": int(_get_meta("last_full_sync") or 0),
        "auto_sync": AUTO_SYNC,
        "refresh_hours": REFRESH_HOURS,
    }


async def ensure_categories(force=False):
    cached = categories()
    if cached and not force:
        return cached
    if not await scraper.ensure_login():
        if cached:
            return cached
        raise RuntimeError("YCT login failed; no saved categories available")
    discovered = await scraper.get_categories()
    rows = [{"id": i, "name": name} for i, name in discovered.items()]
    await asyncio.to_thread(_upsert_categories, rows)
    return categories()


async def sync_category(category_id):
    """Fetch just the missing book details; never erase saved metadata."""
    raw = await scraper.get_books_in_category(category_id)
    existing = await asyncio.to_thread(_existing_ids, category_id)
    new = [item for item in raw if int(item["detail_id"]) not in existing]
    semaphore = asyncio.Semaphore(3)

    async def resolve(item):
        async with semaphore:
            name, book_id = await scraper.get_book_details(int(item["detail_id"]))
            await asyncio.sleep(0.12)
        if not book_id:
            return None
        return {
            "detail_id": int(item["detail_id"]), "book_id": int(book_id),
            "name": name, "cover": item.get("cover", ""),
        }

    resolved = await asyncio.gather(*(resolve(item) for item in new),
                                    return_exceptions=True)
    good = [row for row in resolved if isinstance(row, dict)]
    if new and len(good) < len(new):
        failed = len(new) - len(good)
        log.warning("Catalog category %s: %s detail entries unresolved", category_id, failed)
        # Do not stamp an initially empty category as fully indexed if
        # every detail lookup failed; a future refresh can recover it.
        if not existing and not good:
            raise RuntimeError(f"Could not resolve book details in category {category_id}")
    return await asyncio.to_thread(_save_books, category_id, good)


async def ensure_books(category_id):
    saved = books(category_id)
    if saved or _indexed(category_id):
        return saved
    if not await scraper.ensure_login():
        raise RuntimeError("YCT is unavailable and this category has not been indexed")
    # A category can be opened before the background full scan reaches it.
    # Run an on-demand first index, then all later loads stay local.
    await sync_category(category_id)
    return books(category_id)


async def _run_sync(category_id):
    _state.update(running=True, phase="connecting", current_category="",
                  processed=0, total=0, added=0, errors=[])
    try:
        if not await scraper.ensure_login():
            raise RuntimeError("YCT authentication is unavailable")
        if category_id is None:
            await asyncio.to_thread(_set_meta, "last_full_attempt", int(time.time()))
            await ensure_categories(force=True)
            targets = categories()
        else:
            await ensure_categories()
            targets = [row for row in categories() if row["id"] == category_id]
            if not targets:
                raise RuntimeError("Unknown category ID")
        _state["total"] = len(targets)
        for item in targets:
            _state["phase"] = "indexing"
            _state["current_category"] = item["name"]
            try:
                found = await sync_category(item["id"])
                _state["added"] += found
            except Exception as exc:
                log.warning("Catalog sync failed for category %s: %s", item["id"], exc)
                if len(_state["errors"]) < 15:
                    _state["errors"].append(f'{item["name"]}: {type(exc).__name__}')
            _state["processed"] += 1
            await asyncio.sleep(0.6)  # modest inter-category pacing
        if not _state["errors"]:
            if category_id is None:
                await asyncio.to_thread(_set_meta, "last_full_sync", int(time.time()))
            _state["phase"] = "complete"
        else:
            _state["phase"] = "partial"
    except Exception as exc:
        _state["phase"] = "error"
        _state["errors"].append(str(exc)[:180])
        log.exception("Catalog refresh failed")
    finally:
        _state["current_category"] = ""
        _state["running"] = False


def start_sync(category_id=None):
    global _sync_task
    if _sync_task is not None and not _sync_task.done():
        return status()
    _sync_task = asyncio.create_task(_run_sync(category_id))
    return status()


async def _periodic_sync():
    await asyncio.sleep(20)  # Never delay startup/health checks
    while True:
        try:
            # Partial errors must not trigger a costly full rescan every hour.
            last = int(_get_meta("last_full_attempt") or 0)
            if time.time() - last >= REFRESH_HOURS * 3600:
                start_sync()
                if _sync_task is not None:
                    await _sync_task
        except Exception:
            log.exception("Catalog automatic refresh failed")
        await asyncio.sleep(3600)


def start_scheduler():
    global _scheduler_task
    if AUTO_SYNC and (_scheduler_task is None or _scheduler_task.done()):
        _scheduler_task = asyncio.create_task(_periodic_sync())


async def stop_scheduler():
    global _scheduler_task
    if _scheduler_task is not None:
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass
        _scheduler_task = None
