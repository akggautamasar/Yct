"""Regression tests for bugs fixed in the downloader and API layer.

Run with: python -m pytest -q tests
"""
import asyncio
import importlib
import os
import time

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("WORK_DIR", "/tmp/yct-tests")


@pytest.fixture()
def app(monkeypatch, tmp_path):
    """Import the app with a hermetic config and a mocked upstream."""
    monkeypatch.setenv("WORK_DIR", str(tmp_path))
    monkeypatch.setenv("SITE_PASSWORD", "")
    monkeypatch.setenv("TG_TOKEN", "test-token")
    monkeypatch.setenv("TG_CHAT_ID", "12345")

    import config
    import scraper
    import jobs
    import main

    importlib.reload(config)
    importlib.reload(scraper)
    importlib.reload(jobs)
    importlib.reload(main)

    async def fake_login(force=False):
        return True

    async def fake_total_pages(book_id):
        return 5

    async def fake_access(book_id, page=1):
        return True, "ok"

    async def fake_download_pages(book_id, pages, folder, on_progress=None):
        from PIL import Image
        for page in pages:
            Image.new("RGB", (40, 60), (page % 255, 0, 0)).save(
                os.path.join(folder, f"page_{page:04d}.jpg")
            )
        if on_progress:
            on_progress(len(pages), len(pages), len(pages), 0, 0)
        return len(pages), 0, []

    monkeypatch.setattr(scraper, "login", fake_login)
    monkeypatch.setattr(scraper, "get_total_pages", fake_total_pages)
    monkeypatch.setattr(scraper, "check_page_access", fake_access)
    monkeypatch.setattr(scraper, "download_pages", fake_download_pages)

    class Bundle:
        pass

    bundle = Bundle()
    with TestClient(main.app) as client:
        bundle.client = client
        bundle.main = main
        bundle.jobs = jobs
        bundle.scraper = scraper
        bundle.config = config
        yield bundle


def _wait_for(client, jid, predicate, timeout=20.0):
    deadline = time.time() + timeout
    job = {}
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{jid}").json()
        if predicate(job):
            return job
        time.sleep(0.05)
    return job


def test_non_ascii_filename_is_downloadable(app):
    """Hindi titles used to raise UnicodeEncodeError -> HTTP 500 on download."""
    title = "कक्षा 10 विज्ञान — YCT"
    jid = app.client.post("/api/download", json={
        "book_id": 7, "title": title, "start": 1, "end": 2, "browser_download": True,
    }).json()["job_id"]
    job = _wait_for(app.client, jid, lambda j: j["status"].startswith("waiting_user"))
    assert job["status"].startswith("waiting_user"), job

    response = app.client.get(f"/api/jobs/{jid}/download")
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")
    disposition = response.headers["content-disposition"]
    assert "filename*=utf-8''" in disposition
    assert disposition.isascii()

    inline = app.client.get(f"/api/jobs/{jid}/read")
    assert inline.status_code == 200
    assert inline.headers["content-disposition"].isascii()


def test_refresh_query_really_bypasses_cache(app, monkeypatch):
    """``?refresh=true`` used to return the cached value, so Refresh did nothing."""
    state = {"n": 1}

    async def fake_categories():
        return {state["n"]: f"Category {state['n']}"}

    monkeypatch.setattr(app.scraper, "get_categories", fake_categories)

    assert app.client.get("/api/categories").json() == [{"id": 1, "name": "Category 1"}]
    state["n"] = 2
    assert app.client.get("/api/categories").json() == [{"id": 1, "name": "Category 1"}]
    assert app.client.get("/api/categories?refresh=true").json() == [
        {"id": 2, "name": "Category 2"}
    ]


def test_process_tolerates_deleted_job(app):
    """A KeyError here killed the queue worker and stalled every later job."""
    app.jobs.JOBS.pop("does-not-exist", None)
    asyncio.run(app.jobs._process("does-not-exist"))


def test_worker_survives_a_crashing_job(app, monkeypatch):
    calls = []

    async def boom(jid):
        calls.append(jid)
        raise RuntimeError("boom")

    async def record(jid):
        calls.append(jid)

    monkeypatch.setattr(app.jobs, "_process", boom)
    monkeypatch.setattr(app.jobs, "_worker_started", False)
    monkeypatch.setattr(app.jobs, "_queue", None)

    async def scenario():
        worker = asyncio.create_task(app.jobs._worker())
        app.jobs.JOBS["a"] = {"id": "a", "ts": 0}
        app.jobs.JOBS["b"] = {"id": "b", "ts": 0}
        app.jobs._q().put_nowait("a")
        app.jobs._q().put_nowait("b")
        await asyncio.sleep(0.3)
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert calls == ["a", "b"]          # the second job was still processed
    assert app.jobs.JOBS["a"]["status"] == "error"


