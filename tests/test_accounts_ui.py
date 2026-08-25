# tests/test_accounts_ui.py
"""Phase 3 browser pages (Jinja2 templates, form posts).

Smoke coverage for /login, /register, /account, and the device approval
page - including the form-mode responses (redirects + rendered errors)
that only trigger on urlencoded posts.
"""

from tests.conftest import register_account

PASSWORD = "longenough1"


async def test_login_page_renders_without_github(client, monkeypatch):
    monkeypatch.delenv("INVINCIBLE_GITHUB_CLIENT_ID", raising=False)
    page = await client.get("/login")
    assert page.status_code == 200
    assert 'action="/auth/login"' in page.text
    assert "Sign in with GitHub" not in page.text


async def test_login_page_shows_github_when_configured(client, monkeypatch):
    monkeypatch.setenv("INVINCIBLE_GITHUB_CLIENT_ID", "cid")
    monkeypatch.setenv("INVINCIBLE_GITHUB_CLIENT_SECRET", "shh")
    page = await client.get("/login")
    assert "/auth/github/login" in page.text


async def test_register_page_renders(client):
    page = await client.get("/register")
    assert page.status_code == 200
    assert 'action="/auth/register"' in page.text


async def test_form_register_redirects_to_account(client):
    made = await client.post(
        "/auth/register",
        content=f"email=form@example.com&password={PASSWORD}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert made.status_code == 303
    assert made.headers["location"] == "/account"
    # session cookie landed; the account page renders for us
    account = await client.get("/account")
    assert account.status_code == 200
    assert "form@example.com" in account.text


async def test_form_register_error_rerenders(client):
    form_headers = {"Content-Type": "application/x-www-form-urlencoded"}
    first = await client.post(
        "/auth/register",
        content=f"email=taken@example.com&password={PASSWORD}",
        headers=form_headers,
        follow_redirects=False,
    )
    assert first.status_code == 303
    dup = await client.post(
        "/auth/register",
        content=f"email=taken@example.com&password={PASSWORD}",
        headers=form_headers,
    )
    assert dup.status_code == 200
    assert "already exists" in dup.text


async def test_form_login_bad_credentials_rerenders_with_error(client):
    await register_account(client, "ui@example.com")
    fresh_cookieless_headers = {"Content-Type":
                                "application/x-www-form-urlencoded"}
    bad = await client.post(
        "/auth/login",
        content="email=ui@example.com&password=wrongpassword",
        headers=fresh_cookieless_headers,
    )
    assert bad.status_code == 200
    assert "Invalid email or password" in bad.text


async def test_account_page_requires_session(client):
    anon = await client.get("/account")
    assert anon.status_code == 401


async def test_device_approval_page_renders_template(client, monkeypatch):
    monkeypatch.setattr("invincible.endpoints.accounts.DEFAULT_POLL_INTERVAL", 0)
    await register_account(client, "paged@device.example")
    payload = (await client.post("/auth/device/code")).json()
    page = await client.get(f"/auth/devices/{payload['user_code']}")
    assert page.status_code == 200
    assert payload["user_code"] in page.text
    assert f"/auth/devices/{payload['user_code']}/approve" in page.text


async def test_unknown_device_code_gets_result_page(client):
    await register_account(client, "lost@device.example")
    page = await client.get("/auth/devices/ZZZZZZZZ")
    assert page.status_code == 404
    assert "Unknown or expired code" in page.text
