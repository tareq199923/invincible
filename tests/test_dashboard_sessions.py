# tests/test_dashboard_sessions.py
"""Phase 5 PR-5B: dashboard session list/detail pages and the cross-session
task list (cookie realm).

Covers: anonymous 401s, the sessions list page, per-session detail
rendered from the SAME projection core.projection builds for the graph
API, identical generic missing pages for unknown AND foreign session ids
(anti-enumeration), task-head isolation between users, and the overview
Tasks card.
"""
import time

from invincible.main import app
from tests.conftest import register_account


def card_count(html: str, name: str) -> int:
    import re

    match = re.search(rf'data-card="{name}"><span class="num">(\d+)<', html)
    assert match is not None, f"card {name} missing from page"
    return int(match.group(1))


async def make_session(client, client_id: str, *, email: str,
                       content="hello there"):
    """Register an account through HTTP and seed one owned session row;
    returns (uid, pid, session_pk)."""
    made, _ = await register_account(client, email)
    body = made.json()
    uid, pid = body["id"], body["project_id"]
    await app.state.sessions.append(
        client_id, [{"role": "user", "content": content}],
        user_id=uid, project_id=pid)
    pk = await app.state.sessions.lookup(
        client_id, user_id=uid, project_id=pid)
    return uid, pid, pk


# --- Anonymous gate -------------------------------------------------------


async def test_new_pages_require_session(client):
    for path in ("/dashboard/sessions",
                 "/dashboard/tasks",
                 "/dashboard/sessions/1"):
        anon = await client.get(path)
        assert anon.status_code == 401, path


# --- Sessions list --------------------------------------------------------


async def test_sessions_page_lists_owned_rows_with_links(client):
    _, _, pk = await make_session(client, "web-1", email="list@example.com")
    page = await client.get("/dashboard/sessions")
    assert page.status_code == 200
    assert "web-1" in page.text
    assert f'href="/dashboard/sessions/{pk}"' in page.text


async def test_sessions_page_empty_state(client):
    await register_account(client, "nosessions@example.com")
    page = await client.get("/dashboard/sessions")
    assert page.status_code == 200
    assert "No sessions yet." in page.text


# --- Session detail (projection reuse) -------------------------------------


async def test_detail_page_renders_projection_pieces(client):
    _, _, pk = await make_session(client, "detail-1",
                                  email="detail@example.com")
    runs, continuity = app.state.runs, app.state.continuity
    # Checkpoint FIRST so the seeded failure genuinely lands AFTER it -
    # interruption_note only reports post-checkpoint upstream failures.
    await continuity.set_state("detail-1", {"through": 5}, actor="mcp:t",
                               session_pk=pk)
    await continuity.set_state("detail-1", {"through": 9}, actor="mcp:t",
                               session_pk=pk)
    await continuity.create_checkpoint(
        "detail-1", note="through 9", actor="mcp:c", session_pk=pk)
    # Runs stamped AFTER the checkpoint instant (C): interruption_note
    # skips any attempt whose finished_at <= latest checkpoint time.
    base = time.time() + 10
    for rid, outcome, provider, index, offset, err in [
        ("req-1", "failover", "alpha", 1, 0.0, "429"),
        ("req-1", "ok", "beta", 2, 1.0, None),
        ("req-2", "error", "beta", 1, 2.0, "timeout"),
    ]:
        await runs.record({
            "request_id": rid, "session_id": "detail-1",
            "session_pk": pk, "provider_name": provider,
            "model_id": f"{provider}-model", "attempt_index": index,
            "outcome": outcome, "error_class": err,
            "started_at": base + offset,
            "finished_at": base + offset + 0.5,
            "meta": None,
        })

    page = await client.get(f"/dashboard/sessions/{pk}")
    assert page.status_code == 200
    assert "detail-1" in page.text
    # Summary numbers come from the shared projection...
    assert "attempt #1 → alpha/alpha-model [failover]" in page.text
    assert "attempt #2 → beta/beta-model [ok]" in page.text
    # ...as do task heads, checkpoint pins, and the interruption banner.
    assert "&#34;through&#34;: 9" in page.text or '"through": 9' in page.text
    assert "through 9" in page.text
    assert "ended unexpectedly" in page.text
    assert "alpha/alpha-model [failover] → attempt #2 → beta/beta-model [ok]" \
        in page.text.replace("\n", "")


async def test_unknown_and_foreign_detail_are_identical_404s(client):
    _, _, foreign_pk = await make_session(client, "secret-1",
                                          email="a@example.com")
    # Switch the browser cookie to a different account entirely.
    await register_account(client, "b@example.com")
    stranger = await client.get(f"/dashboard/sessions/{foreign_pk}")
    ghost = await client.get("/dashboard/sessions/999999")
    assert stranger.status_code == ghost.status_code == 404
    assert "secret-1" not in stranger.text
    # Existence never leaks: same generic body for foreign AND unknown.
    assert stranger.text == ghost.text


# --- Cross-session task list ----------------------------------------------


async def test_tasks_page_shows_active_heads_only(client):
    _, pid, pk = await make_session(client, "tasked-1",
                                    email="tasks@example.com")
    continuity = app.state.continuity
    await continuity.set_state("tasked-1", {"step": 1}, actor="mcp:t",
                               session_pk=pk)
    await continuity.set_state("tasked-1", {"step": 2}, actor="mcp:t",
                               session_pk=pk)
    # A superseded task key disappears; only its head shows.
    await continuity.set_state("tasked-1", {"n": 1}, actor="mcp:t",
                               task_key="docs", status="done",
                               session_pk=pk)

    mine = await client.get("/dashboard/tasks")
    assert mine.status_code == 200
    assert "tasked-1" in mine.text
    assert "v2" in mine.text          # head version, not v1
    assert "docs" not in mine.text    # non-active heads are filtered
    assert f'href="/dashboard/sessions/{pk}"' in mine.text
    # Another account sees NONE of these heads.
    await register_account(client, "elsewhere@example.com")
    theirs = await client.get("/dashboard/tasks")
    assert theirs.status_code == 200
    assert "tasked-1" not in theirs.text
    assert "No active tasks right now." in theirs.text


async def test_overview_tasks_card_counts_heads(client):
    _, _, pk = await make_session(client, "countme-1",
                                  email="cards@example.com")
    await app.state.continuity.set_state(
        "countme-1", {"a": 1}, actor="mcp:t", session_pk=pk)
    await app.state.continuity.set_state(
        "countme-1", {"b": 2}, actor="mcp:t", task_key="second",
        session_pk=pk)
    page = await client.get("/dashboard")
    assert card_count(page.text, "tasks") == 2
