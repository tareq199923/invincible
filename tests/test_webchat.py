# tests/test_webchat.py
"""Dashboard webchat: cookie-realm BYOK chat with SSE streaming (text-only v1).

Covers the realm gates (anonymous 401 everywhere; inv_ API keys rejected
on every new route - realms never merge), cross-user isolation through the
new endpoints, the SSE happy path on mocked providers (persisted history
identical to the API path), the model picker contents, and the
no-credentials error event.
"""
import httpx

from invincible.core.accounts import SESSION_COOKIE
from invincible.core.credential_store import ByokCredentialStore
from invincible.core.identity import ApiKeyStore
from invincible.main import app
from tests.conftest import (
    provider_body,
    register_account,
    sse_body,
    stream_chunk,
)


async def webchat_user(client, email, credential_count=1):
    """Register an account (session cookie set) and connect N mock
    providers through the real encrypted store. Returns (uid, pid)."""
    registered, _ = await register_account(client, email)
    assert registered.status_code == 201, registered.text
    body = registered.json()
    store = ByokCredentialStore(app.state.engine)
    for i in range(credential_count):
        await store.create(
            user_id=body["id"],
            provider_name=f"Web{i + 1}",
            model_id=f"w{i + 1}-model",
            base_url=f"https://w{i + 1}.example.com/v1",
            api_key=f"web-key-{i + 1}",
        )
    return body["id"], body["project_id"]


def stream_handlers(content="hello"):
    chunks = [
        stream_chunk("w1", {"content": content}),
        stream_chunk("w1", {}, finish_reason="stop"),
    ]
    return {
        f"w{i + 1}.example.com": httpx.Response(
            200, content=sse_body(*chunks))
        for i in range(2)
    }


def parse_web_events(text):
    """Split a webchat SSE body into (event, data) pairs."""
    import json

    events = []
    for part in text.split("\n\n"):
        part = part.strip()
        if not part:
            continue
        name, data = None, None
        for line in part.split("\n"):
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:"):].strip())
        events.append((name, data))
    return events


STREAM_BODY = {"session_id": "web-abc123", "message": "hi"}


async def test_webchat_requires_session(client):
    anon = await client.get("/dashboard/chat")
    assert anon.status_code == 401
    anon = await client.post("/dashboard/chat/new", json={})
    assert anon.status_code == 401
    anon = await client.get("/dashboard/chat/models")
    assert anon.status_code == 401
    anon = await client.post("/dashboard/chat/stream", json=STREAM_BODY)
    assert anon.status_code == 401


async def test_webchat_rejects_inv_keys(client, byok_env):
    uid, _pid = await webchat_user(client, "keyed@example.com")
    raw = (await ApiKeyStore(app.state.engine).create(uid, label="t"))["raw"]
    client.cookies.delete(SESSION_COOKIE)
    headers = {"Authorization": f"Bearer {raw}"}
    assert (await client.get("/dashboard/chat", headers=headers)).status_code == 401
    assert (await client.post(
        "/dashboard/chat/new", json={}, headers=headers)).status_code == 401
    assert (await client.get(
        "/dashboard/chat/models", headers=headers)).status_code == 401
    assert (await client.post(
        "/dashboard/chat/stream", json=STREAM_BODY,
        headers=headers)).status_code == 401


async def test_chat_page_renders_picker_and_empty_sidebar(
    client, byok_env, router_setter
):
    router_setter({})
    await webchat_user(client, "page@example.com", credential_count=2)
    page = await client.get("/dashboard/chat")
    assert page.status_code == 200, page.text
    assert "w1-model" in page.text
    assert "w2-model" in page.text
    assert 'href="/dashboard/chat"' in page.text
    assert "No conversations yet." in page.text
    assert "No AI provider" not in page.text


async def test_chat_page_no_credentials_empty_state(client, byok_env):
    await webchat_user(client, "empty@example.com", credential_count=0)
    page = await client.get("/dashboard/chat")
    assert page.status_code == 200, page.text
    assert "No AI provider" in page.text
    assert "/dashboard/providers" in page.text
    models = (await client.get("/dashboard/chat/models")).json()
    assert models == {"models": []}


async def test_new_chat_json_and_form(client, byok_env):
    await webchat_user(client, "new@example.com", credential_count=0)
    made = await client.post("/dashboard/chat/new", json={})
    assert made.status_code == 200, made.text
    session_id = made.json()["session_id"]
    assert session_id.startswith("web-")

    # Non-empty data so httpx sends a form-encoded body like a browser.
    formed = await client.post("/dashboard/chat/new", data={"x": "1"})
    assert formed.status_code == 303, formed.text
    assert formed.headers["location"].startswith(
        "/dashboard/chat?session=web-")


async def test_models_lists_candidates(client, byok_env):
    await webchat_user(client, "mods@example.com", credential_count=2)
    models = (await client.get("/dashboard/chat/models")).json()
    assert models == {"models": ["w1-model", "w2-model"]}