@pytest.mark.parametrize("payload,fragment", [
    ({"book_id": 7, "start": 5, "end": 2}, "end page"),
    ({"book_id": 7, "start": 0}, "start page"),
    ({"book_id": 0, "start": 1}, "book_id"),
    ({"book_id": 7, "start": 1, "end": 99, "total_pages": 5}, "last page"),
])
def test_download_rejects_invalid_ranges(app, payload, fragment):
    response = app.client.post("/api/download",
                               json={"title": "T", "browser_download": True, **payload})
    assert response.status_code == 400
    assert fragment in response.json()["detail"]


def test_download_requires_a_destination(app):
    response = app.client.post("/api/download",
                               json={"book_id": 7, "start": 1, "title": "T"})
    assert response.status_code == 400


@pytest.mark.parametrize("secret", ["hook", "short", "definitely-not-it"])
def test_webhook_rejects_short_or_wrong_secret(app, secret):
    assert app.client.post(f"/tg/{secret}", json={}).status_code == 403


def test_webhook_rejects_invalid_payload(app, monkeypatch):
    monkeypatch.setattr(app.config, "TG_WEBHOOK_SECRET", "s3cret-value")
    response = app.client.post("/tg/s3cret-value", content=b"nope",
                               headers={"content-type": "application/json"})
    assert response.status_code == 400


def test_webhook_accepts_configured_secret(app, monkeypatch):
    monkeypatch.setattr(app.config, "TG_WEBHOOK_SECRET", "s3cret-value")
    seen = []

    async def fake_handle(update):
        seen.append(update)

    monkeypatch.setattr(app.main, "handle_update", fake_handle)

    async def scenario():
        response = app.client.post("/tg/s3cret-value", json={"update_id": 1})
        await asyncio.sleep(0.05)
        return response

    response = asyncio.run(scenario())
    assert response.status_code == 200
    assert seen == [{"update_id": 1}]


def test_pdf_parts_all_confirm_and_finish(app, monkeypatch):
    """Multi-part browser download must finish cleanly with no leftover files."""
    monkeypatch.setattr(app.config, "CHUNK_PAGES", 2)
    jid = app.client.post("/api/download", json={
        "book_id": 7, "title": "Parts", "start": 1, "end": 5, "browser_download": True,
    }).json()["job_id"]

    job = _wait_for(app.client, jid, lambda j: j["status"].startswith("waiting_user"))
    assert job["total_parts"] == 3

    for confirmed in range(1, 4):
        assert app.client.post(f"/api/jobs/{jid}/confirm").status_code == 200
        job = _wait_for(app.client, jid,
                        lambda j, c=confirmed: j["parts_done"] >= c
                        or j["status"] in ("done", "error"))
    job = _wait_for(app.client, jid, lambda j: j["status"] in ("done", "error"))
    assert job["status"] == "done", job
    assert job["parts_done"] == 3
    assert os.listdir(app.jobs.DOWNLOAD_DIR) == []


def test_category_jobs_are_pruned(app):
    job = {"status": "done"}
    for index in range(60):
        app.jobs.BATCH_JOBS[f"batch{index}"] = dict(job)
    app.jobs._prune_category_jobs()
    assert len(app.jobs.BATCH_JOBS) <= app.jobs._BATCH_KEEP


def test_page_decode_helpers(app):
    png = b"\x89PNG\r\n\x1a\n" + b"body"
    assert app.scraper._decode_page(png, "") == (png, "png")
    jpg = b"\xff\xd8\xff\xe0" + b"body"
    assert app.scraper._decode_page(jpg, "") == (jpg, "jpg")
    assert app.scraper._decode_page(b"<html></html>", "<html></html>") is None


def test_disposition_header_is_ascii_safe(app):
    header = app.main._disposition("attachment", 'कक्षा 10 "विज्ञान".pdf')
    header.encode("latin-1")            # must not raise
    assert "filename*=utf-8''" in header
    assert 'filename="' in header
