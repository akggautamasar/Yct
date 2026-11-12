# AirYCT persistent Oracle book library

The library stores **book metadata**, not PDFs or downloaded pages. It indexes
YCT category IDs, names, detail IDs, reader IDs, covers, and known page counts.
PDF downloads, OCR, Telegram storage, checkpoint files, and user uploads are
unchanged.

## Oracle configuration

The recommended persistent path is outside the Git checkout:

```bash
sudo install -d -o ubuntu -g ubuntu -m 700 /home/ubuntu/.local/share/airyct
sudo cp -p /home/ubuntu/.config/yct-driver/env /home/ubuntu/.config/yct-driver/env.before-catalog
sudo tee -a /home/ubuntu/.config/yct-driver/env >/dev/null <<'EOF'
CATALOG_DB_PATH=/home/ubuntu/.local/share/airyct/catalog.sqlite3
CATALOG_AUTO_SYNC=true
CATALOG_REFRESH_HOURS=24
EOF
```

Avoid duplicate variable assignments: if any of the three keys are already
defined in the file, edit the existing values instead of appending duplicates.
Never print the complete environment file; it contains credentials.

Restart the service only when no download/export is active. A configured
automatic refresh starts **20 seconds after application startup**, then checks
whether at least the requested number of hours have passed since the last full
sync attempt. The scan is incremental: categories are visited again but only
previously unknown book detail IDs need individual metadata lookups.

A click on **Sync library** starts the same full scan immediately. Its status
and new-arrival counts appear in the library header. A newly discovered book
is labeled "NEW ARRIVAL" for 30 days, provided its category had previously
been indexed.

## Endpoints

* `GET /api/categories` — SQLite-first categories.
* `GET /api/books?cat=123` — saved category; first visit indexes it if missing.
* `GET /api/books?all=true` — all **indexed** books, deduplicated by reader ID.
* `GET /api/pages?book_id=489` — cached known page count, otherwise fetches it
  and saves the result for future views.
* `GET /api/catalog/status` — live index progress, counts, revision, recent errors.
* `POST /api/catalog/sync` with JSON `{"cat":null}` — full incremental sync.
  Send `{"cat":123}` to refresh a single category.
* `GET /api/performance` — existing downloader performance counters.

Endpoints follow AirYCT's existing site password gate. Do not expose an
unprotected instance to untrusted visitors.

## Safe deployment

```bash
cd ~/apps/Yct
git fetch origin main
git merge --no-ff origin/main -m "Merge persistent AirYCT library into Oracle"
.venv/bin/python -m py_compile catalog.py main.py jobs.py scraper.py pdfbuilder.py config.py
.venv/bin/python -m unittest discover -s tests -p 'test_catalog_cache.py' -v
```

If conflicts appear, stop and resolve them before any restart. Do **not** reset
or overwrite Oracle's custom `jobs.py`: it contains complete-PDF user Telegram
upload and checkpoint improvements not necessarily present in GitHub.

After setting the persistent catalog path and passing tests, restart only
when jobs are idle:

```bash
sudo systemctl restart yct-driver
sudo systemctl is-active yct-driver
sudo journalctl -u yct-driver -p err -n 20 --no-pager
```

Open AirYCT and select **Sync library**. The first full sync can take time
because it must resolve every new detail ID; each finished category is saved
immediately and survives restarts. Further normal visits read the local DB
rather than re-fetching remote metadata.

## Backup

Use SQLite's live backup API (copying only the .sqlite3 file while WAL writes
are active can lose recent committed entries):

```bash
sudo -u ubuntu env CATALOG_DB_PATH=/home/ubuntu/.local/share/airyct/catalog.sqlite3 \
  /home/ubuntu/apps/Yct/.venv/bin/python - <<'PY'
import catalog
import sqlite3
from pathlib import Path

destination = Path('/home/ubuntu/.local/share/airyct/catalog-backup.sqlite3')
with sqlite3.connect(catalog.DB_PATH) as source, sqlite3.connect(destination) as target:
    source.backup(target)
print('Saved catalog backup:', destination)
PY
```

The backup contains metadata only. Telegram already holds exported PDFs;
this feature does not attempt to re-upload those PDFs.

## Notes and limits

* The local library shows indexed items even when YCT is temporarily offline.
  Actual page downloads still require access to YCT.
* If an upstream category returns errors, its existing cached books are retained.
  The status endpoint reports partial scans.
* The first index is not labeled "new"; only books detected on later scans are.
* Only known page counts are cached: the full sync intentionally does not
  fetch every book's pages, keeping upstream load and scan time reasonable.
* The UI initially renders 96 book cards and offers **Show more**, keeping
  large catalogs responsive on mobile.
* Automatic sync is opt-in via `CATALOG_AUTO_SYNC` to avoid unintentionally
  scraping every category on small free-tier hosting instances.
