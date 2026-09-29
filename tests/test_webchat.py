# tests/test_webchat.py
"""Dashboard webchat: cookie-realm BYOK chat with SSE streaming and
plan/manual/auto agent modes.

Covers the realm gates (anonymous 401 everywhere; inv_ API keys rejected
on every new route - realms never merge), cross-user isolation through the
new endpoints, the SSE happy path on mocked providers (persisted history
identical to the API path), the model picker contents, the
no-credentials error event, and the web-only sidebar.

Upstream mocks are non-streaming JSON bodies: the agent loop routes each
iteration through ``route_request_detailed`` (whole messages, so tool
calls arrive complete), while the browser still receives live SSE.
"""
import json

import httpx
from sqlalchemy import text

from invincible.core.accounts import SESSION_COOKIE
from invincible.core.credential_store import ByokCredentialStore
from invincible.core.identity import ApiKeyStore
from invincible.main import app
from tests.conftest import provider_body, register_account


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
    return {
        f"w{i + 1}.example.com": httpx.Response(
            200, json=provider_body(f"w{i + 1}", content=content))
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


async def test_stream_bounded_read_of_long_history(
    client, byok_env, router_setter
):
    """A webchat turn in a 100-turn session reads only the newest
    INVINCIBLE_HISTORY_READ_MAX_TURNS turns from Postgres (bounded prompt,
    bounded transfer) and still persists the new turn correctly."""
    captured = []

    def alpha_handler(request):
        captured.append(json.loads(request.read()))
        return httpx.Response(
            200, json=provider_body("Web1", content="hello"))

    router_setter({"w1.example.com": alpha_handler, "w2.example.com":
                   httpx.Response(200, json=provider_body("Web2", content="hi"))})
    uid, pid = await webchat_user(client, "bounded@example.com")
    store = app.state.sessions
    for i in range(50):
        await store.append(
            "web-abc123",
            [{"role": "user", "content": f"q{i}"},
             {"role": "assistant", "content": f"a{i}"}],
            user_id=uid, project_id=pid)

    resp = await client.post("/dashboard/chat/stream", json=STREAM_BODY)
    assert resp.status_code == 200, resp.text
    assert [d for n, d in parse_web_events(resp.text) if n == "error"] == []

    # The upstream prompt carried only the bounded window: q0 (the oldest
    # of 50 stored turns) is NOT in it, the newest turns are.
    contents = [m.get("content") for m in captured[-1]["messages"]]
    assert "q0" not in contents
    assert "q49" in contents
    # 30 turns x 2 + the new user turn (+ a system prompt).
    assert len(contents) <= 30 * 2 + 2

    # The new turn persisted once on top of the retained tail.
    history = await store.load("web-abc123", user_id=uid, project_id=pid)
    assert history[-2:] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    assert len(history) <= 30 * 2


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


async def test_sidebar_lists_only_dashboard_sessions(client, byok_env):
    uid, pid = await webchat_user(client, "filter@example.com",
                                  credential_count=0)
    store = app.state.sessions
    await store.append(
        "web-aaa",
        [{"role": "user", "content": "browser chat"}],
        user_id=uid, project_id=pid)
    await store.append(
        "09ba1cbf-0837-425a-93c6-89ec011caac7",
        [{"role": "user", "content": "agent chat"}],
        user_id=uid, project_id=pid)
    page = await client.get("/dashboard/chat")
    assert page.status_code == 200, page.text
    assert "browser chat" in page.text
    assert "agent chat" not in page.text
    assert "09ba1cbf" not in page.text
    # Direct links to owned API sessions still resolve.
    direct = await client.get(
        "/dashboard/chat?session=09ba1cbf-0837-425a-93c6-89ec011caac7")
    assert direct.status_code == 200, direct.text
    assert "agent chat" in direct.text


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


async def test_chat_list_requires_session(client):
    anon = await client.get("/dashboard/chat/list")
    assert anon.status_code == 401


async def test_chat_list_feeds_sidebar(client, byok_env):
    await webchat_user(client, "list@example.com", credential_count=0)
    body = (await client.get("/dashboard/chat/list")).json()
    assert body == {"sessions": []}
    page = await client.get("/dashboard/chat")
    assert 'id="side-history"' in page.text
    assert 'id="side-search"' in page.text
    assert "/static/app.css" in page.text


async def test_sidebar_query_count_is_constant(
    client, byok_env, statements
):
    """A chat page with many conversations must not issue a load() per
    session. Before: one full-payload SELECT per sidebar row (~1 + N, up
    to ~61 with the active session's second load). Now: the sidebar is ONE
    bounded SELECT (title snippet only), plus the active session's history
    - a small constant, independent of how many sessions exist."""
    uid, pid = await webchat_user(client, "many@example.com",
                                  credential_count=0)
    store = app.state.sessions
    for i in range(12):
        await store.append(
            f"web-many-{i:02d}",
            [{"role": "user", "content": f"conversation number {i}"},
             {"role": "assistant", "content": "ok"}],
            user_id=uid, project_id=pid)

    del statements[:]
    page = await client.get("/dashboard/chat")
    assert page.status_code == 200, page.text
    assert "conversation number 11" in page.text

    selects = [sql for sql, _ in statements
               if sql.lstrip().upper().startswith("SELECT")]
    # Only the queries that actually read message history matter for the
    # transfer bound (auth/credential reads are unrelated and constant).
    history_selects = [sql for sql in selects if "messages.payload" in sql]
    # One sidebar title query + the active session's history load - NOT one
    # per listed session (12 sessions here would have meant 13 before).
    assert len(history_selects) <= 2, selects
    # The sidebar query must NOT transfer the full payload column; it
    # extracts a bounded title server-side.
    assert any("substr" in sql.lower() for sql in history_selects), \
        history_selects


async def test_sidebar_rows_title_extraction(client, byok_env):
    """The single sidebar query derives titles from the first user message
    without pulling full payloads - behaviour matches the old load()-based
    derivation (first user content, whitespace-collapsed, elided)."""
    uid, pid = await webchat_user(client, "sql-title@example.com",
                                  credential_count=0)
    store = app.state.sessions
    await store.append(
        "web-sqltitle",
        [{"role": "assistant", "content": "greeting"},
         {"role": "user", "content": "   fix   the\n\nflaky   test  "}],
        user_id=uid, project_id=pid)
    rows = await store.sidebar_rows(
        uid, project_id=pid, client_session_id_prefix="web-")
    by_id = {r["client_session_id"]: r["first_user_content"] for r in rows}
    assert by_id["web-sqltitle"] == "   fix   the\n\nflaky   test  "
    page = await client.get("/dashboard/chat")
    assert "fix the flaky test" in page.text


# ---------------------------------------------------------------------------
# Sidebar management: rename / pin / delete (the ⋮ menu's routes)
# ---------------------------------------------------------------------------


async def _session_pk(uid, pid, client_id):
    return await app.state.sessions.lookup(
        client_id, user_id=uid, project_id=pid)


async def _row_counts(session_pk):
    """(sessions, turns, messages, runs) rows for one session pk."""
    async with app.state.engine.connect() as conn:
        out = []
        for sql in (
            "SELECT count(*) FROM sessions WHERE id = :pk",
            "SELECT count(*) FROM turns WHERE session_id = :pk",
            "SELECT count(*) FROM messages WHERE turn_id IN "
            "(SELECT id FROM turns WHERE session_id = :pk)",
            "SELECT count(*) FROM runs WHERE session_pk = :pk",
        ):
            out.append(int((await conn.execute(
                text(sql), {"pk": session_pk})).scalar_one()))
    return tuple(out)


async def test_chat_session_manage_requires_session(client):
    """Rename/pin/delete live on the cookie realm only (no key realm)."""
    assert (await client.patch(
        "/dashboard/chat/sessions/1", json={"title": "x"})).status_code == 401
    assert (await client.delete(
        "/dashboard/chat/sessions/1")).status_code == 401


async def test_chat_session_manage_rejects_inv_keys(client, byok_env):
    uid, pid = await webchat_user(client, "manage-keyed@example.com",
                                  credential_count=0)
    await app.state.sessions.append(
        "web-keyed", [{"role": "user", "content": "hi"}],
        user_id=uid, project_id=pid)
    pk = await _session_pk(uid, pid, "web-keyed")
    raw = (await ApiKeyStore(app.state.engine).create(uid, label="t"))["raw"]
    client.cookies.delete(SESSION_COOKIE)
    headers = {"Authorization": f"Bearer {raw}"}
    assert (await client.patch(
        f"/dashboard/chat/sessions/{pk}", json={"title": "nope"},
        headers=headers)).status_code == 401
    assert (await client.delete(
        f"/dashboard/chat/sessions/{pk}", headers=headers)).status_code == 401


async def test_rename_and_clear_chat_title(client, byok_env):
    uid, pid = await webchat_user(client, "rename@example.com",
                                  credential_count=0)
    await app.state.sessions.append(
        "web-rename", [{"role": "user", "content": "plan the migration"}],
        user_id=uid, project_id=pid)
    pk = await _session_pk(uid, pid, "web-rename")

    renamed = await client.patch(
        f"/dashboard/chat/sessions/{pk}", json={"title": "  Release  plan "})
    assert renamed.status_code == 200, renamed.text
    # Whitespace collapsed on the way in.
    assert renamed.json() == {"id": pk, "title": "Release plan"}

    page = (await client.get("/dashboard/chat")).text
    assert ">Release plan<" in page
    # The label shown is the custom one (the derived text is only kept in
    # the data-derived-title attribute asserted below).
    assert '<span class="sess-title">plan the migration' not in page
    # The derived label survives next to the custom one, so clearing a name
    # needs no extra round trip (the template renders data-derived-title).
    assert 'data-derived-title="plan the migration"' in page

    # Blank clears the custom name: the first-message label comes back.
    cleared = await client.patch(
        f"/dashboard/chat/sessions/{pk}", json={"title": "   "})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json() == {"id": pk, "title": None}
    page = (await client.get("/dashboard/chat")).text
    assert "plan the migration" in page


async def test_rename_validation(client, byok_env):
    uid, pid = await webchat_user(client, "rename-bad@example.com",
                                  credential_count=0)
    await app.state.sessions.append(
        "web-rename-bad", [{"role": "user", "content": "x"}],
        user_id=uid, project_id=pid)
    pk = await _session_pk(uid, pid, "web-rename-bad")
    for body in ({}, {"title": 5}, {"title": "x" * 101},
                 {"pinned": "yes"}):
        resp = await client.patch(
            f"/dashboard/chat/sessions/{pk}", json=body)
        assert resp.status_code == 400, body
    assert (await client.patch(
        f"/dashboard/chat/sessions/{pk}", content=b"not json")).status_code == 400
    # Nothing was written by the rejected requests.
    rows = await app.state.sessions.sidebar_rows(
        uid, project_id=pid, client_session_id_prefix="web-")
    assert rows[0]["title"] is None and rows[0]["pinned"] is False


async def test_pin_sorts_first_and_reports_state(client, byok_env):
    uid, pid = await webchat_user(client, "pin@example.com",
                                  credential_count=0)
    store = app.state.sessions
    await store.append("web-old", [{"role": "user", "content": "older chat"}],
                       user_id=uid, project_id=pid)
    await store.append("web-new", [{"role": "user", "content": "newer chat"}],
                       user_id=uid, project_id=pid)
    old_pk = await _session_pk(uid, pid, "web-old")

    # Newest-first to begin with.
    page = (await client.get("/dashboard/chat")).text
    assert page.index("newer chat") < page.index("older chat")

    pinned = await client.patch(
        f"/dashboard/chat/sessions/{old_pk}", json={"pinned": True})
    assert pinned.status_code == 200, pinned.text
    assert pinned.json() == {"id": old_pk, "pinned": True}

    page = (await client.get("/dashboard/chat")).text
    assert page.index("older chat") < page.index("newer chat")
    assert 'data-pinned="1"' in page
    assert "Unpin" in page

    listed = (await client.get("/dashboard/chat/list")).json()["sessions"]
    assert [s["client_session_id"] for s in listed] == ["web-old", "web-new"]
    assert listed[0]["pinned"] is True and listed[1]["pinned"] is False

    unpinned = await client.patch(
        f"/dashboard/chat/sessions/{old_pk}", json={"pinned": False})
    assert unpinned.json() == {"id": old_pk, "pinned": False}
    page = (await client.get("/dashboard/chat")).text
    assert page.index("newer chat") < page.index("older chat")


async def test_sidebar_html_fragment_is_the_same_markup(client, byok_env):
    """The lazy loader asks for HTML and gets the SAME partial the chat
    page rendered - one row definition, ⋮ menu included."""
    uid, pid = await webchat_user(client, "fragment@example.com",
                                  credential_count=0)
    await app.state.sessions.append(
        "web-frag", [{"role": "user", "content": "fragment chat"}],
        user_id=uid, project_id=pid)
    pk = await _session_pk(uid, pid, "web-frag")

    frag = await client.get("/dashboard/chat/list",
                            headers={"Accept": "text/html"})
    assert frag.status_code == 200, frag.text
    assert "text/html" in frag.headers["content-type"]
    assert 'class="chat-row' in frag.text
    assert 'class="chat-menu-btn"' in frag.text
    assert f'data-session-url="/dashboard/chat/sessions/{pk}"' in frag.text
    assert 'data-chat-action="rename"' in frag.text
    assert 'data-chat-action="pin"' in frag.text
    assert 'data-chat-action="delete"' in frag.text
    assert 'data-delete-mode="sidebar"' in frag.text
    assert "fragment chat" in frag.text
    # Not a page: it is injected into <ul id="side-history"> as-is.
    assert "<html" not in frag.text
    assert f'data-session-pk="{pk}"' in (await client.get(
        "/dashboard/chat")).text

    # The empty state belongs to the same fragment.
    await client.delete(f"/dashboard/chat/sessions/{pk}")
    frag = await client.get("/dashboard/chat/list",
                            headers={"Accept": "text/html"})
    assert 'id="side-history-empty"' in frag.text


async def test_delete_chat_cascades_and_is_one_shot(client, byok_env,
                                                    router_setter):
    router_setter(stream_handlers())
    await webchat_user(client, "delete@example.com")
    streamed = await client.post("/dashboard/chat/stream", json={
        "session_id": "web-del", "message": "delete me"})
    assert streamed.status_code == 200, streamed.text

    listed = (await client.get("/dashboard/chat/list")).json()["sessions"]
    assert [s["client_session_id"] for s in listed] == ["web-del"]
    pk = listed[0]["id"]
    # History + the run row it produced.
    assert await _row_counts(pk) == (1, 1, 2, 1)

    deleted = await client.delete(f"/dashboard/chat/sessions/{pk}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted": True}
    assert await _row_counts(pk) == (0, 0, 0, 0)
    assert (await client.get("/dashboard/chat/list")).json()["sessions"] == []
    assert "delete me" not in (await client.get("/dashboard/chat")).text
    # Gone means gone: a second delete is a 404, not a silent success.
    again = await client.delete(f"/dashboard/chat/sessions/{pk}")
    assert again.status_code == 404


async def test_delete_chat_htmx_returns_empty_204(client, byok_env):
    uid, pid = await webchat_user(client, "hx-delete@example.com",
                                  credential_count=0)
    await app.state.sessions.append(
        "web-hx", [{"role": "user", "content": "hx"}],
        user_id=uid, project_id=pid)
    pk = await _session_pk(uid, pid, "web-hx")
    resp = await client.delete(
        f"/dashboard/chat/sessions/{pk}", headers={"HX-Request": "true"})
    assert resp.status_code == 204
    assert resp.content == b""


async def test_foreign_chat_actions_are_indistinguishable_from_unknown(
    client, byok_env
):
    """Anti-enumeration: another user's chat behaves exactly like a chat
    that does not exist, and nothing is mutated by the attempt."""
    victim_uid, victim_pid = await webchat_user(client, "victim@example.com",
                                                credential_count=0)
    await app.state.sessions.append(
        "web-victim", [{"role": "user", "content": "private"}],
        user_id=victim_uid, project_id=victim_pid)
    victim_pk = await _session_pk(victim_uid, victim_pid, "web-victim")
    await app.state.sessions.rename_session(
        victim_pk, user_id=victim_uid, project_id=victim_pid, title="mine")

    # A second account (registering swaps the session cookie).
    attacker_uid, attacker_pid = await webchat_user(client,
                                                   "attacker@example.com",
                                                   credential_count=0)
    await app.state.sessions.append(
        "web-attacker", [{"role": "user", "content": "own chat"}],
        user_id=attacker_uid, project_id=attacker_pid)
    attacker_pk = await _session_pk(attacker_uid, attacker_pid, "web-attacker")
    unknown_pk = max(victim_pk, attacker_pk) + 1000

    for pk in (victim_pk, unknown_pk):
        assert (await client.patch(
            f"/dashboard/chat/sessions/{pk}",
            json={"title": "stolen"})).status_code == 404
        assert (await client.patch(
            f"/dashboard/chat/sessions/{pk}",
            json={"pinned": True})).status_code == 404
        assert (await client.delete(
            f"/dashboard/chat/sessions/{pk}")).status_code == 404

    # The victim's row is untouched, and the attacker still owns theirs.
    rows = await app.state.sessions.sidebar_rows(victim_uid,
                                                 project_id=victim_pid)
    assert [(r["client_session_id"], r["title"], r["pinned"])
            for r in rows] == [("web-victim", "mine", False)]
    own = await app.state.sessions.sidebar_rows(attacker_uid,
                                                project_id=attacker_pid)
    assert [r["client_session_id"] for r in own] == ["web-attacker"]


async def test_store_management_requires_the_owner_triple(client, byok_env):
    """The store's own contract: a foreign pk reports False/None, so a call
    site that forgets the predicate fails loudly instead of silently
    reading or deleting someone else's conversation."""
    uid, pid = await webchat_user(client, "store-scope@example.com",
                                  credential_count=0)
    await app.state.sessions.append(
        "web-scope", [{"role": "user", "content": "scoped"}],
        user_id=uid, project_id=pid)
    pk = await _session_pk(uid, pid, "web-scope")
    store = app.state.sessions

    assert await store.rename_session(
        pk, user_id=uid + 999, project_id=pid, title="x") is False
    assert await store.set_pinned(
        pk, user_id=uid, project_id=pid + 999, pinned=True) is None
    assert await store.delete_session(
        pk, user_id=uid + 999, project_id=pid) is False
    assert await store.rename_session(
        pk, user_id=uid, project_id=pid, title="kept") is True
    assert await store.set_pinned(
        pk, user_id=uid, project_id=pid, pinned=True) is True
    assert await store.delete_session(
        pk, user_id=uid, project_id=pid) is True
    assert await store.delete_session(
        pk, user_id=uid, project_id=pid) is False




