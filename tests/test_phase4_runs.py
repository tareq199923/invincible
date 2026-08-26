# tests/test_phase4_runs.py
"""Phase 4 run accounting + reactive failover checkpoints.

- Token columns on ``runs``: real upstream usage when the provider reports
  it, flagged chars/4 estimates otherwise (non-streaming body estimate;
  streaming input at open + endpoint-attached output).
- Reactive checkpoints: one pre-switch snapshot per request, only when a
  task_state exists, fired through the injected hook - never breaking
  routing.
"""

import httpx
import pytest

from invincible.core.continuity import ContinuityEngine
from invincible.core.provider_registry import ProviderRegistry
from invincible.core.router import Router
from invincible.core.run_store import RunStore
from tests.conftest import (
    default_providers,
    make_transport,
    provider_body,
    sse_body,
    stream_chunk,
)


@pytest.fixture
async def runs_store(pg_engine):
    store = RunStore(engine=pg_engine)
    await store.init()
    yield store


@pytest.fixture
async def continuity(pg_engine, runs_store):
    engine = ContinuityEngine(engine=pg_engine, runs=runs_store)
    await engine.init()
    yield engine


def make_router_with_recorder(
    monkeypatch, tmp_path, runs_store, handlers
):
    """Router in registry mode with fake keys - same shape as
    test_run_store.py's failover test."""
    for key in ("ALPHA_API_KEY", "BETA_API_KEY", "GAMMA_API_KEY"):
        monkeypatch.setenv(key, "test-key")
    registry = ProviderRegistry(
        file_path=str(tmp_path / "p.yaml"),
        seed_config={"providers": default_providers()},
    )
    return Router(
        transport=make_transport(handlers),
        registry=registry,
        run_recorder=runs_store.record,
    )


async def latest_run(runs_store):
    rows = await runs_store.recent(limit=5)
    return rows[0] if rows else None


# --- usage persistence ----------------------------------------------------------


@pytest.mark.asyncio
async def test_nonstreaming_records_real_usage(
    runs_store, monkeypatch, tmp_path
):
    body = provider_body("alpha")
    body["usage"] = {"prompt_tokens": 123, "completion_tokens": 456}
    router = make_router_with_recorder(
        monkeypatch, tmp_path, runs_store,
        {"alpha.example.com": httpx.Response(200, json=body)},
    )
    result, _info = await router.route_request_detailed(
        [{"role": "user", "content": "hi"}])
    assert result["choices"][0]["message"]["content"] == "hello"
    run = await latest_run(runs_store)
    assert run["outcome"] == "ok"
    assert run["input_tokens"] == 123
    assert run["output_tokens"] == 456
    # Real counts: no estimation flag in meta.
    assert not (run.get("meta") or {}).get("usage_estimated")


@pytest.mark.asyncio
async def test_nonstreaming_estimates_when_usage_absent(
    runs_store, monkeypatch, tmp_path
):
    router = make_router_with_recorder(
        monkeypatch, tmp_path, runs_store,
        {"alpha.example.com": httpx.Response(
            200, json=provider_body("alpha", content="word " * 100))},
    )
    await router.route_request_detailed([{"role": "user", "content": "hi"}])
    run = await latest_run(runs_store)
    assert run["input_tokens"] is None  # no real input count reported
    assert (run["output_tokens"] or 0) > 0
    assert (run.get("meta") or {}).get("usage_estimated") is True


@pytest.mark.asyncio
async def test_streaming_records_input_estimate_and_attaches_output(
    runs_store, monkeypatch, tmp_path
):
    chunks = [
        stream_chunk("alpha", {"role": "assistant", "content": "partial "}),
        stream_chunk("alpha", {"content": "reply"}),
        stream_chunk("alpha", {}, finish_reason="stop"),
    ]
    router = make_router_with_recorder(
        monkeypatch, tmp_path, runs_store,
        {"alpha.example.com": httpx.Response(200, content=sse_body(*chunks))},
    )
    _first, tail = await router.stream_open(
        [{"role": "user", "content": "hello stream"}],
        session_id="usage-s",
    )
    # The endpoint accumulates from BOTH the first chunk and the tail.
    collected = ""

    def _take(chunk):
        nonlocal collected
        for choice in chunk.get("choices") or []:
            piece = (choice.get("delta") or {}).get("content")
            if piece:
                collected += piece

    if _first is not None:
        _take(_first)
    async for chunk in tail:
        _take(chunk)
    assert collected == "partial reply"

    run = await latest_run(runs_store)
    assert run["outcome"] == "ok"
    # Input estimate recorded at stream-open...
    assert (run["input_tokens"] or 0) > 0
    assert (run.get("meta") or {}).get("usage_estimated") is True

    # ...endpoint attaches the output estimate afterwards.
    assert await runs_store.attach_output(
        request_id=run["request_id"], output_tokens=7, estimated=True,
    ) is True
    updated = await latest_run(runs_store)
    assert updated["output_tokens"] == 7
    assert updated["input_tokens"] == run["input_tokens"]
    assert (updated.get("meta") or {}).get("usage_estimated") is True