async def test_stream_happy_path_and_history(
    client, byok_env, router_setter
):
    router_setter(stream_handlers())
    uid, pid = await webchat_user(client, "stream@example.com")
    resp = await client.post("/dashboard/chat/stream", json=STREAM_BODY)
    assert resp.status_code == 200, resp.text
    assert "text/event-stream" in resp.headers["content-type"]
    events = parse_web_events(resp.text)
    tokens = "".join(
        data["text"] for name, data in events if name == "token")
    assert tokens == "hello"
    done = [data for name, data in events if name == "done"]
    assert len(done) == 1
    assert done[0]["provider"] == "Web1"
    assert done[0]["model"] == "w1-model"
    assert done[0]["bubble_html"] == "hello"
    assert not [data for name, data in events if name == "error"]
    # Persisted history is the API-path shape: the user turn plus the
    # assembled assistant turn, no system injections stored.
    history = await app.state.sessions.load(
        "web-abc123", user_id=uid, project_id=pid)
    assert history == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


async def test_stream_no_credentials_error_event(client, byok_env):
    await webchat_user(client, "nocreds@example.com", credential_count=0)
    resp = await client.post("/dashboard/chat/stream", json=STREAM_BODY)
    assert resp.status_code == 200, resp.text
    events = parse_web_events(resp.text)
    assert not [data for name, data in events if name == "token"]
    errors = [data for name, data in events if name == "error"]
    assert len(errors) == 1
    assert "No AI provider" in errors[0]["message"]
    assert "/dashboard/providers" in errors[0]["message"]


async def test_stream_validation(client, byok_env):
    await webchat_user(client, "valid@example.com", credential_count=0)
    assert (await client.post(
        "/dashboard/chat/stream", json={})).status_code == 400
    assert (await client.post(
        "/dashboard/chat/stream",
        json={"session_id": "web-x", "message": "  "})).status_code == 400
    assert (await client.post(
        "/dashboard/chat/stream",
        json={"message": "hi"})).status_code == 400
    assert (await client.post(
        "/dashboard/chat/stream",
        json={"session_id": "web-x", "message": "hi",
              "model": "m" * 201})).status_code == 400


async def test_cross_user_isolation(client, byok_env, router_setter):
    router_setter(stream_handlers())
    uid_a, pid_a = await webchat_user(client, "iso-a@example.com")
    resp = await client.post("/dashboard/chat/stream", json={
        "session_id": "web-shared", "message": "alpha secret"})
    assert resp.status_code == 200, resp.text

    # User B on the same client (cookie replaced): A's session reads as
    # empty, and the sidebar shows none of A's titles.
    uid_b, pid_b = await webchat_user(client, "iso-b@example.com")
    page = await client.get("/dashboard/chat?session=web-shared")
    assert page.status_code == 200, page.text
    assert "alpha secret" not in page.text
    # Foreign ids render exactly like unknown ones: empty thread.
    assert "No messages yet." in page.text
    sidebar = await client.get("/dashboard/chat")
    assert "alpha secret" not in sidebar.text

    # B writing to the same client string lands in B's own namespaced
    # history, never in A's.
    resp = await client.post("/dashboard/chat/stream", json={
        "session_id": "web-shared", "message": "beta hello"})
    assert resp.status_code == 200, resp.text
    history_a = await app.state.sessions.load(
        "web-shared", user_id=uid_a, project_id=pid_a)
    history_b = await app.state.sessions.load(
        "web-shared", user_id=uid_b, project_id=pid_b)
    assert [m["content"] for m in history_a] == ["alpha secret", "hello"]
    assert [m["content"] for m in history_b] == ["beta hello", "hello"]


async def test_history_shared_with_api_path(client, byok_env, router_setter):
    router_setter(stream_handlers())
    uid, pid = await webchat_user(client, "shared@example.com")
    resp = await client.post("/dashboard/chat/stream", json={
        "session_id": "web-mixed", "message": "from browser"})
    assert resp.status_code == 200, resp.text

    # The same conversation continues over /v1/* with the user's own key.
    router_setter({
        "w1.example.com": httpx.Response(
            200, json=provider_body("w1", content="from api")),
        "w2.example.com": httpx.Response(
            200, json=provider_body("w2", content="from api")),
    })
    raw = (await ApiKeyStore(app.state.engine).create(uid, label="t"))["raw"]
    api = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "from client"}]},
        headers={"Authorization": f"Bearer {raw}",
                 "X-Session-Id": "web-mixed"},
    )
    assert api.status_code == 200, api.text

    history = await app.state.sessions.load(
        "web-mixed", user_id=uid, project_id=pid)
    assert [m["content"] for m in history] == [
        "from browser", "hello", "from client", "from api"]
    # ... and the browser page renders the whole mixed thread.
    page = await client.get("/dashboard/chat?session=web-mixed")
    assert page.status_code == 200, page.text
    assert "from browser" in page.text
    assert "from client" in page.text


async def test_sidebar_title_from_first_message(
    client, byok_env, router_setter
):
    router_setter(stream_handlers())
    await webchat_user(client, "titles@example.com")
    await client.post("/dashboard/chat/stream", json={
        "session_id": "web-t1", "message": "plan the migration carefully"})
    page = await client.get("/dashboard/chat")
    assert page.status_code == 200, page.text
    assert "plan the migration carefully" in page.text
