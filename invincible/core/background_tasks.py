# invincible/core/background_tasks.py
"""Cookie-realm background agent tasks (plan mode only).

A logged-in user can start a task that runs on the server without a
browser attached. Every task goes through the SAME one-shot pipeline the
dashboard webchat uses - ``chat_service.prepare_chat`` followed by
``webchat_agent.run_agent_turn(mode="plan")``, hence the single
``router._iter_attempts`` failover loop - and persists its answer in a
chat session whose id is the workflow id.

Layering: pure ``core/`` business logic. The registry, stores, executor,
approval waiter, and the pre-resolved ``byok`` tuple are injected by the
endpoint. No FastAPI, and no ``endpoints/`` imports.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from invincible.core import tool_executor
from invincible.core.chat_service import ChatError, prepare_chat
from invincible.core.harness_events import HarnessEventType
from invincible.core.principal import Principal
from invincible.core.webchat_agent import run_agent_turn

logger = logging.getLogger("invincible.background_tasks")

# Wall-clock cap for one background task. The model can legitimately
# spend a while in plan mode (many read-only tool calls), so the default
# is deliberately generous; tests monkeypatch it to a tiny value.
BACKGROUND_TASK_TIMEOUT_SECONDS = 30 * 60

# Per-user concurrency cap enforced by the endpoint before start().
# In-process by design, like the agent registry and approval waiter.
MAX_RUNNING_PER_USER = 2

_PREVIEW_CHARS = 200
_ERROR_CHARS = 200


def new_workflow_id() -> str:
    """Server-minted workflow id, also used as the chat session id."""
    return f"task-{uuid.uuid4().hex[:12]}"


class BackgroundTaskError(Exception):
    """A task-level failure with a client-safe message."""


class BackgroundTaskRegistry:
    """Strong references to running tasks, keyed by owner user id.

    ``running_count`` ignores finished tasks; the done callback prunes
    them from both the per-user set and the outer mapping. ``shutdown``
    cancels everything and waits, so callers can flush persisted events
    afterwards.
    """

    MAX_RUNNING_PER_USER = MAX_RUNNING_PER_USER

    def __init__(self) -> None:
        self._tasks: dict[int, set[asyncio.Task]] = {}

    def start(self, user_id: int, coro: Awaitable[Any]) -> asyncio.Task:
        """Schedule one runner and keep a strong reference until it ends."""
        task = asyncio.create_task(coro)
        self._tasks.setdefault(user_id, set()).add(task)
        task.add_done_callback(lambda done: self._forget(user_id, done))
        return task

    def _forget(self, user_id: int, task: asyncio.Task) -> None:
        tasks = self._tasks.get(user_id)
        if tasks is None:
            return
        tasks.discard(task)
        if not tasks:
            self._tasks.pop(user_id, None)

    def running_count(self, user_id: int) -> int:
        """Live (not done) tasks for one user; foreign users are unseen."""
        tasks = self._tasks.get(user_id)
        if not tasks:
            return 0
        return sum(1 for task in tasks if not task.done())

    async def shutdown(self) -> None:
        """Cancel every running task and wait for cancellation to settle."""
        tasks = [
            task
            for group in list(self._tasks.values())
            for task in list(group)
            if not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()


def _emit(bus, event_type: HarnessEventType, **fields: Any) -> None:
    if bus is not None:
        bus.emit(event_type, **fields)


def _short(text: object) -> str:
    rendered = str(text or "").strip() or "task failed"
    return rendered[:_ERROR_CHARS]


def _chat_error_message(exc: ChatError) -> str:
    """Client-safe message from a ChatError body (never a traceback)."""
    body = exc.body if isinstance(exc.body, dict) else {}
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    return _short(error.get("message") or "task failed")


def _event_message(data: object, fallback: str) -> str:
    if isinstance(data, dict):
        return _short(data.get("message") or fallback)
    return _short(fallback)


async def _execute_task(
    *,
    task: str,
    model: str | None,
    principal: Principal,
    workflow_id: str,
    router,
    sessions,
    memory,
    runs_store,
    pending_store,
    executor,
    waiter,
    retrieval=None,
    continuity=None,
    engine=None,
    agent_platform: str | None = None,
    harness_bus=None,
    byok=None,
    audit: Callable | None = None,
) -> str:
    """Prepare the turn and consume ``run_agent_turn`` to completion."""
    prepared = await prepare_chat(
        [{"role": "user", "content": task}],
        principal=principal,
        session_id=workflow_id,
        model=model,
        sessions=sessions,
        retrieval=retrieval,
        continuity=continuity,
        byok=byok,
    )
    stream = run_agent_turn(
        prepared, model=model, router=router,
        sessions=sessions, memory=memory, runs_store=runs_store,
        principal=principal, mode="plan",
        pending_store=pending_store, executor=executor,
        waiter=waiter, audit=audit,
        retrieval=retrieval, continuity=continuity,
        engine=engine, agent_platform=agent_platform,
        bus=harness_bus,
    )
    try:
        async for event_name, data in stream:
            if event_name == "done":
                text = data.get("text") if isinstance(data, dict) else ""
                return str(text or "")
            if event_name == "error":
                raise BackgroundTaskError(
                    _event_message(data, "task failed"))
            if event_name == "approval":
                # Plan mode offers no mutating tools, so this should be
                # unreachable. If a model calls one anyway, deny the
                # staged action immediately (never wait for a browser)
                # and fail the workflow. confirm_action(approve=False)
                # pops the token, so it cannot be approved afterwards.
                token = (
                    data.get("token") if isinstance(data, dict) else None
                )
                if token and pending_store is not None:
                    await tool_executor.confirm_action(
                        pending_store, str(token), False,
                        requester_subject=principal.user_id,
                        executor=executor,
                    )
                raise BackgroundTaskError("approval not supported yet")
        raise BackgroundTaskError("task ended without a result")
    finally:
        # Close early (approval/error) so run_agent_turn's partial-turn
        # persistence runs now, not at loop shutdown.
        with contextlib.suppress(Exception):
            await stream.aclose()


async def run_background_task(
    *,
    task: str,
    model: str | None,
    principal: Principal,
    workflow_id: str,
    router,
    sessions,
    memory,
    runs_store,
    pending_store,
    executor,
    waiter,
    retrieval=None,
    continuity=None,
    engine=None,
    agent_platform: str | None = None,
    harness_bus=None,
    byok=None,
    audit: Callable | None = None,
) -> None:
    """Run one plan-mode task and emit its workflow events.

    The started event fires before preparation, so the dashboard shows
    the task immediately. Every terminal path emits exactly one
    ``workflow.completed`` or ``workflow.failed`` event; unexpected
    exceptions are logged and never escape silently.
    """
    base = {
        "workflow_id": workflow_id,
        "user_id": principal.user_id,
    }
    _emit(
        harness_bus, HarnessEventType.WORKFLOW_STARTED,
        **base, input_preview=task[:_PREVIEW_CHARS],
    )
    try:
        text = await asyncio.wait_for(
            _execute_task(
                task=task, model=model, principal=principal,
                workflow_id=workflow_id, router=router, sessions=sessions,
                memory=memory, runs_store=runs_store,
                pending_store=pending_store, executor=executor,
                waiter=waiter, retrieval=retrieval,
                continuity=continuity, engine=engine,
                agent_platform=agent_platform, harness_bus=harness_bus,
                byok=byok, audit=audit,
            ),
            timeout=BACKGROUND_TASK_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("Background task %s timed out", workflow_id)
        _emit(
            harness_bus, HarnessEventType.WORKFLOW_FAILED,
            **base, error="timed out",
        )
        return
    except asyncio.CancelledError:
        _emit(
            harness_bus, HarnessEventType.WORKFLOW_FAILED,
            **base, error="cancelled",
        )
        raise
    except ChatError as exc:
        # Preparation rejected the task (e.g. provider disconnected
        # between the endpoint pre-check and the runner). The message
        # is client-safe; a traceback would only be noise.
        message = _chat_error_message(exc)
        logger.warning(
            "Background task %s rejected: %s", workflow_id, message)
        _emit(
            harness_bus, HarnessEventType.WORKFLOW_FAILED,
            **base, error=message,
        )
        return
    except BackgroundTaskError as exc:
        message = _short(exc)
        logger.warning(
            "Background task %s failed: %s", workflow_id, message)
        _emit(
            harness_bus, HarnessEventType.WORKFLOW_FAILED,
            **base, error=message,
        )
        return
    except Exception as exc:  # noqa: BLE001 - logged, never silent
        logger.exception("Background task %s failed", workflow_id)
        _emit(
            harness_bus, HarnessEventType.WORKFLOW_FAILED,
            **base, error=_short(exc),
        )
        return
    _emit(
        harness_bus, HarnessEventType.WORKFLOW_COMPLETED,
        **base, output_preview=text[:_PREVIEW_CHARS],
    )