@pytest.mark.asyncio
async def test_attach_output_without_ok_row_is_noop(runs_store):
    assert await runs_store.attach_output(
        request_id="never-recorded", output_tokens=10) is False


# --- reactive failover checkpoints ---------------------------------------------


@pytest.mark.asyncio
async def test_failover_creates_one_pre_switch_checkpoint(
    pg_engine, runs_store, continuity, monkeypatch, tmp_path
):
    """Two providers fail in sequence; exactly ONE snapshot lands before
    the winning attempt - and it names the first broken provider."""
    router = make_router_with_recorder(
        monkeypatch, tmp_path, runs_store,
        {
            "alpha.example.com": httpx.Response(429),
            "beta.example.com": httpx.Response(500),
            "gamma.example.com": httpx.Response(
                200, json=provider_body("gamma")),
        },
    )
    router.failover_hook = continuity.failover_hook()
    await continuity.set_state("ckpt-s", {"step": "deploy"}, actor="t")

    result, info = await router.route_request_detailed(
        [{"role": "user", "content": "go"}], session_id="ckpt-s")
    assert info["attempts"] == 3

    cps = await continuity.checkpoints("ckpt-s")
    assert len(cps) == 1
    note = cps[0]["note"]
    assert "pre-failover" in note and "alpha" in note and "429" in note
    assert cps[0]["state_version"] >= 1


@pytest.mark.asyncio
async def test_no_task_state_means_no_checkpoint(
    pg_engine, runs_store, continuity, monkeypatch, tmp_path
):
    router = make_router_with_recorder(
        monkeypatch, tmp_path, runs_store,
        {
            "alpha.example.com": httpx.Response(429),
            "beta.example.com": httpx.Response(
                200, json=provider_body("beta")),
        },
    )
    router.failover_hook = continuity.failover_hook()

    _, info = await router.route_request_detailed(
        [{"role": "user", "content": "go"}], session_id="ckpt-empty")
    assert info["attempts"] == 2
    assert await continuity.checkpoints("ckpt-empty") == []


@pytest.mark.asyncio
async def test_broken_hook_never_breaks_routing(
    runs_store, monkeypatch, tmp_path
):
    async def exploding_hook(**kwargs):
        raise RuntimeError("checkpoint backend down")

    router = make_router_with_recorder(
        monkeypatch, tmp_path, runs_store,
        {
            "alpha.example.com": httpx.Response(429),
            "beta.example.com": httpx.Response(
                200, json=provider_body("beta")),
        },
    )
    router.failover_hook = exploding_hook
    result, _info = await router.route_request_detailed(
        [{"role": "user", "content": "go"}])
    assert result["model"] == "beta-model"


@pytest.mark.asyncio
async def test_checkpoint_scopes_by_owning_session_pk(
    pg_engine, runs_store, continuity, monkeypatch, tmp_path
):
    """The Phase 2 isolation shape: two principals sharing a client string
    get independent checkpoint chains via their surrogate sessions."""
    from invincible.core.db import ensure_local_owner
    from invincible.core.session_store import SessionStore

    uid_a, pid_a = await ensure_local_owner(pg_engine)
    # A second owner sharing the same client string.
    import time

    from sqlalchemy import insert

    from invincible.core.db import projects, users

    async with pg_engine.begin() as conn:
        uid_b = (await conn.execute(
            insert(users).values(
                email="runs-b@example.com", created_at=time.time())
        )).inserted_primary_key[0]
        pid_b = (await conn.execute(
            insert(projects).values(
                user_id=uid_b, name="other", is_default=True,
                created_at=time.time())
        )).inserted_primary_key[0]

    sessions = SessionStore(engine=pg_engine)
    pk_a = await sessions.resolve_or_create(
        "shared-str", user_id=uid_a, project_id=pid_a)
    pk_b = await sessions.resolve_or_create(
        "shared-str", user_id=uid_b, project_id=pid_b)

    router = make_router_with_recorder(
        monkeypatch, tmp_path, runs_store,
        {
            "alpha.example.com": httpx.Response(429),
            "beta.example.com": httpx.Response(
                200, json=provider_body("beta")),
        },
    )
    router.failover_hook = continuity.failover_hook()
    await continuity.set_state(
        "shared-str", {"who": "b"}, actor="t", session_pk=pk_b)

    await router.route_request_detailed(
        [{"role": "user", "content": "go"}],
        session_id="shared-str", session_pk=pk_a)

    cps_a = await continuity.checkpoints("shared-str", session_pk=pk_a)
    cps_b = await continuity.checkpoints("shared-str", session_pk=pk_b)
    # A tracks no task state -> nothing checkpointed; B's chain untouched.
    assert cps_a == [] and cps_b == []
