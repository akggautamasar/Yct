# YCT Downloader — Render edition

Web UI + Telegram bot that logs into yctpublication.com, downloads ebook pages,
builds a PDF (max-quality lossless, or searchable OCR), and pushes it to a
Telegram channel (used as storage, since Render disk is ephemeral).

## ⚠️ Test connectivity FIRST

Datacenter IPs are often blocked/throttled by sites that home/mobile IPs reach
fine. Before trusting the whole stack, deploy and hit:

    GET  https://<service>.onrender.com/healthz      -> env configured?
    GET  https://<service>.onrender.com/api/categories  -> can it log in + scrape?

If `/api/categories` returns categories, Render's IP works. If it 502s with
"login failed" or returns empty, the site is blocking the datacenter IP and
Termux remains the better host.

## Architecture

- **Single FastAPI process** on Render free tier.
- **Web UI** (`/`) to browse categories/books and trigger downloads.
- **Telegram bot** via webhook at `/tg/<secret>` — `/cats`, `/books`, `/get`, `/status`.
- **In-memory async job queue**, one book at a time (512MB RAM limit).
- **PDFs → Telegram channel**; `/tmp` scratch cleaned after each job.

## Deploy

1. Push this folder to a GitHub repo.
2. On Render: New → Blueprint → pick the repo (uses `render.yaml`).
3. Set env vars (Render dashboard → Environment):
   - `YCT_EMAIL`, `YCT_PASSWORD`
   - `TG_TOKEN` (from @BotFather)
   - `TG_CHAT_ID` (your channel/chat id; bot must be admin of the channel)
   - `PUBLIC_URL` — after first deploy, set to `https://<service>.onrender.com`
     then redeploy so the Telegram webhook registers.
4. Visit the URL → web UI. Or message the bot `/help`.

## Env vars

| var | required | notes |
|-----|----------|-------|
| YCT_EMAIL / YCT_PASSWORD | yes | site login |
| TG_TOKEN / TG_CHAT_ID | yes | bot + storage channel |
| PUBLIC_URL | for bot | enables webhook registration |
| TG_WEBHOOK_SECRET | auto | secret path segment |
| OCR_LANG | no | default `hin+eng` |
| MAX_WORKERS | no | concurrent page fetches (default 6) |
| TG_MAX_MB | no | split threshold (default 45) |

## Notes / limits

- Free tier sleeps after 15 min idle → first request is slow (cold start).
- Long books may take a while; the bot/UI shows live progress.
- Jobs live in memory; a restart loses queue state (finished PDFs are safe in Telegram).
- OCR needs the `tesseract-ocr-hin` apt package (installed in `buildCommand`).
  If apt install is not permitted on your plan, OCR auto-falls back to the
  max-quality lossless PDF.

## Local run

    pip install -r requirements.txt
    export YCT_EMAIL=... YCT_PASSWORD=... TG_TOKEN=... TG_CHAT_ID=...
    uvicorn main:app --reload
