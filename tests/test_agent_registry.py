import asyncio

import pytest

from invincible.core.agent_registry import AgentRegistry


class FakeClock:
    """Deterministic wall clock - the registry only ever reads it."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def registry():
    clock = FakeClock()
    return AgentRegistry(clock=clock), clock


async def test_heartbeat_marks_online_and_ttl_expires(registry):
    reg, clock = registry
    assert not reg.online(42)
    reg.heartbeat(42)
    assert reg.online(42)
    clock.advance(61)  # past AGENT_ONLINE_TTL_SECONDS (60)
    assert not reg.online(42)


async def test_dispatch_poll_result_round_trip(registry):
    """The core journey: dispatch -> long-poll hands the job out ->
    submit_result resolves the awaiting dispatcher."""
    reg, _ = registry

    async def dispatcher():
        return await reg.dispatch(
            42, "execute_bash", {"command": "echo hi", "timeout": 30.0},
            timeout=5,
        )

    async def agent():
        job = await reg.poll(42, hold=1)
        assert job is not None
        assert job["type"] == "execute_bash"
        assert job["args"]["command"] == "echo hi"
        assert reg.submit_result(
            42, job["job_id"], {"stdout": "hi", "returncode": 0}
        )

    result, _ = await asyncio.gather(dispatcher(), agent())
    assert result == {"stdout": "hi", "returncode": 0}


async def test_poll_with_no_work_returns_none(registry):
    reg, _ = registry
    assert await reg.poll(7, hold=0.01) is None


async def test_queue_is_fifo_per_user(registry):
    reg, _ = registry
    # Stage two dispatches concurrently (each runs to its await); the
    # queue order - not result delivery - is what's under test.
    tasks = [
        asyncio.ensure_future(
            reg.dispatch(42, "read_file", {"path": path}, timeout=5)
        )
        for path in ("a", "b")
    ]
    await asyncio.sleep(0.01)  # both have staged + parked on their future
    first = await reg.poll(42, hold=0.01)
    second = await reg.poll(42, hold=0.01)
    assert first["args"]["path"] == "a"
    assert second["args"]["path"] == "b"
    for task in tasks:  # never resolved: time them out cleanly
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_timeout_returns_agent_timeout_and_drops_queued_job(registry):
    """A dispatched job nobody picks up times out, AND a late poll must
    not receive the stale job."""
    reg, clock = registry
    result = await reg.dispatch(42, "execute_bash", {"command": "x"},
                                timeout=0.05)
    assert result["status"] == "agent_timeout"
    assert "may still have been executed" in result["message"]
    # The queued copy is gone - late polls get nothing.
    assert await reg.poll(42, hold=0.01) is None


async def test_submit_result_after_deadline_is_refused(registry):
    reg, clock = registry

    async def dispatcher():
        # dispatch returns agent_timeout (never raises) once the real
        # 0.2s wait expires with the future unresolved.
        return await reg.dispatch(42, "read_file", {"path": "a"}, timeout=0.2)

    dispatched = asyncio.ensure_future(dispatcher())
    job = await reg.poll(42, hold=1)
    clock.advance(2)  # now past the job's deadline (fake clock)
    assert reg.submit_result(42, job["job_id"], {"status": "read"}) is False
    result = await dispatched
    assert result["status"] == "agent_timeout"


async def test_job_id_is_single_use(registry):
    reg, _ = registry
    # consume a completed job's id, then replay it
    async def dispatcher():
        return await reg.dispatch(42, "read_file", {"path": "a"}, timeout=5)

    async def agent():
        job = await reg.poll(42, hold=1)
        assert reg.submit_result(42, job["job_id"], {"status": "read"})
        # replay of a resolved job_id: refused, indistinguishable
        assert not reg.submit_result(42, job["job_id"], {"status": "evil"})

    result, _ = await asyncio.gather(dispatcher(), agent())
    assert result == {"status": "read"}


async def test_cross_user_result_is_refused(registry):
    """User 1 cannot resolve user 42's job even with the right id."""
    reg, _ = registry

    async def dispatcher():
        return await reg.dispatch(42, "read_file", {"path": "a"}, timeout=1)

    async def attacker():
        job = await reg.poll(42, hold=1)  # 42's own agent picks it up
        assert job is not None
        assert not reg.submit_result(1, job["job_id"], {"status": "evil"})
        # the legitimate owner can still resolve it
        assert reg.submit_result(42, job["job_id"], {"status": "read"})

    result, _ = await asyncio.gather(dispatcher(), attacker())
    assert result == {"status": "read"}


async def test_unknown_job_id_is_refused(registry):
    reg, _ = registry
    assert not reg.submit_result(42, "no-such-job", {})


async def test_dispatch_wakes_a_held_poll(registry):
    reg, _ = registry

    async def holder():
        # starts holding before any work exists
        return await reg.poll(42, hold=5)

    held = asyncio.ensure_future(holder())
    await asyncio.sleep(0.01)  # let the poll actually park
    asyncio.ensure_future(
        reg.dispatch(42, "read_file", {"path": "a"}, timeout=5)
    )
    job = await asyncio.wait_for(held, timeout=1)
    assert job is not None
    assert job["args"]["path"] == "a"


async def test_dispatch_timeout_leaves_no_orphan_future(registry):
    reg, _ = registry
    await reg.dispatch(42, "execute_bash", {"command": "x"}, timeout=0.05)
    # internal bookkeeping cleaned up: the futures map is empty again
    assert not reg._futures.get(42, {})
    assert not reg._jobs
