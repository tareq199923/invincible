# invincible/core/background_tasks.py
"""Cookie-realm background agent tasks (plan, or manual with approvals).

A logged-in user can start a task that runs on the server without a
browser attached. Every task goes through the SAME one-shot pipeline the
dashboard webchat uses - ``chat_service.prepare_chat`` followed by
``webchat_agent.run_agent_turn`` (plan, or manual with a durable
approval seam) - hence the single ``router._iter_attempts`` failover
loop - and persists its answer in a chat session whose id is the
workflow id.

Layering: pure ``core/`` business logic. The registry, stores, executor,
approval store/waiter, and the pre-resolved ``byok`` tuple are injected
by the endpoint. No FastAPI, and no ``endpoints/`` imports.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from invincible.core import tool_executor
from invincible.core.chat_service import ChatError, prepare_chat
from invincible.core.harness_events import HarnessEventType
from invincible.core.principal import Principal
from invincible.core.task_checkpoints import TaskCheckpointStore
from invincible.core.webchat_agent import run_agent_turn

logger = logging.getLogger("invincible.background_tasks")

# Wall-clock cap for one background task. The model can legitimately
# spend a while in plan mode (many read-only tool calls), so the default
# is deliberately generous; tests monkeypatch it to a tiny value.
BACKGROUND_TASK_TIMEOUT_SECONDS = 30 * 60

# Per-user concurrency cap enforced by the endpoint before start().
# In-process by design, like the agent registry and approval waiter.
MAX_RUNNING_PER_USER = 2

# F35 D6: a background approval waits one hour, NOT
# ApprovalStore.DEFAULT_SUSPEND_TTL_SECONDS (24h). A suspended task
# counts against MAX_RUNNING_PER_USER (D8), so the longer default would
# lock a user out of their own queue for a day.
BACKGROUND_APPROVAL_TTL_SECONDS = 60 * 60

# F35 D6: the real approval window is bounded by the task clock, not the
# 1h row TTL. No approval may START an execution inside the last minute,
# so a 30s command plus the agent grace period always fits before the
# task's wall-clock timeout. The dashboard shows the earlier deadline;
# the adapter enforces it at stage and wait time.
APPROVAL_SAFETY_MARGIN_SECONDS = 60

_PREVIEW_CHARS = 200
_ERROR_CHARS = 200


def effective_approval_deadline(
    approval_deadline: float | None,
    task_started_at: float | None,
    *,
    task_timeout_seconds: float | None = None,
) -> float | None:
    """The earlier of the row deadline and the task-clock cutoff.

    ``None`` when neither bound is available. A known task start always
    yields a cutoff: the task timeout minus the safety margin.
    """
    if task_started_at is None:
        return approval_deadline
    timeout = (
        BACKGROUND_TASK_TIMEOUT_SECONDS
        if task_timeout_seconds is None
        else task_timeout_seconds
    )
    cutoff = task_started_at + timeout - APPROVAL_SAFETY_MARGIN_SECONDS
    if approval_deadline is None:
        return cutoff
    return min(approval_deadline, cutoff)



def new_workflow_id() -> str:
    """Server-minted workflow id, also used as the chat session id."""
    return f"task-{uuid.uuid4().hex[:12]}"


class BackgroundTaskError(Exception):
    """A task-level failure with a client-safe message."""


class DurableApprovalAdapter:
    """Slow-path approval seam for ONE background workflow (F35 B).

    Conforms to ``webchat_agent.DurableApprovalSource``: ``stage`` parks
    the prepared record in ``pending_actions`` (1h TTL), ``wait`` blocks
    on the shared in-process rendezvous, and ``resolve`` atomically
    claims the row. ``discard`` is the cleanup gate for every exit
    (cancel, timeout, error, completion): it removes any still-parked
    row so a late approval reads as an unknown token (D7).

    The rendezvous is registered at stage time, before the runner can
    yield the approval event; otherwise a dashboard that lists the row
    immediately could approve before the runner reaches ``wait``.

    The row TTL is only an upper bound. ``_task_deadline`` is the task
    clock minus ``APPROVAL_SAFETY_MARGIN_SECONDS``; ``window_open`` and
    the bounded wait keep any approval from starting an execution inside
    that final margin (D6).
    """

    def __init__(
        self, store, waiter, *, workflow_id: str, owner_subject: int,
        ttl_seconds: float = BACKGROUND_APPROVAL_TTL_SECONDS,
    ) -> None:
        self._store = store
        self._waiter = waiter
        self._workflow_id = workflow_id
        self._owner_subject = owner_subject
        self._ttl_seconds = ttl_seconds
        self._tokens: set[str] = set()
        self._task_deadline = (
            time.time() + BACKGROUND_TASK_TIMEOUT_SECONDS
            - APPROVAL_SAFETY_MARGIN_SECONDS
        )

    def window_open(self) -> bool:
        """Whether an approval may still start an execution."""
        return time.time() < self._task_deadline

    async def stage(self, record: dict) -> str:
        token = await self._store.suspend(
            workflow_id=self._workflow_id,
            action_type=record["type"],
            args=record["args"],
            owner_subject=self._owner_subject,
            ttl_seconds=self._ttl_seconds,
        )
        self._waiter.register(token, self._owner_subject, durable=True)
        self._tokens.add(token)
        return token

    async def wait(self, token: str) -> bool | None:
        remaining = self._task_deadline - time.time()
        if remaining <= 0:
            return None
        return await self._waiter.wait_decision(
            token, self._owner_subject,
            timeout=min(self._ttl_seconds, remaining),
            durable=True,
        )

    async def resolve(self, token: str, approve: bool) -> dict | None:
        self._tokens.discard(token)
        return await self._store.resolve(
            token, approve=approve,
            requester_subject=self._owner_subject)

    async def discard(self) -> None:
        for token in list(self._tokens):
            self._waiter.discard(token)
        self._tokens.clear()
        await self._store.purge_workflow(
            self._workflow_id, owner_subject=self._owner_subject)


class BackgroundTaskRegistry:
    """Strong references to running tasks, keyed by owner and workflow id.

    The nested mapping is the ownership boundary: a workflow id is only
    resolvable under the user it was started for, so a foreign id behaves
    exactly like an unknown one. ``running_count`` ignores finished tasks;
    the done callback prunes them from both levels. ``shutdown`` cancels
    everything and waits, so callers can flush persisted events
    afterwards.
    """

    MAX_RUNNING_PER_USER = MAX_RUNNING_PER_USER

    def __init__(self) -> None:
        self._tasks: dict[int, dict[str, asyncio.Task]] = {}

    def start(
        self, user_id: int, coro: Awaitable[Any], *, workflow_id: str
    ) -> asyncio.Task:
        """Schedule one runner and keep a strong reference until it ends."""
        task = asyncio.create_task(coro)
        self._tasks.setdefault(user_id, {})[workflow_id] = task
        task.add_done_callback(
            lambda done: self._forget(user_id, workflow_id, done))
        return task

    def _forget(
        self, user_id: int, workflow_id: str, task: asyncio.Task
    ) -> None:
        tasks = self._tasks.get(user_id)
        if tasks is None:
            return
        # Only prune if this exact task still owns the slot: a done
        # callback from an older task must never evict a newer one.
        if tasks.get(workflow_id) is task:
            tasks.pop(workflow_id, None)
        if not tasks:
            self._tasks.pop(user_id, None)

    def running_count(self, user_id: int) -> int:
        """Live (not done) tasks for one user; foreign users are unseen."""
        tasks = self._tasks.get(user_id)
        if not tasks:
            return 0
        return sum(1 for task in tasks.values() if not task.done())

    def is_running(self, user_id: int, workflow_id: str) -> bool:
        """Whether this user's workflow is still live."""
        tasks = self._tasks.get(user_id)
        task = tasks.get(workflow_id) if tasks else None
        return task is not None and not task.done()

    def cancel(self, user_id: int, workflow_id: str) -> bool:
        """Cancel one owned running workflow; never touches another user's.

        Cancellation flows through the runner's existing
        ``asyncio.CancelledError`` branch, which emits the single
        ``workflow.failed`` / ``error="cancelled"`` event. This method
        deliberately emits nothing itself.
        """
        tasks = self._tasks.get(user_id)
        task = tasks.get(workflow_id) if tasks else None
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def shutdown(self) -> None:
        """Cancel every running task and wait for cancellation to settle."""
        tasks = [
            task
            for group in list(self._tasks.values())
            for task in list(group.values())
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


def _event_field(data: object, key: str) -> str:
    """Metadata-only string field from a runner event (never args)."""
    if isinstance(data, dict):
        return str(data.get(key) or "")
    return ""


def _make_checkpointer(engine, principal: Principal, prepared, workflow_id):
    """Build the per-task checkpoint callback, or None when unavailable.

    Needs a database engine and a session resolved under the principal.
    Without either (for example unit tests that fake ``prepare_chat``)
    the task simply runs without checkpoints, exactly as before.
    """
    session_pk = getattr(prepared, "session_pk", None)
    if engine is None or session_pk is None:
        return None
    store = TaskCheckpointStore(engine)

    async def _checkpoint(*, call_id: str, name: str, args, result) -> None:
        await store.record(
            user_id=principal.user_id, session_pk=session_pk,
            workflow_id=workflow_id, call_id=call_id, tool_name=name,
            args=args, result=result,
        )

    return _checkpoint


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
    mode: str = "plan",
    approval_store=None,
    retrieval=None,
    continuity=None,
    engine=None,
    agent_platform: str | None = None,
    harness_bus=None,
    byok=None,
    audit: Callable | None = None,
) -> str:
    """Prepare the turn and consume ``run_agent_turn`` to completion."""
    if mode not in ("plan", "manual"):
        # D1: plan is read-only; manual adds durable approvals. `auto`
        # has no background surface (and would execute on stage), so
        # reject it here instead of letting a caller widen the surface.
        raise BackgroundTaskError(
            f"unsupported background task mode: {mode}")
    durable = None
    if mode == "manual":
        # Manual: every mutating call must suspend on the slow path.
        # Without a store there is nowhere durable to park it, so fail
        # before any turn runs rather than silently falling back to the
        # fast browser flow (which has no browser here).
        if approval_store is None:
            raise BackgroundTaskError(
                "durable approvals are not available on this server")
        durable = DurableApprovalAdapter(
            approval_store, waiter,
            workflow_id=workflow_id,
            owner_subject=principal.user_id,
            ttl_seconds=BACKGROUND_APPROVAL_TTL_SECONDS,
        )
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
    checkpointer = _make_checkpointer(
        engine, principal, prepared, workflow_id)
    stream = run_agent_turn(
        prepared, model=model, router=router,
        sessions=sessions, memory=memory, runs_store=runs_store,
        principal=principal, mode=mode,
        pending_store=pending_store, executor=executor,
        waiter=waiter, durable=durable, audit=audit,
        retrieval=retrieval, continuity=continuity,
        engine=engine, agent_platform=agent_platform,
        bus=harness_bus, checkpointer=checkpointer,
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
                if durable is not None:
                    # The action is parked; the runner is about to block
                    # on the rendezvous. Metadata only - never the
                    # command, path, args, summary, or detail.
                    action_name = _event_field(data, "action")
                    if audit is not None:
                        try:
                            await audit(
                                "background_task.approval_requested",
                                meta={
                                    "workflow_id": workflow_id,
                                    "action": action_name,
                                },
                            )
                        except Exception:
                            logger.warning(
                                "Background approval audit write failed "
                                "for %s", workflow_id)
                    _emit(
                        harness_bus, HarnessEventType.APPROVAL_REQUESTED,
                        workflow_id=workflow_id,
                        user_id=principal.user_id,
                        name=action_name,
                    )
                    continue
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
            if event_name == "approval_resolved":
                _emit(
                    harness_bus, HarnessEventType.APPROVAL_RESOLVED,
                    workflow_id=workflow_id,
                    user_id=principal.user_id,
                    name=_event_field(data, "action"),
                    approved=bool(
                        data.get("approved")
                        if isinstance(data, dict) else False),
                )
                continue
            if event_name == "approval_timeout":
                raise BackgroundTaskError("approval timed out")
        raise BackgroundTaskError("task ended without a result")
    finally:
        # Close early (approval/error) so run_agent_turn's partial-turn
        # persistence runs now, not at loop shutdown.
        with contextlib.suppress(Exception):
            await stream.aclose()
        if durable is not None:
            # D7: no exit may leave a suspended row behind (completion,
            # denial, timeout, error, or cancellation).
            try:
                await durable.discard()
            except Exception:
                logger.warning(
                    "Background approval cleanup failed for %s",
                    workflow_id, exc_info=True)


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
    mode: str = "plan",
    approval_store=None,
    retrieval=None,
    continuity=None,
    engine=None,
    agent_platform: str | None = None,
    harness_bus=None,
    byok=None,
    audit: Callable | None = None,
) -> None:
    """Run one background task and emit its workflow events.

    ``plan`` (the default) is read-only. ``manual`` adds the durable
    approval path: a mutating call parks in ``pending_actions`` and the
    task waits for its owner (``approval_store`` + the shared waiter).
    ``auto`` is deliberately rejected by core (F35 D1).

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
                waiter=waiter, mode=mode, approval_store=approval_store,
                retrieval=retrieval,
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
