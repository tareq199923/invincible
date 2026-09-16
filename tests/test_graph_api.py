# tests/test_graph_api.py
"""Phase 15c: continuity-graph projection endpoint.

Covers authz (fail-closed inv_-key realm), the failover-chain edges that
answer "why did work move from A to B", state/checkpoint pinning edges, and
the summary contract - all as a pure PROJECTION over authoritative stores.

The operator override retired with Phase 2: every caller is a scoped user
principal confined to its own ownership triple.
"""
import pytest

from invincible.core.continuity import ContinuityEngine
from invincible.core.run_store import RunStore
from invincible.main import app
from tests.conftest import v1_user


@pytest.fixture
async def graph_stack(client, pg_engine, monkeypatch):
    # Attach runs + continuity exactly like the lifespan does.
    runs = RunStore(engine=pg_engine)
    engine = ContinuityEngine(engine=pg_engine, runs=runs)
    app.state.runs = runs
    app.state.continuity = engine
    try:
        yield runs, engine
    finally:
        await engine.close()
        await runs.close()


async def record_run(runs, request_id, outcome, provider="alpha",
                     attempt=1, session_pk=None):
    import time as _time

    await runs.record(
        {
            "request_id": request_id,
            "session_id": "default",
            "session_pk": session_pk,
            "provider_name": provider,
            "model_id": f"{provider}-model",
            "attempt_index": attempt,
            "outcome": outcome,
            "error_class": "500" if outcome != "ok" else None,
            "started_at": _time.time(),
            "finished_at": _time.time(),
        }
    )


async def _seed_session(client, email, session_id, turns):
    """A session row under the given user plus its inv_ auth headers."""
    uid, raw = await v1_user(client, email, providers=[])
    from invincible.core.identity import ensure_default_project

    pid = await ensure_default_project(app.state.engine, uid)
    await app.state.sessions.append(
        session_id, turns, user_id=uid, project_id=pid)
    return uid, pid, {"Authorization": f"Bearer {raw}"}


async def test_graph_without_any_credential_is_401(client, monkeypatch):
    """No bearer token: 401, not a scoped anonymous view (the fail-open
    gateway-key realm is long gone)."""
    monkeypatch.delenv("INVINCIBLE_OWNER_SECRET", raising=False)
    resp = await client.get("/api/v1/sessions/default/graph")
    assert resp.status_code == 401


