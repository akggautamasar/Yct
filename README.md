# YCT Downloader — Render edition

FastAPI web app + Telegram bot for browsing the YCT publication catalogue, reading pages online, building PDFs, and optionally sending them to Telegram.

## Performance-focused behavior

- **Instant repeat catalogue visits:** browser-local catalogue cache lasts up to 24 hours on the same device.
- **Server cache:** categories cache for 15 minutes; category book lists and page counts for 30 minutes. Use the web UI's refresh control to bypass cache for fresh data.
- **Local search:** search within the loaded category without another server request.
- **On-demand reader:** loads individual book pages, not the whole book, and uses a strict 8-page / 8 MiB in-memory page cache with a 15-minute TTL.
- **Light polling:** active jobs refresh quickly; idle job lists are polled less frequently.
- **Bounded upstream concurrency:** book details resolve with a small cap so browsing does not overwhelm the source website.

The server cache is intentionally process-local. Render free instances can restart at any time, and the app remains correct when the cache is empty.

## Test connectivity

Before relying on the deployment, check:

    GET https://<service>.onrender.com/healthz
    GET https://<service>.onrender.com/api/categories

Datacenter IPs can be blocked or throttled by source websites. If categories cannot load on Render, check source-site access and credentials before changing hosting.

## Architecture

- Single FastAPI service on Render.
- Web UI at `/`.
- Telegram webhook at `/tg/<secret>`.
- In-memory async job queue; jobs run one at a time to limit memory usage.
- PDF scratch/output files use `/tmp`; treat it as ephemeral.
- Completed PDFs may be sent to Telegram; Render local files are not permanent storage.

## Deploy

1. On Render, create/update the web service from this repository.
2. Set environment variables:
   - `YCT_EMAIL`, `YCT_PASSWORD`
   - `TG_TOKEN`, `TG_CHAT_ID` (optional for browser-only mode)
   - `PUBLIC_URL` (optional, for webhook registration)
   - `TG_WEBHOOK_SECRET` (generate a secret value)
   - `SITE_PASSWORD` (optional site password)
3. Deploy, then check `/healthz` and `/api/categories`.

## Render free-tier notes

- Free services sleep after a period of inactivity; the first request after sleeping can be slow. Application caching cannot remove a platform cold start.
- Server caches clear on restart. Browser caching speeds up repeat visits on the same device.
- Catalogue cache refresh is time-limited; use Refresh when the source catalogue changes.
- Reader pages are streamed on demand; do not pre-download every book onto the Render instance. It is memory-limited and local files are ephemeral.
- Large books are processed in bounded page batches to keep memory and disk usage down.
- OCR may require operating-system Tesseract language packages; the lossless PDF path does not depend on OCR.

## Local run

    pip install -r requirements.txt
    export YCT_EMAIL=... YCT_PASSWORD=...
    uvicorn main:app --reload
