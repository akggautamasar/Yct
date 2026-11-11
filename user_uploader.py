"""Optional Telegram user uploader; secrets remain on the server."""
import os
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def _session():
    path = os.getenv("TG_STRING_SESSION_FILE", "")
    if path:
        file = Path(path).expanduser()
        if file.is_file():
            return file.read_text(encoding="utf-8").strip()
    return os.getenv("TG_STRING_SESSION", "").strip()


def configured():
    return (os.getenv("TG_UPLOAD_MODE", "bot").lower() == "user"
            and bool(os.getenv("TG_API_ID"))
            and bool(os.getenv("TG_API_HASH"))
            and bool(_session()))


async def upload(path, chat_id, caption):
    """Return True only after Telegram acknowledges the entire file."""
    if not configured():
        return False
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    client = TelegramClient(
        StringSession(_session()),
        int(os.environ["TG_API_ID"]),
        os.environ["TG_API_HASH"],
        request_retries=3,
        connection_retries=3,
        timeout=45,
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            log.warning("Telegram user session not authorized")
            return False
        # Telethon accepts numeric peer IDs; account must have access to the target chat.
        target = int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id
        try:
            entity = await client.get_input_entity(target)
        except (ValueError, TypeError):
            # A fresh StringSession has no entity cache. Populate it from dialogs.
            await client.get_dialogs(limit=200)
            entity = await client.get_input_entity(target)
        await client.send_file(entity, path, caption=caption, parse_mode="html",
                               force_document=True)
        return True
    except Exception:
        log.exception("Telegram user upload failed")
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
