# tests/test_anonymous_rate_limits.py
"""MEDIUM-4 (2026-09-07 audit): per-IP fixed-window caps on the
anonymous write endpoints.

Both POST /oauth/register and POST /auth/device/code are open by design
(consent is the gate, not registration), but each call is a row write.
One client IP hammering either endpoint must hit a 429 well before the
table grows unbounded, while the login scopes keep working - the caps
are deliberately scoped ("client-register" / "device-code") so no
anonymous flood can lock a real user out of anything.

Gates:
- N rapid /oauth/register calls from one IP -> 429 with the project's
  OAuth error shape, and no client row for the refused request;
- under the cap, registrations still succeed;
- /auth/device/code: same shape - over the cap 429, under it 200;
- different IP is unaffected (the limiter is per-IP keyed);
- a full device-code budget does NOT lock out /auth/login (scope
  separation - the anonymous cap never breaks the login surface);
- invalid bodies still count toward the cap (each attempt is a
  potential row write).
"""
import httpx
from sqlalchemy import text

from invincible.endpoints.accounts import DEVICE_CODE_MAX_ATTEMPTS
from invincible.endpoints.oauth import REGISTER_MAX_ATTEMPTS
from invincible.main import app
from tests.conftest import TEST_REDIRECT_URI, register_account


async def _clear_login_attempts():
    """Both scopes live in login_attempts; start every test clean."""
    async with app.state.engine.begin() as conn:
        await conn.execute(text("DELETE FROM login_attempts"))


def _different_ip_client():
    """A second httpx client whose requests carry a distinct client IP
    (ASGITransport uses the transport's client address)."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("10.9.9.9", 1234)),
        base_url="http://test",
    )


async def test_register_over_the_cap_returns_429(client):
    await _clear_login_attempts()
    made = 0
    for _ in range(REGISTER_MAX_ATTEMPTS):
        response = await client.post(
            "/oauth/register",
            json={"redirect_uris": [TEST_REDIRECT_URI],
                  "client_name": "flood"},
        )
        assert response.status_code == 201, response.text
        made += 1
    assert made == REGISTER_MAX_ATTEMPTS

    refused = await client.post(
        "/oauth/register",
        json={"redirect_uris": [TEST_REDIRECT_URI],
              "client_name": "flood-overflow"},
    )
    assert refused.status_code == 429
    body = refused.json()
    assert body["error"] == "rate_limited"
    assert "retry in" in body["error_description"]
    # The refused request never became a client row.
    async with app.state.engine.connect() as conn:
        count = (await conn.execute(text(
            "SELECT COUNT(*) FROM oauth_clients"
            " WHERE client_name = 'flood-overflow'"))).scalar()
    assert count == 0


async def test_register_under_the_cap_still_succeeds(client):
    await _clear_login_attempts()
    for i in range(REGISTER_MAX_ATTEMPTS - 1):
        response = await client.post(
            "/oauth/register",
            json={"redirect_uris": [TEST_REDIRECT_URI],
                  "client_name": f"ok-{i}"},
        )
        assert response.status_code == 201, response.text


async def test_register_different_ip_is_unaffected(client):
    await _clear_login_attempts()
    for _ in range(REGISTER_MAX_ATTEMPTS):
        await client.post(
            "/oauth/register",
            json={"redirect_uris": [TEST_REDIRECT_URI],
                  "client_name": "same-ip"},
        )
    other = _different_ip_client()
    try:
        response = await other.post(
            "/oauth/register",
            json={"redirect_uris": [TEST_REDIRECT_URI],
                  "client_name": "other-ip"},
        )
        assert response.status_code == 201, response.text
    finally:
        await other.aclose()


async def test_invalid_register_bodies_still_count(client):
    """Every attempt is a potential row write, so even garbage bodies
    burn the budget - the cap guards the table, not just valid rows."""
    await _clear_login_attempts()
    for _ in range(REGISTER_MAX_ATTEMPTS):
        response = await client.post("/oauth/register", json=[1, 2])
        assert response.status_code == 400
    refused = await client.post(
        "/oauth/register",
        json={"redirect_uris": [TEST_REDIRECT_URI],
              "client_name": "late-valid"},
    )
    assert refused.status_code == 429


async def test_device_code_over_the_cap_returns_429(client):
    await _clear_login_attempts()
    for _ in range(DEVICE_CODE_MAX_ATTEMPTS):
        response = await client.post("/auth/device/code")
        assert response.status_code == 200, response.text
    refused = await client.post("/auth/device/code")
    assert refused.status_code == 429
    body = refused.json()
    assert body["error"]["code"] == "locked_out"
    assert "retry in" in body["error"]["message"]


async def test_device_code_different_ip_is_unaffected(client):
    await _clear_login_attempts()
    for _ in range(DEVICE_CODE_MAX_ATTEMPTS):
        await client.post("/auth/device/code")
    other = _different_ip_client()
    try:
        response = await other.post("/auth/device/code")
        assert response.status_code == 200, response.text
    finally:
        await other.aclose()


async def test_device_code_cap_does_not_lock_out_login(client):
    """Scope separation: exhausting the anonymous device-code budget
    leaves the auth-login scope untouched, so a real user at the same
    IP can still sign in (and still gets its OWN lockout after
    failures)."""
    await _clear_login_attempts()
    await register_account(client, "scope-user@example.com")
    for _ in range(DEVICE_CODE_MAX_ATTEMPTS):
        await client.post("/auth/device/code")
    # The device-code surface is now refusing this IP...
    assert (await client.post("/auth/device/code")).status_code == 429
    # ...but the login surface works.
    login = await client.post(
        "/auth/login",
        json={"email": "scope-user@example.com",
              "password": "longenough1"})
    assert login.status_code == 200, login.text
