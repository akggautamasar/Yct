"""Durable JSON upload ledger stored using GitHub's Contents API.

Set CHECKPOINT_GITHUB_TOKEN to a fine-grained GitHub token with Contents
read/write access to the checkpoint repository. Never commit this token.
"""
import asyncio
import base64
import json
import logging
import os
import httpx

log = logging.getLogger(__name__)

REPO = os.environ.get("CHECKPOINT_GITHUB_REPO", "akggautamasar/Yct")
PATH = os.environ.get("CHECKPOINT_GITHUB_PATH", "data/telegram-uploads.json")
TOKEN = os.environ.get("CHECKPOINT_GITHUB_TOKEN", "")
_LOCK = asyncio.Lock()


def enabled():
    return bool(TOKEN)


def _url():
    return f"https://api.github.com/repos/{REPO}/contents/{PATH}"


def _headers():
    return {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


async def _read(client):
    # No "ref": read the same branch the PUT writes to (the repository default),
    # so a repo whose default branch is not "main" still works.
    response = await client.get(_url(), headers=_headers())
    if response.status_code == 404:
        return {"version": 1, "uploads": {}}, None
    response.raise_for_status()
    payload = response.json()
    if payload.get("content"):
        decoded = base64.b64decode(payload["content"]).decode("utf-8")
    else:
        # GitHub omits "content" for files above 1 MB; fetch the blob instead.
        blob = await client.get(payload["download_url"], timeout=45)
        blob.raise_for_status()
        decoded = blob.text
    try:
        ledger = json.loads(decoded)
    except ValueError as exc:
        raise ValueError("Invalid checkpoint JSON") from exc
    if not isinstance(ledger.get("uploads"), dict):
        raise ValueError("Invalid checkpoint JSON")
    return ledger, payload["sha"]


def part_key(chat_id, book_id, start, end, part_number=1):
    return f"{chat_id}:{book_id}:{start}-{end}:{part_number}"


async def has_part(key):
    """True when the part was already uploaded.

    A transient GitHub failure must not abort a multi-hour export, so an
    unreadable ledger is treated as "unknown" and the part is simply rebuilt.
    """
    if not TOKEN:
        return False
    async with _LOCK:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                ledger, _ = await _read(client)
                return key in ledger["uploads"]
        except Exception:
            log.warning("Could not read Telegram upload checkpoint; continuing", exc_info=True)
            return False


async def mark_part(key, details):
    """Persist acknowledgement; raise if storage fails to avoid silent duplicates."""
    if not TOKEN:
        return False
    async with _LOCK:
        async with httpx.AsyncClient(timeout=45) as client:
            for attempt in range(3):
                ledger, sha = await _read(client)
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
            raise RuntimeError("Could not save Telegram upload checkpoint after retries")
