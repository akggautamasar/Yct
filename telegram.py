"""Telegram — send messages and PDF files to the storage channel.

Uses httpx async. Bot uploads cap at ~50MB, so files are pre-split by pdfbuilder.
"""
import os
import asyncio
import httpx

import config

def _api() -> str:
    """Resolve the API root at call time so the token is read from config."""
    return f"https://api.telegram.org/bot{config.TG_TOKEN}"


_API = _api()   # kept for backwards compatibility with external imports


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
        for attempt in range(3):
            try:
                r = await c.post(f"{_api()}/sendMessage", json=data)
                if r.status_code == 200:
                    return r.json().get("result")
                if r.status_code == 400 and "parse_mode" in data:
                    # Malformed HTML in a caption must not swallow the message.
                    data = {k: v for k, v in data.items() if k != "parse_mode"}
                    continue
                if 400 <= r.status_code < 500:
                    return None
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
            r = await c.post(f"{_api()}/editMessageText", json=data)
            if r.status_code == 400 and "parse_mode" in data:
                # Same message text without HTML, e.g. an unescaped '<'.
                data.pop("parse_mode", None)
                r = await c.post(f"{_api()}/editMessageText", json=data)
            return r.status_code == 200
        except Exception:
            return False


async def answer_callback(callback_id: str, text: str = "") -> bool:
    async with httpx.AsyncClient(timeout=15) as c:
        try:
            r = await c.post(f"{_api()}/answerCallbackQuery",
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
                        f"{_api()}/sendDocument",
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
                # Any other 4xx means the request itself is wrong (unknown chat,
                # bad HTML caption, file too large...). Retrying eight times just
                # stalls the export for minutes, so fail fast.
                if 400 <= r.status_code < 500:
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
                r = await c.post(f"{_api()}/sendPhoto",
                                 data={"chat_id": chat, "photo": url_or_path,
                                       "caption": caption, "parse_mode": "HTML"})
            else:
                with open(url_or_path, "rb") as f:
                    r = await c.post(f"{_api()}/sendPhoto",
                                     data={"chat_id": chat, "caption": caption,
                                           "parse_mode": "HTML"},
                                     files={"photo": f})
            return r.status_code == 200
        except Exception:
            return False


async def set_webhook() -> bool:
    if not config.PUBLIC_URL or not config.TG_TOKEN:
        return False
    if len(config.TG_WEBHOOK_SECRET) < 8:
        # The webhook route rejects short secrets, so registering one would
        # leave the bot silently dead.
        return False
    url = f"{config.PUBLIC_URL}/tg/{config.TG_WEBHOOK_SECRET}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{_api()}/setWebhook", data={"url": url})
        return r.status_code == 200
