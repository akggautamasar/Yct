"""Durable Telegram upload checkpoint ledger for Oracle and Render.

Oracle: set CHECKPOINT_SQLITE_PATH to a persistent disk path.
Render: optional CHECKPOINT_GITHUB_TOKEN keeps legacy remote checkpoints.
Both can be enabled during migration; remote entries are consulted before upload.
"""
import asyncio
import base64
import json
import logging
import os
import sqlite3
import time
import httpx

log = logging.getLogger(__name__)
REPO = os.getenv("CHECKPOINT_GITHUB_REPO", "akggautamasar/Yct")
PATH = os.getenv("CHECKPOINT_GITHUB_PATH", "data/telegram-uploads.json")
TOKEN = os.getenv("CHECKPOINT_GITHUB_TOKEN", "")
DB = os.getenv("CHECKPOINT_SQLITE_PATH", "")
_LOCK = asyncio.Lock()

def _connect():
    if not DB:
        return None
    os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)
    conn = sqlite3.connect(DB, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("CREATE TABLE IF NOT EXISTS uploads (key TEXT PRIMARY KEY, details TEXT NOT NULL, created_at INTEGER NOT NULL)")
    conn.commit()
    return conn

def enabled():
    return bool(DB or TOKEN)

def part_key(chat_id, book_id, start, end, part_number=1):
    return f"{chat_id}:{book_id}:{start}-{end}:{part_number}"

def _url():
    return f"https://api.github.com/repos/{REPO}/contents/{PATH}"

def _headers():
    return {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}

async def _remote_read(client):
    response = await client.get(_url(), headers=_headers())
    if response.status_code == 404:
        return {"version": 1, "uploads": {}}, None
    response.raise_for_status()
    payload = response.json()
    if payload.get("content"):
        decoded = base64.b64decode(payload["content"]).decode("utf-8")
    else:
        blob = await client.get(payload["download_url"], timeout=45)
        blob.raise_for_status()
        decoded = blob.text
    ledger = json.loads(decoded)
    if not isinstance(ledger.get("uploads"), dict):
        raise ValueError("Invalid checkpoint JSON")
    return ledger, payload["sha"]

async def has_part(key):
    async with _LOCK:
        if DB:
            conn = await asyncio.to_thread(_connect)
            try:
                if conn.execute("SELECT 1 FROM uploads WHERE key=?", (key,)).fetchone():
                    return True
            finally:
                conn.close()
        if TOKEN:
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    ledger, _ = await _remote_read(client)
                    if key in ledger["uploads"]:
                        return True
            except Exception:
                # Do not silently assume a remote upload never occurred.
                log.exception("Remote checkpoint lookup failed")
                raise RuntimeError("Cannot verify prior Telegram uploads; retry later")
        return False

async def mark_part(key, details):
    async with _LOCK:
        if DB:
            conn = await asyncio.to_thread(_connect)
            try:
                conn.execute("INSERT OR IGNORE INTO uploads VALUES (?, ?, ?)",
                             (key, json.dumps(details, ensure_ascii=False), int(time.time())))
                conn.commit()
            finally:
                conn.close()
        if TOKEN:
            try:
                async with httpx.AsyncClient(timeout=45) as client:
                    for attempt in range(3):
                        ledger, sha = await _remote_read(client)
                        if key in ledger["uploads"]:
                            return True
                        ledger["uploads"][key] = details
                        body = {"message": "Record Telegram PDF upload checkpoint",
                                "content": base64.b64encode(
                                    (json.dumps(ledger, indent=2, ensure_ascii=False) + "\n").encode()
                                ).decode()}
                        if sha:
                            body["sha"] = sha
                        response = await client.put(_url(), headers=_headers(), json=body)
                        if response.status_code in (200, 201):
                            return True
                        if response.status_code not in (409, 422):
                            response.raise_for_status()
                        await asyncio.sleep(1 + attempt)
                    raise RuntimeError("Remote checkpoint commit failed")
            except Exception:
                if not DB:
                    raise
                log.exception("GitHub checkpoint sync failed; local SQLite checkpoint saved")
        return bool(DB or TOKEN)
