# tests/test_accounts_ui.py
"""Phase 3 browser pages (Jinja2 templates, form posts).

Smoke coverage for /login, /register, /account, and the device approval
page - including the form-mode responses (redirects + rendered errors)
that only trigger on urlencoded posts.
"""

import pytest

from tests.conftest import register_account

PASSWORD = "longenough1"
FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}


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


# --- first-run funnel: register honors the `next` bounce target ----------------
# A brand-new user's first touch is the device-pairing page the CLI opens
# (anonymous 401 -> /login?next=...). Registration must bounce back there
# instead of stranding them on the dashboard while the CLI polls.


async def test_register_page_carries_next_target(client):
    page = await client.get("/register?next=/auth/devices/ABCD1234")
    assert page.status_code == 200
    assert 'name="next" value="/auth/devices/ABCD1234"' in page.text


async def test_register_page_rejects_open_redirect_next(client):
    page = await client.get("/register?next=https://evil.example.net")
    assert page.status_code == 200
    assert "evil.example.net" not in page.text


async def test_form_register_bounces_to_next(client):
    made = await client.post(
        "/auth/register",
        content=(f"email=funnel@example.com&password={PASSWORD}"
                 "&next=/auth/devices/ABCD1234"),
        headers=FORM_HEADERS,
        follow_redirects=False,
    )
    assert made.status_code == 303
    assert made.headers["location"] == "/auth/devices/ABCD1234"


@pytest.mark.parametrize("evil", ["https://evil.example.net",
                                  "//evil.example.net"])
async def test_form_register_ignores_unsafe_next(client, evil):
    made = await client.post(
        "/auth/register",
        content=(f"email=unsafe@example.com&password={PASSWORD}"
                 f"&next={evil}"),
        headers=FORM_HEADERS,
        follow_redirects=False,
    )
    assert made.status_code == 303
    assert made.headers["location"] == "/account"


async def test_form_register_error_preserves_next(client):
    await client.post(
        "/auth/register",
        content=f"email=keep@example.com&password={PASSWORD}",
        headers=FORM_HEADERS, follow_redirects=False)
    dup = await client.post(
        "/auth/register",
        content=(f"email=keep@example.com&password={PASSWORD}"
                 "&next=/auth/devices/ABCD1234"),
        headers=FORM_HEADERS, follow_redirects=False)
    assert dup.status_code == 200
    assert 'name="next" value="/auth/devices/ABCD1234"' in dup.text


async def test_login_page_register_link_carries_next(client):
    page = await client.get("/login?next=/auth/devices/ABCD1234")
    assert page.status_code == 200
    # Jinja's urlencode keeps '/' safe, so the target rides along unescaped
    # - a valid query value either way.
    assert 'href="/register?next=/auth/devices/ABCD1234"' in page.text


async def test_login_error_rerender_preserves_next(client):
    await register_account(client, "typo@example.com")
    bad = await client.post(
        "/auth/login",
        content="email=typo@example.com&password=wrongpassword"
                "&next=/auth/devices/ABCD1234",
        headers=FORM_HEADERS,
        follow_redirects=False,
    )
    assert bad.status_code == 200
    assert 'name="next" value="/auth/devices/ABCD1234"' in bad.text


async def test_new_user_pairing_funnel_end_to_end(client):
    """The whole first-run browser journey: CLI starts pairing, a brand-new
    user follows the 401 bounce, registers, lands on the approval page,
    approves, and the CLI's single token poll mints the API key."""
    started = (await client.post("/auth/device/code")).json()
    code = started["user_code"]
    device_code = started["device_code"]

    # 1. Fresh browser on the approval URL: bounced to login with target.
    redirect = await client.get(
        f"/auth/devices/{code}",
        headers={"accept": "text/html"}, follow_redirects=False)
    assert redirect.status_code == 302
    assert redirect.headers["location"] == f"/login?next=/auth/devices/{code}"

    # 2. The login page offers registration without losing the target.
    login_page = await client.get(f"/login?next=/auth/devices/{code}")
    assert f'href="/register?next=/auth/devices/{code}"' in login_page.text

    # 3. Register through the form, carrying the target.
    made = await client.post(
        "/auth/register",
        content=(f"email=new@device.example&password={PASSWORD}"
                 f"&next=/auth/devices/{code}"),
        headers=FORM_HEADERS,
        follow_redirects=False,
    )
    assert made.status_code == 303
    assert made.headers["location"] == f"/auth/devices/{code}"

    # 4. The approval page renders for the fresh session.
    page = await client.get(f"/auth/devices/{code}")
    assert page.status_code == 200
    assert code in page.text

    # 5. Approve; the CLI's token poll now mints the key.
    approved = await client.post(f"/auth/devices/{code}/approve")
    assert "Device approved" in approved.text
    token = await client.post("/auth/device/token",
                              data={"device_code": device_code})
    assert token.status_code == 200
    assert token.json()["access_token"]


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
