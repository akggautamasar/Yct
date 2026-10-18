"""Telegram — send messages and PDF files to the storage channel.

Uses httpx async. Bot uploads cap at ~50MB, so files are pre-split by pdfbuilder.
"""
import os
import asyncio
import httpx

import config

_API = f"https://api.telegram.org/bot{config.TG_TOKEN}"


async def send_message(text: str, chat_id: str | None = None,
                       buttons: list | None = None) -> dict | None:
    """Send a message, optionally with an inline keyboard.
    buttons = list of rows; each row a list of (label, callback_data) tuples.
    Returns the Telegram message object on success."""
    chat = chat_id or config.TG_CHAT_ID
    data = {"chat_id": chat, "text": text, "parse_mode": "HTML"}
    if buttons:
        data["reply_markup"] = _kb(buttons)
    async with httpx.AsyncClient(timeout=30) as c:
        for _ in range(3):
            try:
                r = await c.post(f"{_API}/sendMessage", json=data)
                if r.status_code == 200:
                    return r.json().get("result")
            except Exception:
                pass
            await asyncio.sleep(2)
    return None


def _kb(buttons: list) -> dict:
    """Build an inline_keyboard markup from rows of (label, callback_data)."""
    rows = []
    for row in buttons:
        rows.append([{"text": label, "callback_data": data} for label, data in row])
    return {"inline_keyboard": rows}


async def edit_message(chat_id, message_id, text: str,
                       buttons: list | None = None) -> bool:
    data = {"chat_id": chat_id, "message_id": message_id,
            "text": text, "parse_mode": "HTML"}
    if buttons is not None:
        data["reply_markup"] = _kb(buttons)
    async with httpx.AsyncClient(timeout=30) as c:
        try:
            r = await c.post(f"{_API}/editMessageText", json=data)
            return r.status_code == 200
        except Exception:
            return False


async def answer_callback(callback_id: str, text: str = "") -> bool:
    async with httpx.AsyncClient(timeout=15) as c:
        try:
            r = await c.post(f"{_API}/answerCallbackQuery",
                             json={"callback_query_id": callback_id, "text": text})
            return r.status_code == 200
        except Exception:
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
                if r.status_code == 200 and r.json().get("ok") is True:
                    return True
                if r.status_code == 429:
                    try:
                        retry_after = int(r.json().get("parameters", {}).get("retry_after", 5))
                    except (ValueError, TypeError):
                        retry_after = 5
                    await asyncio.sleep(min(retry_after, 120))
                    continue
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
