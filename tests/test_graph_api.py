# tests/test_graph_api.py
"""Phase 15c: continuity-graph projection endpoint.

Covers authz (fail-closed admin key), the failover-chain edges that answer
"why did work move from A to B", state/checkpoint pinning edges, and the
summary contract - all as a pure PROJECTION over authoritative stores.
"""
import pytest

from invincible.core.continuity import ContinuityEngine
from invincible.core.run_store import RunStore
from invincible.main import app

ADMIN = {"Authorization": "Bearer admin-secret"}
GATEWAY = {"Authorization": "Bearer test-gateway-key"}


@pytest.fixture
async def graph_stack(client, pg_engine, monkeypatch):
    monkeypatch.setenv("INVINCIBLE_ADMIN_KEY", "admin-secret")
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
                     attempt=1):
    import time as _time

    await runs.record(
        {
            "request_id": request_id,
            "session_id": "default",
            "provider_name": provider,
            "model_id": f"{provider}-model",
            "attempt_index": attempt,
            "outcome": outcome,
            "error_class": "500" if outcome != "ok" else None,
            "started_at": _time.time(),
            "finished_at": _time.time(),
        }
    )


async def test_graph_without_any_credential_is_401(client, monkeypatch):
    """Phase 2 dual-realm: with the admin key unset the user realm decides
    - and with the gateway key also unset, fail-open applies (anonymous
    local principal, scoped view) rather than 503."""
    monkeypatch.delenv("INVINCIBLE_ADMIN_KEY", raising=False)
    monkeypatch.setenv("GATEWAY_API_KEY", "test-gateway-key")
    resp = await client.get("/api/v1/sessions/default/graph")
    assert resp.status_code == 401


async def test_graph_accepts_gateway_key_as_scoped_user(client, graph_stack):
    """Phase 2: a user principal gets the projection for ITS OWN session;
    an unknown-to-it string is indistinguishable from a nonexistent one."""
    resp = await client.get("/api/v1/sessions/ghost/graph", headers=GATEWAY)
    assert resp.status_code == 200
    data = resp.json()
    assert data["known"] is False
    assert data["nodes"] == []


async def test_graph_admin_still_sees_any_session(client, graph_stack):
    resp = await client.get("/api/v1/sessions/ghost/graph", headers=ADMIN)
    assert resp.status_code == 200
    assert resp.json()["known"] is False


async def test_empty_session_projection_shape(client, graph_stack):
    resp = await client.get("/api/v1/sessions/ghost/graph", headers=ADMIN)
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == "ghost"
    assert data["known"] is False
    assert data["nodes"] == [] and data["edges"] == []
    assert data["summary"]["attempts"] == 0


async def test_failover_chain_edges_answer_the_core_question(
    client, graph_stack
):
    """One request_id, three attempts alpha->beta->gamma: the projection
    must show WHY work moved (failover_from chain) and where it landed."""
    runs, _ = graph_stack
    await record_run(runs, "req-1", "failover", provider="alpha", attempt=1)
    await record_run(runs, "req-1", "failover", provider="beta", attempt=2)
    await record_run(runs, "req-1", "ok", provider="gamma", attempt=3)

    resp = await client.get("/api/v1/sessions/default/graph", headers=ADMIN)
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


async def test_separate_requests_are_followed_by_not_failover(client, graph_stack):
    runs, _ = graph_stack
    await record_run(runs, "req-a", "ok", provider="alpha")
    await record_run(runs, "req-b", "ok", provider="beta")
    resp = await client.get("/api/v1/sessions/default/graph", headers=ADMIN)
    kinds = {(e["source"], e["target"]): e["kind"]
             for e in resp.json()["edges"] if e["target"].startswith("run:")
             and e["source"].startswith("run:")}
    assert kinds[("run:1", "run:2")] == "followed_by"


async def test_state_versions_and_checkpoint_pins(client, graph_stack):
    _, engine = graph_stack
    await engine.set_state("default", {"through": 5}, actor="mcp:tss")
    await engine.set_state("default", {"through": 37}, actor="mcp:tss")
    cp = await engine.create_checkpoint("default", note="through 37",
                                        actor="mcp:checkpoint_create")

    resp = await client.get("/api/v1/sessions/default/graph", headers=ADMIN)
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


async def test_interruption_note_surfaces_in_summary(client, graph_stack):
    runs, engine = graph_stack
    await engine.set_state("default", {"next": 6}, actor="mcp:x")
    await engine.create_checkpoint("default", note="before resume",
                                   actor="mcp")
    await record_run(runs, "req-x", "error", provider="groq")

    resp = await client.get("/api/v1/sessions/default/graph", headers=ADMIN)
    summary = resp.json()["summary"]
    assert summary["interruption_note"]
    assert "'groq'" in summary["interruption_note"]


async def test_turn_nodes_project_from_normalized_storage(client, graph_stack):
    from invincible.main import app as fastapi_app

    await fastapi_app.state.sessions.append("default", [
        {"role": "user", "content": "count please"},
        {"role": "assistant", "content": "1 2 3"},
    ])
    resp = await client.get("/api/v1/sessions/default/graph", headers=ADMIN)
    data = resp.json()
    assert data["known"] is True
    turn_nodes = [n for n in data["nodes"] if n["kind"] == "turn"]
    assert len(turn_nodes) == 1
    assert turn_nodes[0]["message_count"] == 2
    assert any(e for e in data["edges"]
               if e == {"source": "session", "target": "turn:0",
                        "kind": "contains"})
