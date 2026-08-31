# tests/test_browser_entry.py
"""Browser-entry UX: anonymous HTML navigations to cookie-realm pages land
on the login page (with a `next` bounce target) instead of raw JSON 401,
while every non-browser client keeps the structured error body. Also pins
the CLI `start` browser URL on /dashboard (not the health-JSON root).
"""
import pytest
from click.testing import CliRunner

from invincible.cli import cli

BROWSER_ACCEPT = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}


async def test_anonymous_browser_gets_login_redirect(client):
    resp = await client.get("/dashboard", headers=BROWSER_ACCEPT,
                            follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login?next=/dashboard"


async def test_anonymous_browser_redirect_replays_path(client):
    resp = await client.get("/dashboard/sessions/42", headers=BROWSER_ACCEPT,
                            follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login?next=/dashboard/sessions/42"


async def test_non_browser_keeps_json_401(client):
    # Default httpx Accept (*/*) - what API clients and SDKs send.
    resp = await client.get("/dashboard")
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"]["type"] == "auth_error"


async def test_htmx_request_gets_hx_redirect(client):
    resp = await client.get("/dashboard",
                            headers={"hx-request": "true"},
                            follow_redirects=False)
    assert resp.status_code == 401
    assert resp.headers["hx-redirect"] == "/login?next=/dashboard"


async def test_form_post_401_is_not_redirected(client):
    # A POST losing its session must not silently bounce into a GET of
    # the same path - only safe navigations redirect.
    resp = await client.post("/memories", json={"content": "x"},
                             headers={"accept": "text/html"},
                             follow_redirects=False)
    assert resp.status_code == 401
    assert "location" not in resp.headers


async def test_login_page_carries_next_target(client):
    page = await client.get("/login?next=/dashboard%2Fusage")
    assert page.status_code == 200
    assert ('name="next" value="/dashboard/usage"' in page.text)


async def test_login_page_rejects_open_redirect_next(client):
    page = await client.get("/login?next=https://evil.example.net")
    assert page.status_code == 200
    assert "evil.example.net" not in page.text


async def test_login_bounces_to_next_after_success(client):
    await client.post("/auth/register", json={
        "email": "bounce@example.com", "password": "longenough1"})
    await client.post("/auth/logout")
    resp = await client.post(
        "/auth/login",
        data={"email": "bounce@example.com", "password": "longenough1",
              "next": "/dashboard/usage"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/usage"
    # The bounced target now resolves with the fresh session cookie.
    landed = await client.get("/dashboard/usage")
    assert landed.status_code == 200


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", "http://127.0.0.1:8000/dashboard"),
    ("0.0.0.0", "http://127.0.0.1:8000/dashboard"),
    ("localhost", "http://localhost:8000/dashboard"),
])
def test_start_opens_dashboard_not_health_json(monkeypatch, tmp_path,
                                               host, expected):
    calls = []
    monkeypatch.setattr("invincible.cli.uvicorn.run", lambda *a, **k: None)

    class _FakeTimer:
        def __init__(self, delay, fn, args):
            calls.append(args)

        def start(self):
            pass

    monkeypatch.setattr("invincible.cli.threading.Timer", _FakeTimer)
    # CliRunner pipes stdout (not a tty), which the headless guard would
    # treat as headless; force the attached-terminal path under test.
    monkeypatch.setattr("invincible.cli._browser_session_available",
                        lambda: True)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["start", "--host", host,
                                      "--no-tunnel"])
    assert result.exit_code == 0
    assert calls and calls[0][0] == expected
