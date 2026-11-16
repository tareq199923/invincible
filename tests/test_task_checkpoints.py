"""Durable metadata-only checkpoints for background-task tool calls."""

import asyncio
import logging

import pytest

from invincible.core.db import ensure_local_owner
from invincible.core.session_store import SessionStore
from invincible.core.task_checkpoints import (
    CheckpointOwnershipError,
    TaskCheckpointStore,
    _digest,
)


@pytest.fixture
async def checkpoint_scope(pg_engine):
    user_id, project_id = await ensure_local_owner(pg_engine)
    workflow_id = "checkpoint-test-session"
    session_pk = await SessionStore(pg_engine).resolve_or_create(
        workflow_id, user_id=user_id, project_id=project_id
    )
    return TaskCheckpointStore(pg_engine), user_id, session_pk, workflow_id


async def test_record_persists_metadata_only_and_history_is_ordered(
    checkpoint_scope,
):
    store, user_id, session_pk, workflow_id = checkpoint_scope

    first = await store.record(
        user_id=user_id,
        session_pk=session_pk,
        workflow_id=workflow_id,
        call_id="call-1",
        tool_name="read_file",
        args={"path": "/private/example.txt", "token": "not-stored"},
        result={"content": "private file contents"},
    )
    second = await store.record(
        user_id=user_id,
        session_pk=session_pk,
        workflow_id=workflow_id,
        call_id="call-2",
        tool_name="list_dir",
        args={"path": "/private"},
        result={"entries": ["secret.txt"]},
    )

    assert first["created"] is True
    assert second["created"] is True
    assert [first["seq"], second["seq"]] == [1, 2]
    assert first["args_hash"] != "/private/example.txt"
    assert first["result_bytes"] > 0
    assert "args" not in first and "result" not in first
    assert "/private/example.txt" not in repr(first)
    assert "private file contents" not in repr(first)

    history = await store.history(user_id, workflow_id)
    assert [row["call_id"] for row in history] == ["call-1", "call-2"]
    assert (await store.latest(user_id, workflow_id))["call_id"] == "call-2"


async def test_record_is_idempotent_for_a_repeated_call_id(checkpoint_scope):
    store, user_id, session_pk, workflow_id = checkpoint_scope
    kwargs = {
        "user_id": user_id,
        "session_pk": session_pk,
        "workflow_id": workflow_id,
        "call_id": "same-call",
        "tool_name": "read_file",
        "args": {"path": "first"},
        "result": {"content": "first"},
    }
    original = await store.record(**kwargs)
    repeated = await store.record(
        **{**kwargs, "args": {"path": "changed"}, "result": {"content": "changed"}}
    )

    assert original["created"] is True
    assert repeated["created"] is False
    assert repeated["id"] == original["id"]
    assert repeated["seq"] == original["seq"]
    assert repeated["args_hash"] == original["args_hash"]
    assert len(await store.history(user_id, workflow_id)) == 1


async def test_checkpoint_reads_and_writes_are_owner_scoped(checkpoint_scope):
    store, user_id, session_pk, workflow_id = checkpoint_scope
    await store.record(
        user_id=user_id,
        session_pk=session_pk,
        workflow_id=workflow_id,
        call_id="owned-call",
        tool_name="list_dir",
        args={},
        result={},
    )

    assert await store.latest(user_id + 999_999, workflow_id) is None
    assert await store.history(user_id + 999_999, workflow_id) == []
    with pytest.raises(CheckpointOwnershipError):
        await store.record(
            user_id=user_id + 999_999,
            session_pk=session_pk,
            workflow_id=workflow_id,
            call_id="foreign-call",
            tool_name="list_dir",
            args={},
            result={},
        )


async def test_concurrent_records_get_distinct_dense_sequences(checkpoint_scope):
    store, user_id, session_pk, workflow_id = checkpoint_scope

    async def record(call_id):
        return await store.record(
            user_id=user_id,
            session_pk=session_pk,
            workflow_id=workflow_id,
            call_id=call_id,
            tool_name="list_dir",
            args={"call": call_id},
            result={"ok": True},
        )

    rows = await asyncio.gather(record("parallel-a"), record("parallel-b"))
    assert sorted(row["seq"] for row in rows) == [1, 2]
    history = await store.history(user_id, workflow_id)
    assert [row["seq"] for row in history] == [1, 2]


async def test_identical_retry_is_idempotent_and_not_a_conflict(
    checkpoint_scope, caplog
):
    store, user_id, session_pk, workflow_id = checkpoint_scope
    kwargs = {
        "user_id": user_id,
        "session_pk": session_pk,
        "workflow_id": workflow_id,
        "call_id": "retry-call",
        "tool_name": "read_file",
        "args": {"path": "same"},
        "result": {"content": "same"},
    }
    original = await store.record(**kwargs)
    with caplog.at_level(
        logging.WARNING, logger="invincible.task_checkpoints"
    ):
        repeated = await store.record(**kwargs)

    assert original["created"] is True and original["conflict"] is False
    assert repeated["created"] is False
    assert repeated["conflict"] is False
    assert repeated["id"] == original["id"]
    assert "collision" not in caplog.text


async def test_reused_call_id_for_a_different_call_is_reported(
    checkpoint_scope, caplog
):
    store, user_id, session_pk, workflow_id = checkpoint_scope
    original = await store.record(
        user_id=user_id,
        session_pk=session_pk,
        workflow_id=workflow_id,
        call_id="reused-id",
        tool_name="read_file",
        args={"path": "/private/first.txt"},
        result={"content": "first"},
    )
    with caplog.at_level(
        logging.WARNING, logger="invincible.task_checkpoints"
    ):
        collided = await store.record(
            user_id=user_id,
            session_pk=session_pk,
            workflow_id=workflow_id,
            call_id="reused-id",
            tool_name="list_dir",
            args={"path": "/private/second"},
            result={"entries": []},
        )

    # Still idempotent: nothing new is stored and the original row wins...
    assert collided["created"] is False
    assert collided["id"] == original["id"]
    assert collided["tool_name"] == "read_file"
    assert len(await store.history(user_id, workflow_id)) == 1
    # ...but the dropped call is no longer silent.
    assert collided["conflict"] is True
    assert "collision" in caplog.text
    assert "list_dir" in caplog.text
    # Metadata only: no arguments or results in the log.
    assert "/private" not in caplog.text


def test_digest_is_stable_for_ordinary_values():
    assert _digest({"b": 1, "a": [1, 2]}) == _digest({"a": [1, 2], "b": 1})
    assert _digest({"a": 1}) != _digest({"a": 2})


def test_digest_survives_extremely_deep_nesting():
    deep: list = []
    for _ in range(100_000):
        deep = [deep]

    digest, size = _digest(deep)

    assert len(digest) == 64
    assert size > 0
    assert _digest(deep) == (digest, size)


async def test_record_survives_extremely_deep_arguments(checkpoint_scope):
    store, user_id, session_pk, workflow_id = checkpoint_scope
    deep: list = []
    for _ in range(100_000):
        deep = [deep]

    row = await store.record(
        user_id=user_id,
        session_pk=session_pk,
        workflow_id=workflow_id,
        call_id="deep-call",
        tool_name="read_file",
        args={"nested": deep},
        result={"ok": True},
    )

    assert row["created"] is True
    assert len(row["args_hash"]) == 64
