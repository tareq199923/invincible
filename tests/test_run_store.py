# tests/test_run_store.py
"""RunStore round-trips and Router recording-hook integration (Phase 13.5)."""
import httpx
import pytest

from invincible.core.provider_registry import ProviderRegistry
from invincible.core.router import AllProvidersFailedError, Router
from invincible.core.run_store import RunStore
from invincible.core.session_store import SessionStore
from tests.conftest import default_providers, provider_body

MESSAGES = [{"role": "user", "content": "hi"}]


async def make_store():
    store = SessionStore(db_path=":memory:")
    await store.init()
    runs = RunStore(shared=store)
    await runs.init()
    return store, runs


async def test_record_and_recent_roundtrip():
    store, runs = await make_store()
    try:
        await runs.record(
            {
                "request_id": "req-1",
                "session_id": "s-1",
                "provider_name": "alpha",
                "model_id": "m-a",
                "attempt_index": 1,
                "outcome": "failover",
                "error_class": "500",
                "started_at": 100.0,
                "finished_at": 101.0,
                "meta": {"reason": "server_error"},
            }
        )
        await runs.record(
            {
                "request_id": "req-1",
                "session_id": "s-1",
                "provider_name": "beta",
                "model_id": "m-b",
                "attempt_index": 2,
                "outcome": "ok",
                "started_at": 101.0,
                "finished_at": 102.0,
            }
        )
        rows = await runs.recent()
        assert len(rows) == 2
        assert rows[0]["provider_name"] == "beta" and rows[0]["outcome"] == "ok"
        assert rows[0]["meta"] is None
        # Newest first, meta JSON decoded.
        failover_row = rows[1]
        assert failover_row["outcome"] == "failover"
        assert failover_row["meta"] == {"reason": "server_error"}

        scoped = await runs.recent(session_id="other")
        assert scoped == []
    finally:
        # aiosqlite worker threads must be stopped on their own loop; an
        # unclosed connection GC'd later tears down against a dead loop
        # (the CI "Event loop is closed" warning).
        await runs.close()
        await store.close()


async def test_router_records_every_attempt_across_failover(tmp_path, monkeypatch):

    from tests.conftest import default_providers

    monkeypatch.setenv("ALPHA_API_KEY", "k-a")
    monkeypatch.setenv("BETA_API_KEY", "k-b")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.startswith("alpha"):
            return httpx.Response(500, json={"error": "down"})
        return httpx.Response(200, json=provider_body("beta"))

    registry = ProviderRegistry(
        file_path=str(tmp_path / "p.yaml"),
        seed_config={"providers": default_providers()},
    )
    store = SessionStore(db_path=":memory:")
    await store.init()
    runs = RunStore(shared=store)
    await runs.init()

    router = Router(
        transport=httpx.MockTransport(handler),
        registry=registry,
        run_recorder=runs.record,
    )
    result = await router.route_request(MESSAGES, session_id="sess-9")
    assert result["choices"][0]["message"]["role"] == "assistant"

    rows = await runs.recent(session_id="sess-9")
    outcomes = [(r["provider_name"], r["outcome"], r["error_class"]) for r in rows]
    assert list(reversed(outcomes)) == [
        ("alpha", "failover", "500"),
        ("beta", "ok", None),
    ]
    # Both attempts share one request id; indexes are 1-based.
    assert rows[0]["request_id"] == rows[1]["request_id"]
    assert {r["attempt_index"] for r in rows} == {1, 2}
    await router.close()
    await store.close()


async def test_router_without_recorder_still_serves(monkeypatch):
    """run_recorder=None is the legacy no-recording path.

    Legacy mode loads the packaged providers.yaml, so at least one provider
    key must exist or every candidate is skipped (CI has no .env).
    """
    monkeypatch.setenv("ALPHA_API_KEY", "k-a")

    def handler(request):
        return httpx.Response(200, json=provider_body("alpha"))

    router = Router(transport=httpx.MockTransport(handler))
    try:
        result = await router.route_request(MESSAGES)
        assert result["model"]
    finally:
        await router.close()


async def test_recorder_failure_never_breaks_the_completion(monkeypatch):
    monkeypatch.setenv("ALPHA_API_KEY", "k-a")

    async def exploding(entry):
        raise RuntimeError("disk on fire")

    def handler(request):
        return httpx.Response(200, json=provider_body("alpha"))

    router = Router(
        transport=httpx.MockTransport(handler),
        run_recorder=exploding,
    )
    try:
        result = await router.route_request(MESSAGES)
        assert result["choices"][0]["message"]["role"] == "assistant"
    finally:
        await router.close()


async def test_pinned_unavailable_records_nothing_and_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("ALPHA_API_KEY", raising=False)
    recorded = []

    async def recorder(entry):
        recorded.append(entry)

    registry = ProviderRegistry(
        file_path=str(tmp_path / "p.yaml"),
        seed_config={"providers": default_providers()},
    )
    await registry.disable("alpha")
    await registry.set_routing(
        "pinned", pinned={"provider": "alpha", "model": "m"}
    )
    router = Router(registry=registry, run_recorder=recorder)
    try:
        with pytest.raises(
            AllProvidersFailedError, match="not configured or is disabled"
        ):
            await router.route_request(MESSAGES)
        # The pinned target was never attempted (disabled pre-selection).
        assert recorded == []
    finally:
        await router.close()
