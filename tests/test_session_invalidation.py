# tests/test_session_invalidation.py
"""Phase 5 hardening: session_version invalidation on password change.

Every signed cookie (``v2.<uid>.<version>.<expiry>``) pins the users-row
session_version it was minted against; both password flows bump that
version inside the same UPDATE as the hash, so any OTHER cookie for the
user fails Principal resolution immediately - while the acting browser
receives a freshly minted cookie on success. API keys are a separate
realm (random token, sha256 at rest, never derived from the password)
and must keep working across a change.
"""
from invincible.core.accounts import (
    SESSION_COOKIE,
    SessionManager,
    UserService,
)
from invincible.core.identity import ApiKeyStore, ensure_default_project
from invincible.main import app
from tests.conftest import login_account, register_account


def stash_cookie(client) -> str:
    return client.cookies.get(SESSION_COOKIE)


def swap_cookie(client, value: str | None) -> None:
    # Response cookies live under the request host's domain while a plain
    # cookies.set() lands domainless - keeping both sends TWO
    # invincible_session header values and "which one wins" depends on
    # parse order (observed flipping between environments). Clearing the
    # jar first leaves exactly one cookie; only the session cookie exists
    # in these flows.
    client.cookies.clear()
    if value is not None:
        client.cookies.set(SESSION_COOKIE, value)


async def make_user(client, email):
    made, _ = await register_account(client, email)
    return made.json()["id"]


async def test_change_password_kills_other_sessions_keeps_actor(client):
    uid = await make_user(client, "rotate@example.com")
    actor_cookie = stash_cookie(client)

    # A second device holds its own pre-change cookie.
    other_device = SessionManager.create(uid, 0)

    changed = await client.post("/auth/password", json={
        "current_password": "longenough1",
        "new_password": "longenough9"})
    assert changed.status_code == 200

    # The ACTING browser was re-issued a live cookie (new version) ...
    me = await client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["id"] == uid
    user = await UserService(app.state.engine).get(uid)
    assert user["session_version"] == 1
    assert stash_cookie(client) != actor_cookie

    # ... while BOTH pre-change cookies are dead on arrival.
    swap_cookie(client, actor_cookie)
    stale_actor = await client.get("/auth/me")
    assert stale_actor.status_code == 401
    swap_cookie(client, other_device)
    stale_second = await client.get("/auth/me")
    assert stale_second.status_code == 401

    # And a FRESH login works normally, issued at the current version.
    swap_cookie(client, None)
    login = await login_account(client, "rotate@example.com",
                                "longenough9")
    assert login.status_code == 200
    assert (await client.get("/auth/me")).status_code == 200


async def test_github_only_set_password_invalidates_too(client):
    engine = app.state.engine
    made = await UserService(engine).register_without_password(
        "ghonly@example.com")
    await ensure_default_project(engine, made["id"])
    client.cookies.set(
        SESSION_COOKIE, SessionManager.create(made["id"], 0))

    set_pw = await client.post(
        "/auth/password", json={"new_password": "longenough7"})
    assert set_pw.status_code == 200

    swap_cookie(client, None)
    login = await login_account(client, "ghonly@example.com",
                                "longenough7")
    assert login.status_code == 200
    user = await UserService(engine).get(made["id"])
    assert user["session_version"] == 1


async def test_other_users_are_unaffected_by_someone_elses_change(client):
    uid_a = await make_user(client, "keeper@example.com")
    uid_b = await make_user(client, "changer@example.com")
    assert uid_a != uid_b
    keeper_cookie = SessionManager.create(uid_a, 0)

    changed = await client.post("/auth/password", json={
        "current_password": "longenough1",
        "new_password": "longenough8"})
    assert changed.status_code == 200  # acts as B (jar holds B's cookie)

    swap_cookie(client, keeper_cookie)
    me = await client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["id"] == uid_a


async def test_api_keys_keep_working_across_a_password_change(client):
    uid = await make_user(client, "keyed@example.com")
    key = await ApiKeyStore(app.state.engine).create(uid, label="t")

    changed = await client.post("/auth/password", json={
        "current_password": "longenough1",
        "new_password": "longenough5"})
    assert changed.status_code == 200

    # The separately-issued key resolves exactly as before the change.
    listing = await client.get(
        "/api-keys", headers={"Authorization": f"Bearer {key['raw']}"})
    assert listing.status_code == 200
    prefixes = [k["prefix"] for k in listing.json()["api_keys"]]
    assert key["prefix"] in prefixes
