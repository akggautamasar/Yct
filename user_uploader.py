"""Optional Telegram user uploader. Secrets are read from Render environment."""
import os
import asyncio
import logging

log = logging.getLogger(__name__)


def configured():
    return (os.getenv("TG_UPLOAD_MODE", "bot").lower() == "user"
            and bool(os.getenv("TG_API_ID"))
            and bool(os.getenv("TG_API_HASH"))
            and bool(os.getenv("TG_STRING_SESSION")))


async def upload(path, chat_id, caption):
    """Return True only after Telegram acknowledges the entire file."""
    if not configured():
        return False
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.errors import RPCError
    client = TelegramClient(
        StringSession(os.environ["TG_STRING_SESSION"]),
        int(os.environ["TG_API_ID"]),
        os.environ["TG_API_HASH"],
        request_retries=3,
        connection_retries=3,
        timeout=45,
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            log.warning("Telegram user session not authorized; bot fallback")
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
    except (Exception,):
        log.exception("Telegram user upload unavailable; using bot fallback")
        return False
    finally:
        await client.disconnect()
