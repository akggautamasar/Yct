# YCT Downloader — Render edition

FastAPI web app + Telegram bot for browsing the YCT publication catalogue, reading pages online, building PDFs, and optionally sending them to Telegram.

## Performance-focused behavior

- **Instant repeat catalogue visits:** browser-local catalogue cache lasts up to 24 hours on the same device.
- **Server cache:** categories cache for 15 minutes; category book lists and page counts for 30 minutes. Use the web UI's refresh control to bypass cache for fresh data.
- **Local search:** search within the loaded category without another server request.
- **On-demand reader:** loads individual book pages, not the whole book, and uses a strict 8-page / 8 MiB in-memory page cache with a 15-minute TTL.
- **Light polling:** active jobs refresh quickly; idle job lists are polled less frequently.
- **Bounded upstream concurrency:** book details resolve with a small cap so browsing does not overwhelm the source website.
- **Telegram category export:** sends one book/page-batch PDF at a time to Telegram, then immediately deletes temporary files. It does not create a category-wide ZIP or accumulate a library on Render.

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
- In-memory async job queue; normal downloads and category exports share a resource lock so only one PDF-building task runs at a time.
- Category export streams PDF parts directly to the configured Telegram chat/channel, with page batches capped at 40 pages. Render stores only the current temporary batch; completed files live in Telegram.
- PDF scratch/output files use `/tmp`; treat it as ephemeral.
- Completed PDFs may be sent to Telegram; Render local files are not permanent storage.

## Persistent Telegram export checkpoints

Render free-instance files are ephemeral. For durable part-level JSON checkpoints,
create a fine-grained GitHub personal access token restricted to this repository
with **Contents: Read and write**, and add it in Render Environment as
`CHECKPOINT_GITHUB_TOKEN`. Optional: `CHECKPOINT_GITHUB_REPO` (defaults to
`akggautamasar/Yct`), `CHECKPOINT_GITHUB_PATH` (defaults to
`data/telegram-uploads.json`).

Each successfully sent PDF part is written to the repository JSON file.
Future exports skip parts with the same Telegram chat, book ID, page range,
and split-part number. The first interrupted export predates this feature:
manually resume RRB from book 92, page 161. Historical Telegram messages
cannot be reconstructed through the Bot API.

**Caveats:** Without the token, export continues without durable checkpointing.
A crash after Telegram accepts a file but before the JSON commit can cause
a duplicate on retry. GitHub commits per part can be slow and the JSON ledger
will grow over time; a dedicated database is preferable at larger scale.
Do not place the token in source control.

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
- Category export requires `TG_TOKEN` and `TG_CHAT_ID`. It uploads each page-sized PDF batch directly to Telegram and removes local files after upload. An in-progress export cannot be guaranteed to survive a Render restart: job state is in memory, but already-uploaded Telegram files remain. Keep the page open when possible so status polling keeps the service active.
- Large books are processed in bounded page batches to keep memory and disk usage down.
- OCR may require operating-system Tesseract language packages; the lossless PDF path does not depend on OCR.

## Local run

    pip install -r requirements.txt
    export YCT_EMAIL=... YCT_PASSWORD=...
    uvicorn main:app --reload
