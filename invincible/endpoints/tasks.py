# invincible/endpoints/tasks.py
"""Background agent tasks (cookie realm only).

``POST /dashboard/background-tasks`` starts a server-side agent turn with no
browser attached. ``plan`` is read-only; ``manual`` adds durable approvals
for machine-mutating actions. The runner lives in
``core/background_tasks.py``; this module is a thin wrapper that validates
the request, resolves the same chat inputs ``POST /dashboard/chat/stream``
builds, enforces the per-user running cap, and hands the work to the
registry. ``POST .../approvals/{token}`` wakes the runner's waiter; it never
claims or executes the staged action itself.

Auth and CSRF posture match the rest of the dashboard: the signed
``SameSite=Lax`` session cookie is the only accepted credential, and the
routes are JSON-only (a cross-site form cannot post JSON). ``inv_`` API keys
never authorize this surface.
"""
import logging
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from invincible.core.background_tasks import (
    MAX_RUNNING_PER_USER,
    effective_approval_deadline,
    new_workflow_id,
    run_background_task,
)
from invincible.core.principal import Principal
from invincible.core.router import NO_CREDENTIALS_MESSAGE
from invincible.core.settings import settings
from invincible.core.task_records import FAILED, INTERRUPTED
from invincible.core.webchat_agent import build_agent_executor
from invincible.core.workflow_store import (
    list_workflow_events,
    task_started_at,
)
from invincible.endpoints.accounts import _audit, require_user_session
from invincible.endpoints.byok import byok_attempt_source_for
from invincible.endpoints.dashboard import _state

logger = logging.getLogger("invincible.tasks")

router = APIRouter()

_MAX_TASK_CHARS = 8000
_MAX_MODEL_CHARS = 200
_BACKGROUND_MODES = ("plan", "manual")
_NOT_FOUND = {"error": {"message": "No such approval.",
                        "type": "not_found_error"}}
_NOT_FOUND_TASK = {"error": {"message": "No such background task.",
                             "type": "not_found_error"}}
_CAP_MESSAGE = (
    f"At most {MAX_RUNNING_PER_USER} background tasks may run at once. "
    "Wait for one to finish, or answer a pending approval at "
    "/dashboard/approvals, then try again."
)


def _error(message: str, error_type: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": error_type}},
        status_code=status_code,
    )


