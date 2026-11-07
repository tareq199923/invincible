# invincible/endpoints/tasks.py
"""Background agent tasks (cookie realm only, plan mode only).

``POST /dashboard/tasks`` starts a server-side agent turn with no browser
attached. The runner lives in ``core/background_tasks.py``; this module is
a thin wrapper that validates the request, resolves the same chat inputs
``POST /dashboard/chat/stream`` builds, enforces the per-user running cap,
and hands the work to the registry.

Auth and CSRF posture match the rest of the dashboard: the signed
``SameSite=Lax`` session cookie is the only accepted credential, and the
route is JSON-only (a cross-site form cannot post JSON). ``inv_`` API keys
never authorize this surface.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from invincible.core.background_tasks import (
    MAX_RUNNING_PER_USER,
    new_workflow_id,
    run_background_task,
)
from invincible.core.principal import Principal
from invincible.core.router import NO_CREDENTIALS_MESSAGE
from invincible.core.settings import settings
from invincible.core.webchat_agent import build_agent_executor
from invincible.endpoints.accounts import _audit, require_user_session
from invincible.endpoints.byok import byok_attempt_source_for
from invincible.endpoints.dashboard import _state

router = APIRouter()

_MAX_TASK_CHARS = 8000
_MAX_MODEL_CHARS = 200
_PLAN_MODE = "plan"


def _error(message: str, error_type: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": error_type}},
        status_code=status_code,
    )


@router.post("/dashboard/tasks")
async def start_background_task(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    """Start one plan-mode task; return its workflow id.

    Validation and the no-provider check run BEFORE anything starts, so a
    rejected request never leaves a phantom workflow behind. The running
    cap is checked immediately before ``registry.start`` with no ``await``
    in between, making the check atomic on the event loop.
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
    # Step 1 is plan mode only. The field is accepted for forward
    # compatibility but anything else is rejected outright instead of
    # being silently downgraded.
    mode = raw.get("mode")
    if mode is not None:
        mode = str(mode).strip().lower()
    if mode not in (None, "", _PLAN_MODE):
        return _error("mode must be plan.", "invalid_request_error", 400)

    engine = _state(request, "engine")
    registry = _state(request, "background_tasks")
    router_obj = getattr(request.app.state, "router", None)
    if router_obj is None:
        return _error("Router not initialized.", "config_error", 503)

    byok = await byok_attempt_source_for(engine, principal, model=model)
    if byok is None or not byok[0]:
        return _error(NO_CREDENTIALS_MESSAGE, "invalid_request_error", 400)

    if registry.running_count(principal.user_id) >= MAX_RUNNING_PER_USER:
        return _error(
            f"At most {MAX_RUNNING_PER_USER} background tasks may run at "
            "once. Wait for one to finish and try again.",
            "rate_limit_error", 429)

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
        # contents; same discipline as the webchat and MCP audits.
        await _audit(
            request, action, actor_user_id=principal.user_id,
            actor_kind="user", resource_type="webchat_tool",
            meta=meta or {},
        )

    workflow_id = new_workflow_id()
    registry.start(
        principal.user_id,
        run_background_task(
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
            retrieval=retrieval,
            continuity=continuity,
            engine=engine,
            agent_platform=agent_platform,
            harness_bus=harness_bus,
            byok=byok,
            audit=_audit_tool,
        ),
    )
    # Metadata-only trail for the start itself: workflow id and mode -
    # never the task text.
    await _audit(
        request, "background_task.started",
        actor_user_id=principal.user_id,
        actor_kind="user",
        resource_type="background_task",
        resource_id=workflow_id,
        meta={"workflow_id": workflow_id, "mode": _PLAN_MODE},
    )
    return JSONResponse({"workflow_id": workflow_id}, status_code=202)
