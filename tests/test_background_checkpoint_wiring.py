"""Background-task checkpoint wiring (Step 3).

What ``run_agent_turn`` and the background runner do with the optional
``checkpointer`` callback. Database-free: the router, session store and
tool boundary are faked and the callback is a recording fake. The
PostgreSQL behaviour of the store itself lives in
tests/test_task_checkpoints.py.
"""
import inspect
import logging
from types import SimpleNamespace

import invincible.core.background_tasks as bt
import invincible.core.tool_executor as te
from invincible.core.chat_service import PreparedChat
from invincible.core.principal import Principal
from invincible.core.task_checkpoints import _digest
from invincible.core.webchat_agent import ApprovalWaiter, run_agent_turn

PRINCIPAL = Principal(user_id=1, project_id=1, kind="session")


def _call(call_id, name, args):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": __import__("json").dumps(args)},
    }


def _tool_message(calls):
    return {"choices": [{"message": {
        "role": "assistant", "content": None, "tool_calls": calls}}]}


def _text_message(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


class FakeRouter:
    """Serves each scripted response in order (the last one repeats)."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.served = 0

    async def route_request_detailed(self, *args, **kwargs):
        response = self.responses[min(self.served, len(self.responses) - 1)]
        self.served += 1
        return response, {
            "request_id": f"request-{self.served}",
            "provider_name": "test",
            "model_id": "test-model",
            "attempts": 1,
        }


class FakeSessions:
    async def append(self, session_id, messages, **kwargs):
        pass


class Recorder:
    """A checkpoint callback that records calls, or fails on demand."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("db down: /secret/path")


async def _run(router, checkpointer=None):
    prepared = PreparedChat(
        session_id="bg", session_pk=1,
        full_messages=[{"role": "user", "content": "go"}],
        to_persist=[{"role": "user", "content": "go"}],
    )
    extra = {} if checkpointer is None else {"checkpointer": checkpointer}
    events = []
    async for name, data in run_agent_turn(
        prepared, model=None, router=router, sessions=FakeSessions(),
        memory=None, runs_store=None, principal=PRINCIPAL, mode="plan",
        pending_store=None, executor=None, waiter=ApprovalWaiter(),
        **extra,
    ):
        events.append((name, data))
    return events


def _fake_read(monkeypatch, results):
    """Patch the local read boundary; ``results`` maps path -> result."""

    async def fake_read(path, *args, **kwargs):
        return results[path]

    monkeypatch.setattr(te, "read_file", fake_read)


async def test_successful_tool_call_is_checkpointed(monkeypatch):
    _fake_read(monkeypatch, {
        "a.txt": {"status": "read", "path": "a.txt", "content": "hello"}})
    recorder = Recorder()
    events = await _run(
        FakeRouter(
            _tool_message([_call("c1", "read_file", {"path": "a.txt"})]),
            _text_message("done"),
        ),
        checkpointer=recorder,
    )

    assert [d for n, d in events if n == "done"]
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["call_id"] == "c1"
    assert call["name"] == "read_file"
    assert call["args"] == {"path": "a.txt"}


async def test_failed_tool_call_is_not_checkpointed(monkeypatch):
    _fake_read(monkeypatch, {
        "a.txt": {"status": "read", "path": "a.txt", "content": "hello"},
        "bad.txt": {"status": "error", "error": "boom"},
    })
    recorder = Recorder()
    await _run(
        FakeRouter(
            _tool_message([
                _call("good", "read_file", {"path": "a.txt"}),
                _call("bad", "read_file", {"path": "bad.txt"}),
            ]),
            _text_message("done"),
        ),
        checkpointer=recorder,
    )

    assert [c["call_id"] for c in recorder.calls] == ["good"]


async def test_unknown_tool_is_not_checkpointed():
    recorder = Recorder()
    await _run(
        FakeRouter(
            _tool_message([_call("u1", "no_such_tool", {})]),
            _text_message("done"),
        ),
        checkpointer=recorder,
    )

    assert recorder.calls == []


async def test_checkpoint_failure_does_not_stop_the_task(
    monkeypatch, caplog
):
    _fake_read(monkeypatch, {
        "a.txt": {"status": "read", "path": "a.txt", "content": "hello"}})
    recorder = Recorder(fail=True)
    with caplog.at_level(logging.WARNING):
        events = await _run(
            FakeRouter(
                _tool_message([_call("c1", "read_file", {"path": "a.txt"})]),
                _text_message("finished anyway"),
            ),
            checkpointer=recorder,
        )

    done = [d for n, d in events if n == "done"]
    assert len(done) == 1
    assert done[0]["text"] == "finished anyway"
    assert len(recorder.calls) == 1
    # Only the tool name and the error class are logged - never the
    # exception message, arguments, or results.
    assert "RuntimeError" in caplog.text
    assert "/secret/path" not in caplog.text
    assert "db down" not in caplog.text


async def test_run_without_a_checkpointer_still_works(monkeypatch):
    _fake_read(monkeypatch, {
        "a.txt": {"status": "read", "path": "a.txt", "content": "hello"}})
    events = await _run(
        FakeRouter(
            _tool_message([_call("c1", "read_file", {"path": "a.txt"})]),
            _text_message("done"),
        ),
    )

    assert [d for n, d in events if n == "done"]


def test_interactive_chat_never_passes_a_checkpointer():
    import invincible.endpoints.chat as chat

    assert (
        inspect.signature(run_agent_turn).parameters["checkpointer"].default
        is None
    )
    assert "checkpointer" not in inspect.getsource(chat)


def test_make_checkpointer_needs_an_engine_and_a_session():
    prepared = SimpleNamespace(session_pk=7)

    assert bt._make_checkpointer(None, PRINCIPAL, prepared, "wf") is None
    assert bt._make_checkpointer(
        object(), PRINCIPAL, SimpleNamespace(session_pk=None), "wf"
    ) is None
    assert bt._make_checkpointer(
        object(), PRINCIPAL, SimpleNamespace(), "wf") is None


async def test_checkpointer_forwards_to_the_store_under_the_principal(
    monkeypatch,
):
    seen = {}

    class FakeStore:
        def __init__(self, engine):
            seen["engine"] = engine

        async def record(self, **kwargs):
            seen["record"] = kwargs

    monkeypatch.setattr(bt, "TaskCheckpointStore", FakeStore)
    engine = object()
    checkpointer = bt._make_checkpointer(
        engine, PRINCIPAL, SimpleNamespace(session_pk=7), "wf-1")

    await checkpointer(
        call_id="c1", name="read_file", args={"a": 1}, result={"b": 2})

    assert seen["engine"] is engine
    assert seen["record"] == {
        "user_id": 1, "session_pk": 7, "workflow_id": "wf-1",
        "call_id": "c1", "tool_name": "read_file",
        "args": {"a": 1}, "result": {"b": 2},
    }


async def _execute(monkeypatch, engine):
    captured = {}

    async def fake_prepare(*args, **kwargs):
        return SimpleNamespace(session_pk=7)

    async def fake_turn(prepared, **kwargs):
        captured.update(kwargs)
        yield "done", {"text": "ok"}

    monkeypatch.setattr(bt, "prepare_chat", fake_prepare)
    monkeypatch.setattr(bt, "run_agent_turn", fake_turn)
    text = await bt._execute_task(
        task="t", model=None, principal=PRINCIPAL, workflow_id="wf",
        router=None, sessions=None, memory=None, runs_store=None,
        pending_store=None, executor=None, waiter=None, engine=engine,
    )
    return text, captured


async def test_background_execution_passes_a_checkpointer(monkeypatch):
    text, captured = await _execute(monkeypatch, engine=object())

    assert text == "ok"
    assert callable(captured["checkpointer"])


async def test_background_execution_without_an_engine_has_no_checkpointer(
    monkeypatch,
):
    text, captured = await _execute(monkeypatch, engine=None)

    assert text == "ok"
    assert captured["checkpointer"] is None


def test_digest_survives_unusual_values():
    circular: dict = {}
    circular["self"] = circular
    values = (
        circular,
        {1: "a", "b": 2},          # mixed key types break sort_keys
        {"s": {1, 2}},             # a set is not JSON
        b"raw bytes",
        object(),
        "lone surrogate \ud800",
        None,
    )
    for value in values:
        digest, size = _digest(value)
        assert len(digest) == 64
        assert size >= 0


def test_digest_is_stable_and_never_the_raw_value():
    first, _ = _digest({"path": "/private/x", "n": 1})
    second, _ = _digest({"n": 1, "path": "/private/x"})

    assert first == second
    assert "/private/x" not in first
