# tests/test_dashboard_memory.py
"""Phase 5 PR-5C: memory management (dashboard page + /memories API).

Covers the cookie-realm gate, explicit-layer creation (JSON + form),
the INVINCIBLE_MEMORY kill-switch gating creation only, layer/kind
filters and pagination, lexical search with AND->OR fallback scoped to
one owner, audited ownership-predicated deletes with anti-enumeration
404s, cross-user invisibility everywhere, and the overview Memories
card.
"""
import re

from invincible.main import app
from tests.conftest import login_account, register_account

FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def card_count(html: str, name: str) -> int:
    match = re.search(rf'data-card="{name}"><span class="num">(\d+)<', html)
    assert match is not None, f"card {name} missing from page"
    return int(match.group(1))


async def make_user(client, email):
    made, _ = await register_account(client, email)
    return made.json()["id"]


async def add_memory(client, content, kind="note"):
    return await client.post(
        "/memories", json={"content": content, "kind": kind})


# --- Anonymous gate -------------------------------------------------------


async def test_memory_surfaces_require_session(client):
    assert (await client.get("/dashboard/memory")).status_code == 401
    assert (await client.get("/memories")).status_code == 401
    assert (await client.post(
        "/memories", json={"content": "x"})).status_code == 401
    assert (await client.delete("/memories/1")).status_code == 401


# --- Creation --------------------------------------------------------------


async def test_create_lands_explicit_layer_with_defaults(client):
    await make_user(client, "creator@example.com")
    resp = await add_memory(client, "prefers concise answers")
    assert resp.status_code == 201
    body = resp.json()
    rows = (await client.get("/memories")).json()["memories"]
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == body["id"]
    assert row["content"] == "prefers concise answers"
    assert row["layer"] == "explicit"      # dashboard saves are deliberate
    assert row["kind"] == "note"
    assert float(row["confidence"]) == 1.0
    assert row["scope"] == "user"


async def test_create_form_post_redirects_back_to_page(client):
    await make_user(client, "formy@example.com")
    made = await client.post(
        "/memories",
        content="content=form+saved+memory&kind=preference",
        headers=FORM,
        follow_redirects=False,
    )
    assert made.status_code == 303
    assert made.headers["location"] == "/dashboard/memory"
    rows = (await client.get("/memories")).json()["memories"]
    assert [r["content"] for r in rows] == ["form saved memory"]
    assert rows[0]["kind"] == "preference"


async def test_create_rejects_blank_and_oversized_content(client):
    await make_user(client, "limits@example.com")
    blank = await client.post("/memories", json={"content": "   "})
    assert blank.status_code == 400
    huge = await client.post(
        "/memories", json={"content": "x" * 2001})
    assert huge.status_code == 400


async def test_kill_switch_gates_creation_not_browse_or_delete(client,
                                                               monkeypatch):
    await make_user(client, "toggled@example.com")
    saved = await add_memory(client, "kept while toggle flips")
    assert saved.status_code == 201
    monkeypatch.setenv("INVINCIBLE_MEMORY", "0")
    blocked = await add_memory(client, "must not land")
    assert blocked.status_code == 503
    assert blocked.json()["error"]["code"] == "memory_disabled"
    # Browse + delete stay available so data is never trapped.
    rows = (await client.get("/memories")).json()["memories"]
    assert len(rows) == 1
    gone = await client.delete(f"/memories/{rows[0]['id']}")
    assert gone.status_code == 200


# --- Browse: filters + pagination ------------------------------------------


async def test_filters_and_pagination(client):
    uid = await make_user(client, "filterer@example.com")
    store = app.state.memory
    await store.save_memory(user_id=uid, content="auto fact one",
                            layer="auto", kind="fact", confidence=0.6)
    await add_memory(client, "explicit note two", kind="note")
    await add_memory(client, "explicit decision three", kind="decision")

    everything = (await client.get("/memories")).json()
    assert everything["total"] == 3
    auto_only = (await client.get(
        "/memories", params={"layer": "auto"})).json()
    assert [r["content"] for r in auto_only["memories"]] == \
        ["auto fact one"]
    decisions = (await client.get(
        "/memories", params={"kind": "decision"})).json()
    assert decisions["total"] == 1
    assert decisions["memories"][0]["layer"] == "explicit"

    paged = (await client.get(
        "/memories", params={"limit": 2})).json()
    assert paged["total"] == 3 and len(paged["memories"]) == 2
    rest = (await client.get(
        "/memories", params={"limit": 2, "offset": 2})).json()
    assert len(rest["memories"]) == 1

    bad_layer = await client.get("/memories", params={"layer": "nope"})
    assert bad_layer.status_code == 400


