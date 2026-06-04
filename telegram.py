"""Telegram — send messages and PDF files to the storage channel.

Uses httpx async. Bot uploads cap at ~50MB, so files are pre-split by pdfbuilder.
"""
import os
import asyncio
import httpx

import config

_API = f"https://api.telegram.org/bot{config.TG_TOKEN}"


async def send_message(text: str, chat_id: str | None = None) -> bool:
    chat = chat_id or config.TG_CHAT_ID
    async with httpx.AsyncClient(timeout=30) as c:
        for _ in range(3):
            try:
                r = await c.post(f"{_API}/sendMessage",
                                 data={"chat_id": chat, "text": text,
                                       "parse_mode": "HTML"})
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(2)
    return False


async def send_document(path: str, caption: str = "",
                        chat_id: str | None = None) -> bool:
    """Send a file with retries. Caller ensures size <= Telegram limit."""
    chat = chat_id or config.TG_CHAT_ID
    if not os.path.exists(path):
        return False
    attempt = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(600)) as c:
        while attempt < 8:
            attempt += 1
            try:
                with open(path, "rb") as f:
                    r = await c.post(
                        f"{_API}/sendDocument",
                        data={"chat_id": chat, "caption": caption,
                              "parse_mode": "HTML"},
                        files={"document": (os.path.basename(path), f)},
                    )
                if r.status_code == 200:
                    return True
                if r.status_code in (400, 403, 413):
                    return False
            except Exception:
                pass
            await asyncio.sleep(min(5 * attempt, 60))
    return False


async def send_photo(url_or_path: str, caption: str = "",
                     chat_id: str | None = None) -> bool:
    chat = chat_id or config.TG_CHAT_ID
    async with httpx.AsyncClient(timeout=60) as c:
        try:
            if url_or_path.startswith("http"):
                r = await c.post(f"{_API}/sendPhoto",
                                 data={"chat_id": chat, "photo": url_or_path,
                                       "caption": caption, "parse_mode": "HTML"})
            else:
                with open(url_or_path, "rb") as f:
                    r = await c.post(f"{_API}/sendPhoto",
                                     data={"chat_id": chat, "caption": caption,
                                           "parse_mode": "HTML"},
                                     files={"photo": f})
            return r.status_code == 200
        except Exception:
            return False


async def set_webhook() -> bool:
    if not config.PUBLIC_URL:
        return False
    url = f"{config.PUBLIC_URL}/tg/{config.TG_WEBHOOK_SECRET}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{_API}/setWebhook", data={"url": url})
        return r.status_code == 200
