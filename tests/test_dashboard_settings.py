# tests/test_dashboard_settings.py
"""Phase 5 PR-5E: password set/change + the dashboard settings page.

Covers both /auth/password flows following the STORED account state
(NULL password_hash -> first password, no current required - the
GitHub-only gap closure), wrong-current and weak-password rejections,
HTML form redirects with bounded pw_error codes, audit rows
(password.set / password.changed), the read-only system panel, and realm
separation: an inv_ API key authorizes neither the page nor the endpoint.
"""
from invincible.core.accounts import (
    SESSION_COOKIE,
    SessionManager,
    UserService,
)
from invincible.core.identity import ApiKeyStore, ensure_default_project
from invincible.main import app
from tests.conftest import login_account, register_account


async def github_only_session(client, email):
    """Create a NULL-password_hash account directly (the GitHub callback
    creates these) and ride it with a valid session cookie."""
    made = await UserService(
        app.state.engine).register_without_password(email)
    await ensure_default_project(app.state.engine, made["id"])
    client.cookies.set(SESSION_COOKIE, SessionManager.create(made["id"]))
    return made["id"]


async def recent_actions(client, limit=30):
    rows = await app.state.audit_log.recent(limit=limit)
    return [(r["action"], r["actor_user_id"]) for r in rows]


# --- Anonymous gate -------------------------------------------------------


async def test_settings_surfaces_require_session(client):
    assert (await client.get("/dashboard/settings")).status_code == 401
    posted = await client.post(
        "/auth/password", json={"new_password": "longenough1"})
    assert posted.status_code == 401


async def test_inv_api_key_never_authorizes_password_surface(client):
    registered, _ = await register_account(client, "keyed@example.com")
    uid = registered.json()["id"]
    key = await ApiKeyStore(app.state.engine).create(uid, label="t")
    client.cookies.delete(SESSION_COOKIE)
    headers = {"Authorization": f"Bearer {key['raw']}"}
    assert (await client.get("/dashboard/settings",
                             headers=headers)).status_code == 401
    posted = await client.post(
        "/auth/password", headers=headers,
        json={"new_password": "longenough6"})
    assert posted.status_code == 401


# --- Set flow (GitHub-only gap closure) ------------------------------------


async def test_github_only_set_then_login_end_to_end(client):
    uid = await github_only_session(client, "adopt@example.com")
    page = await client.get("/dashboard/settings")
    assert page.status_code == 200
    assert 'data-pw-mode="set"' in page.text
    assert 'name="current_password"' not in page.text

    made = await client.post(
        "/auth/password", json={"new_password": "longenough2"})
    assert made.status_code == 200
    assert made.json() == {"ok": True, "action": "set"}

    # The gap closes: password LOGIN works against that same row.
    client.cookies.delete(SESSION_COOKIE)
    assert (await login_account(
        client, "adopt@example.com", "longenough1")).status_code == 401
    good = await login_account(client, "adopt@example.com", "longenough2")
    assert good.status_code == 200
    assert good.json()["id"] == uid

    # State flipped over: the change variant shows, and a current-less
    # post can no longer overwrite anything.
    assert 'data-pw-mode="change"' in (await
                                       client.get(
                                           "/dashboard/settings")).text
    again = await client.post("/auth/password",
                              json={"new_password": "another-long"})
    assert again.status_code == 403
    assert ("password.set", uid) in await recent_actions(client)


async def test_change_flow_rotates_credentials(client):
    registered, _ = await register_account(client, "rotate@example.com")
    uid = registered.json()["id"]
    changed = await client.post("/auth/password", json={
        "current_password": "longenough1",
        "new_password": "longenough9"})
    assert changed.status_code == 200
    assert changed.json()["action"] == "changed"
    assert ("password.changed", uid) in await recent_actions(client)

    client.cookies.delete(SESSION_COOKIE)
    assert (await login_account(
        client, "rotate@example.com", "longenough1")).status_code == 401
    assert (await login_account(
        client, "rotate@example.com", "longenough9")).status_code == 200


async def test_wrong_current_rejected_and_untouched(client):
    await register_account(client, "wrongcur@example.com")
    bad = await client.post("/auth/password", json={
        "current_password": "not-the-password",
        "new_password": "longenough8"})
    assert bad.status_code == 403
    assert bad.json()["error"]["code"] == "wrong_password"

    client.cookies.delete(SESSION_COOKIE)
    still = await login_account(client, "wrongcur@example.com",
                                "longenough1")
    assert still.status_code == 200


async def test_weak_password_rejected_for_both_flows(client):
    await register_account(client, "weakreg@example.com")
    weak_change = await client.post("/auth/password", json={
        "current_password": "longenough1", "new_password": "tiny"})
    assert weak_change.status_code == 400
    assert weak_change.json()["error"]["code"] == "weak_password"
    client.cookies.delete(SESSION_COOKIE)
    assert (await login_account(
        client, "weakreg@example.com", "longenough1")).status_code == 200

    await github_only_session(client, "weakgh@example.com")
    weak_set = await client.post("/auth/password",
                                 json={"new_password": "short1"})
    assert weak_set.status_code == 400
    assert weak_set.json()["error"]["code"] == "weak_password"
    client.cookies.delete(SESSION_COOKIE)
    # No hash was written for the NULL-password account either.
    assert (await login_account(
        client, "weakgh@example.com", "whateverlong")).status_code == 401


# --- HTML form paths --------------------------------------------------------


async def test_html_form_paths_redirect_with_bounded_errors(client):
    await github_only_session(client, "formy@example.com")
    error_post = await client.post(
        "/auth/password", data={"new_password": "short"})
    assert error_post.status_code == 303
    assert error_post.headers["location"] == \
        "/dashboard/settings?pw_error=weak_password"
    shown = await client.get(error_post.headers["location"])
    assert shown.status_code == 200
    assert "at least 8 characters." in shown.text
    assert ">short<" not in shown.text  # input values are never echoed

    ok_post = await client.post("/auth/password",
                                data={"new_password": "longenough4"})
    assert ok_post.status_code == 303
    assert ok_post.headers["location"] == "/dashboard/settings?pw_saved=1"
    confirm = await client.get(ok_post.headers["location"])
    assert "Password updated." in confirm.text


# --- System panel ------------------------------------------------------------


async def test_system_panel_renders_readonly(client):
    # Note: app.state.registry may or may not be attached here - earlier
    # admin-API tests leave one on the shared app.state. The panel must
    # render correctly either way.
    await github_only_session(client, "panel@example.com")
    page = await client.get("/dashboard/settings")
    assert page.status_code == 200
    assert "Providers configured" in page.text
    assert "Browser sessions" in page.text
    assert ">yes<" in page.text  # owner secret + gateway key are set here
    assert page.text.count('href="/dashboard/settings">Settings</a>') == 1