# --- Search -----------------------------------------------------------------


async def test_search_and_terms_then_or_fallback_scoped_to_owner(client):
    await make_user(client, "searcher@example.com")
    await add_memory(client, "uses postgres pooling everywhere")
    await add_memory(client, "prefers dark mode editors")

    hits = (await client.get(
        "/memories", params={"q": "postgres pooling"})).json()["memories"]
    assert [h["content"] for h in hits] == \
        ["uses postgres pooling everywhere"]

    # AND misses ("unicorn" matches nothing), OR fallback recalls.
    fallback = (await client.get(
        "/memories", params={"q": "postgres unicorn"})).json()["memories"]
    assert [h["content"] for h in fallback] == \
        ["uses postgres pooling everywhere"]

    none = (await client.get(
        "/memories", params={"q": "zzzqqqxxx"})).json()["memories"]
    assert none == []

    # Another user's identical content never leaks into the results.
    await register_account(client, "rival@example.com")
    foreign_scope = (await client.get(
        "/memories", params={"q": "postgres"})).json()["memories"]
    assert foreign_scope == []


# --- Deletion ----------------------------------------------------------------


async def test_delete_own_is_audited(client):
    uid = await make_user(client, "deleter@example.com")
    mid = (await add_memory(client, "ephemeral note")).json()["id"]

    deleted = await client.delete(f"/memories/{mid}")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}
    remaining = (await client.get("/memories")).json()["total"]
    assert remaining == 0

    actions = [
        r["action"]
        for r in await app.state.audit_log.recent(limit=20)
        if r["actor_user_id"] == uid
    ]
    assert "memory.created" in actions
    assert "memory.deleted" in actions


async def test_delete_foreign_and_unknown_identical_404s(client):
    await register_account(client, "victim@example.com")
    owner_mid = (await add_memory(client, "private fact")).json()["id"]

    # Switch the browser cookie to a different account entirely.
    await register_account(client, "attacker@example.com")
    stranger = await client.delete(f"/memories/{owner_mid}")
    ghost = await client.delete("/memories/999999")
    assert stranger.status_code == ghost.status_code == 404
    assert stranger.text == ghost.text

    # The foreign attempt destroyed nothing - the victim still owns it.
    await login_account(client, "victim@example.com",
                        password="longenough1")
    assert (await client.get("/memories")).json()["total"] == 1


# --- Dashboard page -----------------------------------------------------------


async def test_memory_page_renders_rows_search_and_delete_buttons(client):
    uid = await make_user(client, "pager@example.com")
    await add_memory(client, "visible on the page")
    await app.state.memory.save_memory(
        user_id=uid, content="quiet auto row", layer="auto",
        confidence=0.6)

    page = await client.get("/dashboard/memory")
    assert page.status_code == 200
    assert "visible on the page" in page.text
    assert "quiet auto row" in page.text
    assert 'hx-delete="/memories/' in page.text
    assert 'data-card' not in page.text  # page, not overview

    searched = await client.get(
        "/dashboard/memory", params={"q": "postgres"})
    assert "No memories matching your search." in searched.text
    hit = await client.get(
        "/dashboard/memory", params={"q": "visible"})
    assert "visible on the page" in hit.text


async def test_overview_memories_card_counts_owned_rows(client):
    await make_user(client, "cardy@example.com")
    empty = await client.get("/dashboard")
    assert card_count(empty.text, "memories") == 0
    await add_memory(client, "counted once")
    again = await client.get("/dashboard")
    assert card_count(again.text, "memories") == 1
