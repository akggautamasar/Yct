"""Configuration — all secrets/settings come from environment variables on Render."""
import os

# --- yctpublication.com credentials ---
EMAIL    = os.environ.get("YCT_EMAIL", "")
PASSWORD = os.environ.get("YCT_PASSWORD", "")

# --- Telegram (storage + bot) ---
TG_TOKEN   = os.environ.get("TG_TOKEN", "")        # bot token from @BotFather
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")      # channel/chat where PDFs are sent
# Secret path segment for the webhook so randoms can't hit it
TG_WEBHOOK_SECRET = os.environ.get("TG_WEBHOOK_SECRET", "hook")
# Public URL of this Render service, e.g. https://yct-xyz.onrender.com
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

# --- Site-wide password gate (optional) ---
SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "")

# --- Tuning ---
MAX_WORKERS = max(2, min(12, int(os.environ.get("MAX_WORKERS", "8"))))   # concurrent page fetches
PAGE_RETRY  = int(os.environ.get("PAGE_RETRY", "5"))
REQ_DELAY   = float(os.environ.get("REQ_DELAY", "0.15"))
TG_MAX_MB   = int(os.environ.get("TG_MAX_MB", "45"))    # Telegram bot upload cap ~50MB
DO_OCR_DEFAULT = os.environ.get("DO_OCR", "false").lower() == "true"
OCR_LANG    = os.environ.get("OCR_LANG", "hin+eng")
# Process big books in page-batches so memory stays flat on the 512MB tier.
# Each batch is downloaded, built into its own PDF, uploaded, then freed.
CHUNK_PAGES = int(os.environ.get("CHUNK_PAGES", "40"))

# --- Constants ---
BASE_URL = "https://yctpublication.com"
WORK_DIR = os.environ.get("WORK_DIR", "/tmp/yct")     # ephemeral scratch
HEADERS  = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Origin": BASE_URL, "X-Requested-With": "XMLHttpRequest",
    "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
}

os.makedirs(WORK_DIR, exist_ok=True)


def validate():
    missing = [k for k, v in {
        "YCT_EMAIL": EMAIL, "YCT_PASSWORD": PASSWORD,
        "TG_TOKEN": TG_TOKEN, "TG_CHAT_ID": TG_CHAT_ID,
    }.items() if not v]
    return missing
