"""Regression tests for the Render-compatible YCT login handshake.

Run with: python -m pytest -q tests/test_login_compat.py
Requires pytest and pytest-asyncio in the development environment.
"""
import httpx
import pytest
import scraper


@pytest.fixture(autouse=True)
def reset_login_state(monkeypatch):
    monkeypatch.setattr(scraper.config, "EMAIL", "test@example.invalid")
    monkeypatch.setattr(scraper.config, "PASSWORD", "testing-only")
    monkeypatch.setattr(scraper, "_login_complete", False)
    monkeypatch.setattr(scraper, "_session_cookie", None)


@pytest.mark.asyncio
async def test_redirect_to_ebook_with_login_modal_and_no_named_cookie(monkeypatch):
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        if req.method == "POST":
            return httpx.Response(302, headers={"Location": "/ebook"})
        return httpx.Response(
            200, text='<html><form><input type="password"></form></html>'
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        async def get_client():
            return client
        monkeypatch.setattr(scraper, "get_client", get_client)
        assert await scraper.login() is True
        assert scraper._session_cookie is None
        assert await scraper.login() is True
        assert calls == [
            ("GET", "/login"),
            ("POST", "/LoginController/login1"),
            ("GET", "/ebook"),
        ]


@pytest.mark.asyncio
async def test_explicit_redirect_back_to_login_is_rejected(monkeypatch):
    def handler(req):
        if req.method == "POST":
            return httpx.Response(302, headers={"Location": "/login"})
        return httpx.Response(200, text='<html><form><input type="password"></form></html>')

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        async def get_client():
            return client
        monkeypatch.setattr(scraper, "get_client", get_client)
        assert await scraper.login() is False


@pytest.mark.asyncio
async def test_explicit_json_login_failure_is_rejected(monkeypatch):
    def handler(req):
        if req.method == "POST":
            return httpx.Response(200, json={"status": False})
        return httpx.Response(200, text="Login form")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        async def get_client():
            return client
        monkeypatch.setattr(scraper, "get_client", get_client)
        assert await scraper.login() is False


@pytest.mark.asyncio
async def test_expired_session_can_force_login_refresh(monkeypatch):
    calls = []

    def handler(req):
        calls.append(req.method)
        if req.method == "POST":
            return httpx.Response(302, headers={"Location": "/ebook"})
        return httpx.Response(200, text="<html>Home</html>")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        async def get_client():
            return client
        monkeypatch.setattr(scraper, "get_client", get_client)
        assert await scraper.login()
        assert await scraper.login(force=True)
        assert calls.count("POST") == 2