@router.post("/dashboard/background-tasks")
async def start_background_task(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    """Start one plan or manual task; return its workflow id.

    Request validation runs BEFORE anything is persisted or started, so a
    rejected request never leaves a phantom workflow behind. The shared
    helper then inserts the durable ``running`` row, re-checks the running
    cap, and schedules the runner.
    """
    try:
        raw = await request.json()
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return _error("Request body must be JSON.",
                      "invalid_request_error", 400)
    task_value = raw.get("task")
    if not isinstance(task_value, str) or not task_value.strip():
        return _error("task is required.", "invalid_request_error", 400)
    task = task_value.strip()
    if len(task) > _MAX_TASK_CHARS:
        return _error(
            f"task must be at most {_MAX_TASK_CHARS} characters.",
            "invalid_request_error", 400)
    model_value = raw.get("model")
    if model_value is not None and not isinstance(model_value, str):
        return _error("model must be a string.", "invalid_request_error", 400)
    model = (model_value or "").strip() or None
    if model is not None and len(model) > _MAX_MODEL_CHARS:
        return _error(
            f"model must be at most {_MAX_MODEL_CHARS} characters.",
            "invalid_request_error", 400)
    # Plan is the default. Manual adds durable approvals for mutating
    # actions; `auto` has no unattended background surface and is
    # rejected rather than silently downgraded.
    mode_value = raw.get("mode")
    if mode_value is None:
        mode = "plan"
    elif isinstance(mode_value, str):
        mode = mode_value.strip().lower()
    else:
        return _error("mode must be plan or manual.",
                      "invalid_request_error", 400)
    if mode not in _BACKGROUND_MODES:
        return _error("mode must be plan or manual.",
                      "invalid_request_error", 400)

    return await _start_task(
        request, principal, task=task, model=model, mode=mode)


async def _start_task(
    request: Request,
    principal: Principal,
    *,
    task: str,
    model: str | None,
    mode: str,
    retry_of: str | None = None,
) -> JSONResponse:
    """Shared start path for a fresh task and an interrupted-task Retry.

    The durable ``running`` row is inserted before the runner starts. If
    the insert fails the task is not started (503). If scheduling fails
    after the insert, the row is closed ``failed`` - never left as a
    phantom ``running`` row.
    """
    engine = _state(request, "engine")
    registry = _state(request, "background_tasks")
    records = _state(request, "task_records")
    router_obj = getattr(request.app.state, "router", None)
    if router_obj is None:
        return _error("Router not initialized.", "config_error", 503)

    # Manual needs a durable store for the staged rows. Plan never reads
    # it; the same server can still run plan tasks with no store.
    approval_store = None
    if mode == "manual":
        approval_store = _state(request, "approval_store")

    byok = await byok_attempt_source_for(engine, principal, model=model)
    if byok is None or not byok[0]:
        return _error(NO_CREDENTIALS_MESSAGE, "invalid_request_error", 400)

    if registry.running_count(principal.user_id) >= MAX_RUNNING_PER_USER:
        return _error(_CAP_MESSAGE, "rate_limit_error", 429)

    store = _state(request, "sessions")
    memory = getattr(request.app.state, "memory", None)
    retrieval = getattr(request.app.state, "retrieval", None)
    continuity = getattr(request.app.state, "continuity", None)
    runs_store = getattr(request.app.state, "runs", None)
    pending_store = _state(request, "pending_actions")
    waiter = _state(request, "webchat_approvals")
    agent_registry = getattr(request.app.state, "agent_registry", None)
    harness_bus = getattr(request.app.state, "harness_bus", None)
    executor = build_agent_executor(
        agent_registry, principal.user_id, settings.agent_routing())
    agent_platform = None
    if executor is not None and agent_registry is not None:
        try:
            for machine in agent_registry.machines_for(principal.user_id):
                reported = str(machine.get("platform", "")).strip()
                if reported:
                    agent_platform = reported
                    break
        except Exception:
            agent_platform = None

    async def _audit_tool(action: str, meta: dict | None = None):
        # Metadata only (action + mode) - never commands, paths, or
        # contents; background approval requests are scoped to their task.
        background_event = action == "background_task.approval_requested"
        metadata = meta or {}
        await _audit(
            request, action, actor_user_id=principal.user_id,
            actor_kind="user",
            resource_type=("background_task" if background_event
                           else "webchat_tool"),
            resource_id=(metadata.get("workflow_id")
                         if background_event else None),
            meta=metadata,
        )

    workflow_id = new_workflow_id()
    try:
        await records.create_running(
            workflow_id, user_id=principal.user_id, mode=mode)
    except SQLAlchemyError:
        logger.exception(
            "Could not persist background task record %s", workflow_id)
        return _error(
            "Background task storage is unavailable; the task was not "
            "started.", "config_error", 503)

    # The insert above is an await, so re-check the cap immediately
    # before registry.start with no await in between. Two racing starts
    # can both pass the early check; this keeps the cap atomic anyway.
    if registry.running_count(principal.user_id) >= MAX_RUNNING_PER_USER:
        await _close_failed(records, workflow_id)
        return _error(_CAP_MESSAGE, "rate_limit_error", 429)

    runner = run_background_task(
        task=task,
        model=model,
        principal=principal,
        workflow_id=workflow_id,
        router=router_obj,
        sessions=store,
        memory=memory,
        runs_store=runs_store,
        pending_store=pending_store,
        executor=executor,
        waiter=waiter,
        mode=mode,
        approval_store=approval_store,
        retrieval=retrieval,
        continuity=continuity,
        engine=engine,
        agent_platform=agent_platform,
        harness_bus=harness_bus,
        byok=byok,
        audit=_audit_tool,
        task_records=records,
        cancel_status=registry.cancel_status,
    )
    try:
        registry.start(
            principal.user_id, runner, workflow_id=workflow_id)
    except Exception:  # noqa: BLE001 - close the row, no phantom running
        runner.close()
        logger.exception(
            "Could not schedule background task %s", workflow_id)
        await _close_failed(records, workflow_id)
        return _error(
            "The background task could not be scheduled.",
            "config_error", 503)

    # Metadata-only trail for the start itself: workflow id and mode -
    # never the task text.
    await _audit(
        request, "background_task.started",
        actor_user_id=principal.user_id,
        actor_kind="user",
        resource_type="background_task",
        resource_id=workflow_id,
        meta={"workflow_id": workflow_id, "mode": mode},
    )
    if retry_of is not None:
        # A Retry is a NEW task; the old row and timeline are untouched.
        await _audit(
            request, "background_task.retried",
            actor_user_id=principal.user_id,
            actor_kind="user",
            resource_type="background_task",
            resource_id=retry_of,
            meta={
                "workflow_id": retry_of,
                "new_workflow_id": workflow_id,
                "mode": mode,
            },
        )
    return JSONResponse({"workflow_id": workflow_id}, status_code=202)


async def _close_failed(records, workflow_id: str) -> None:
    """Best-effort close for a task that never reached the registry."""
    try:
        await records.mark_terminal(workflow_id, FAILED)
    except Exception:  # noqa: BLE001 - the 429/503 is the real result
        logger.warning(
            "Could not mark background task %s failed", workflow_id,
            exc_info=True)


@router.post("/dashboard/background-tasks/{workflow_id}/cancel")
async def cancel_background_task(
    request: Request,
    workflow_id: str,
    principal: Principal = Depends(require_user_session),
):
    """Stop one owned, still-running background task.

    Ownership and liveness are one predicate in the registry: a foreign,
    finished, or unknown workflow id all produce the SAME 404 body, so a
    workflow id never becomes a capability. A successful cancellation
    only calls ``task.cancel()``; the runner's existing
    ``CancelledError`` branch emits the single ``workflow.failed``
    event. The audit row is metadata-only and written once.
    """
    registry = _state(request, "background_tasks")
    if not registry.cancel(principal.user_id, workflow_id):
        return _error(
            "No such background task.", "not_found_error", 404)
    await _audit(
        request, "background_task.cancelled",
        actor_user_id=principal.user_id,
        actor_kind="user",
        resource_type="background_task",
        resource_id=workflow_id,
        meta={"workflow_id": workflow_id},
    )
    return JSONResponse({"status": "cancelling"}, status_code=202)


@router.post("/dashboard/background-tasks/{workflow_id}/retry")
async def retry_background_task(
    request: Request,
    workflow_id: str,
    principal: Principal = Depends(require_user_session),
):
    """Start a NEW task from an interrupted task's original text.

    Retry is deliberately not a resume: the old row and event timeline
    stay untouched, no checkpoint or approval is reused, and the new task
    gets a fresh workflow id, durable row, and approval window. Only an
    owner's ``interrupted`` row is retryable; unknown and foreign ids
    share the same 404 body. The original task text is read from the chat
    session whose id IS the workflow id.
    """
    # Same CSRF posture as start: a cross-site form cannot set this
    # content type. The body is optional; the JS sends ``{}``.
    content_type = (
        request.headers.get("content-type", "")
        .split(";", 1)[0].strip().lower()
    )
    if content_type != "application/json":
        return _error("Request body must be JSON.",
                      "invalid_request_error", 400)

    records = _state(request, "task_records")
    row = await records.get(workflow_id, user_id=principal.user_id)
    if row is None:
        return JSONResponse(_NOT_FOUND_TASK, status_code=404)
    if row["status"] != INTERRUPTED:
        return _error(
            "Only an interrupted task can be retried.",
            "invalid_request_error", 409)

    sessions = _state(request, "sessions")
    result = await sessions.task_result(
        workflow_id, user_id=principal.user_id)
    task_text = str((result or {}).get("task_text") or "").strip()
    if not task_text:
        return _error(
            "The original task text is no longer available.",
            "invalid_request_error", 409)
    if len(task_text) > _MAX_TASK_CHARS:
        return _error(
            f"task must be at most {_MAX_TASK_CHARS} characters.",
            "invalid_request_error", 409)

    return await _start_task(
        request, principal,
        task=task_text,
        model=None,
        mode=row["mode"],
        retry_of=workflow_id,
    )


@router.post(
    "/dashboard/background-tasks/{workflow_id}/approvals/{token}")
async def decide_background_approval(
    request: Request,
    workflow_id: str,
    token: str,
    principal: Principal = Depends(require_user_session),
):
    """Wake the runner's waiter for one owned durable approval.

    This route NEVER claims or executes the staged action: it resolves
    the in-process waiter, and the runner performs the single atomic
    ``pending_actions`` claim (``DurableApprovalAdapter.resolve``) and
    the execution. Unknown, foreign, already-resolved, and past-the-
    effective-deadline tokens all share the same 404 body.
    """
    try:
        raw = await request.json()
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return _error("Request body must be JSON.",
                      "invalid_request_error", 400)
    approve = raw.get("approve")
    if not isinstance(approve, bool):
        return _error("approve must be true or false.",
                      "invalid_request_error", 400)

    store = _state(request, "approval_store")
    rows = await store.pending_for_workflow(
        workflow_id, owner_subject=principal.user_id)
    row = next((item for item in rows if item["token"] == token), None)
    if row is None:
        return JSONResponse(_NOT_FOUND, status_code=404)

    # Page-facing deadline check: the row TTL is an upper bound, and the
    # task clock minus the safety margin is the real window. The runner
    # enforces the same bound, so this is an identical miss, not a
    # separate outcome.
    engine = _state(request, "engine")
    events = await list_workflow_events(
        engine, user_id=principal.user_id, workflow_id=workflow_id)
    started = task_started_at(events)
    if started is not None:
        cutoff = effective_approval_deadline(row["deadline"], started)
        if cutoff is not None and time.time() >= cutoff:
            return JSONResponse(_NOT_FOUND, status_code=404)

    waiter = _state(request, "webchat_approvals")
    if not waiter.resolve(
            token, principal.user_id, approve, durable=True):
        return JSONResponse(_NOT_FOUND, status_code=404)

    # Metadata only: the owner's choice, never the token/command/path.
    await _audit(
        request, "background_task.approval_decided",
        actor_user_id=principal.user_id,
        actor_kind="user",
        resource_type="background_task",
        resource_id=workflow_id,
        meta={
            "workflow_id": workflow_id,
            "action": row["type"],
            "approved": approve,
        },
    )
    return JSONResponse({"ok": True, "approved": approve}, status_code=202)