async def test_graph_unknown_session_is_indistinguishable_from_foreign(
    client, graph_stack, byok_env
):
    """A user principal gets the projection for ITS OWN session; an
    unknown-to-it string is indistinguishable from a nonexistent one."""
    _, raw = await v1_user(client, "graph-scoped@example.com", providers=[])
    resp = await client.get(
        "/api/v1/sessions/ghost/graph",
        headers={"Authorization": f"Bearer {raw}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["known"] is False
    assert data["nodes"] == []


async def test_empty_session_projection_shape(client, graph_stack, byok_env):
    _, raw = await v1_user(client, "graph-shape@example.com", providers=[])
    resp = await client.get(
        "/api/v1/sessions/ghost/graph",
        headers={"Authorization": f"Bearer {raw}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == "ghost"
    assert data["known"] is False
    assert data["nodes"] == [] and data["edges"] == []
    assert data["summary"]["attempts"] == 0


async def test_failover_chain_edges_answer_the_core_question(
    client, graph_stack, byok_env
):
    """One request_id, three attempts alpha->beta->gamma: the projection
    must show WHY work moved (failover_from chain) and where it landed."""
    runs, _ = graph_stack
    uid, pid, auth = await _seed_session(
        client, "graph-failover@example.com", "default",
        [{"role": "user", "content": "count please"},
         {"role": "assistant", "content": "1 2 3"}])
    session_pk = await app.state.sessions.lookup(
        "default", user_id=uid, project_id=pid)
    await record_run(runs, "req-1", "failover", provider="alpha", attempt=1,
                     session_pk=session_pk)
    await record_run(runs, "req-1", "failover", provider="beta", attempt=2,
                     session_pk=session_pk)
    await record_run(runs, "req-1", "ok", provider="gamma", attempt=3,
                     session_pk=session_pk)

    resp = await client.get(
        "/api/v1/sessions/default/graph", headers=auth)
    data = resp.json()
    failovers = [e for e in data["edges"] if e["kind"] == "failover_from"]
    assert [(e["source"], e["target"]) for e in failovers] == [
        ("run:1", "run:2"),
        ("run:2", "run:3"),
    ]
    summary = data["summary"]
    assert summary["providers_used"] == ["alpha", "beta", "gamma"]
    assert summary["attempts"] == 3 and summary["failovers"] == 2
    # Timeline is time-ordered and contains the run nodes.
    assert [i for i in data["timeline"] if i.startswith("run:")] == [
        "run:1", "run:2", "run:3"
    ]


async def test_separate_requests_are_followed_by_not_failover(
    client, graph_stack, byok_env
):
    runs, _ = graph_stack
    uid, pid, auth = await _seed_session(
        client, "graph-followed@example.com", "default",
        [{"role": "user", "content": "count please"},
         {"role": "assistant", "content": "1 2 3"}])
    session_pk = await app.state.sessions.lookup(
        "default", user_id=uid, project_id=pid)
    await record_run(runs, "req-a", "ok", provider="alpha",
                     session_pk=session_pk)
    await record_run(runs, "req-b", "ok", provider="beta",
                     session_pk=session_pk)
    resp = await client.get(
        "/api/v1/sessions/default/graph", headers=auth)
    kinds = {(e["source"], e["target"]): e["kind"]
             for e in resp.json()["edges"] if e["target"].startswith("run:")
             and e["source"].startswith("run:")}
    assert kinds[("run:1", "run:2")] == "followed_by"


async def test_state_versions_and_checkpoint_pins(client, graph_stack, byok_env):
    _, engine = graph_stack
    uid, pid, auth = await _seed_session(
        client, "graph-state@example.com", "default",
        [{"role": "user", "content": "count please"},
         {"role": "assistant", "content": "1 2 3"}])
    session_pk = await app.state.sessions.lookup(
        "default", user_id=uid, project_id=pid)
    await engine.set_state("default", {"through": 5}, actor="mcp:tss",
                           session_pk=session_pk)
    await engine.set_state("default", {"through": 37}, actor="mcp:tss",
                           session_pk=session_pk)
    cp = await engine.create_checkpoint("default", note="through 37",
                                        actor="mcp:checkpoint_create",
                                        session_pk=session_pk)

    resp = await client.get(
        "/api/v1/sessions/default/graph", headers=auth)
    data = resp.json()
    state_ids = {n["id"] for n in data["nodes"] if n["kind"] == "task_state"}
    assert state_ids == {
        "state:default:v1", "state:default:v2"
    }
    supersede = [e for e in data["edges"] if e["kind"] == "supersedes"]
    assert (f"state:default:v{cp['state_version'] - 1}",
            f"state:default:v{cp['state_version']}") in [
        (e["source"], e["target"]) for e in supersede
    ]
    pins = [e for e in data["edges"] if e["kind"] == "pins"]
    assert pins == [{
        "source": f"checkpoint:{cp['id']}",
        "target": f"state:default:v{cp['state_version']}",
        "kind": "pins",
    }]
    assert data["summary"]["tasks"]["default"]["payload"] == {"through": 37}


async def test_interruption_note_surfaces_in_summary(client, graph_stack, byok_env):
    runs, engine = graph_stack
    uid, pid, auth = await _seed_session(
        client, "graph-note@example.com", "default",
        [{"role": "user", "content": "count please"},
         {"role": "assistant", "content": "1 2 3"}])
    session_pk = await app.state.sessions.lookup(
        "default", user_id=uid, project_id=pid)
    await engine.set_state("default", {"next": 6}, actor="mcp:x",
                           session_pk=session_pk)
    await engine.create_checkpoint("default", note="before resume",
                                   actor="mcp", session_pk=session_pk)
    await record_run(runs, "req-x", "error", provider="groq",
                     session_pk=session_pk)

    resp = await client.get(
        "/api/v1/sessions/default/graph", headers=auth)
    summary = resp.json()["summary"]
    assert summary["interruption_note"]
    assert "'groq'" in summary["interruption_note"]


async def test_turn_nodes_project_from_normalized_storage(
    client, graph_stack, byok_env
):
    uid, pid, auth = await _seed_session(
        client, "graph-turns@example.com", "default",
        [{"role": "user", "content": "count please"},
         {"role": "assistant", "content": "1 2 3"}])
    resp = await client.get(
        "/api/v1/sessions/default/graph", headers=auth)
    data = resp.json()
    assert data["known"] is True
    turn_nodes = [n for n in data["nodes"] if n["kind"] == "turn"]
    assert len(turn_nodes) == 1
    assert turn_nodes[0]["message_count"] == 2
    assert any(e for e in data["edges"]
               if e == {"source": "session", "target": "turn:0",
                        "kind": "contains"})
