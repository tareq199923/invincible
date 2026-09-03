# invincible/endpoints/admin_api.py
"""Management API: provider CRUD, enable/disable, connectivity tests, and
routing-mode control (Phase 13.5).

Authz model - decided explicitly:

- The surface is **fail-closed**: when browser sessions are not configured
  (INVINCIBLE_OWNER_SECRET unset) every route answers 503, so a default
  deployment exposes nothing manageable.
- Management authenticates through the **operator account realm** (the
  same realm as the dashboard): an operator-role browser session OR the
  operator's own ``inv_`` API key. INVINCIBLE_ADMIN_KEY was retired -
  a single-operator deployment should not carry a second top-level
  secret. Chat credentials (the legacy gateway key, MCP bearer tokens)
  still never pass: resolve() matches inv_ hashes only, and a plain-user
  session or key answers 403.
- Sessions ride a SameSite=Lax cookie, so cross-site form posts cannot
  carry them (same CSRF posture as every other dashboard mutation).
- Credentials remain env-var references only (``api_key_env``): this API
  never accepts raw provider keys and never returns secret values.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from invincible.core.accounts import (
    SESSION_COOKIE,
    SessionManager,
    UserService,
    resolve_session,
)
from invincible.core.db import ROLE_OPERATOR
from invincible.core.identity import ApiKeyStore, ensure_default_project
from invincible.core.provider_registry import ProviderRegistryError
from invincible.core.principal import Principal
from invincible.endpoints.auth import extract_token

logger = logging.getLogger(__name__)


class RoutingIn(BaseModel):
    mode: str = "auto"
    pinned: dict | None = None
    chain: list[dict] | None = None


async def require_operator(request: Request) -> Principal:
    """Operator-role account session or ``inv_`` API key; 503 when browser
    sessions are unconfigured; 403 for an authenticated non-operator; 401
    when no account credential is present at all."""
    if not SessionManager.available():
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "message": (
                        "Management API disabled: INVINCIBLE_OWNER_SECRET "
                        "is not set (no account sessions)"
                    ),
                    "type": "config_error",
                }
            },
        )
    engine = request.app.state.engine

    # Candidate credentials, in resolution order: session cookie, then
    # bearer inv_ key. Any operator candidate admits the request; a
    # resolved-but-plain-user candidate answers 403 (not 401) so a
    # logged-in non-owner is told why, not invited to retry.
    candidates: list[Principal] = []

    async def _principal_for(user_id: int, kind: str,
                             api_key_id: int | None = None) -> Principal:
        project_id = await ensure_default_project(engine, user_id)
        return Principal(user_id=user_id, project_id=project_id, kind=kind,
                         api_key_id=api_key_id)

    user = await resolve_session(
        engine, request.cookies.get(SESSION_COOKIE))
    if user is not None:
        candidates.append(await _principal_for(int(user["id"]), "session"))
        if user["role"] == ROLE_OPERATOR:
            return candidates[0]
    token = extract_token(request)
    if token:
        resolved_key = await ApiKeyStore(engine).resolve(token)
        if resolved_key is not None:
            candidates.append(await _principal_for(
                resolved_key["user_id"], "api_key",
                api_key_id=resolved_key["id"]))
            row = await UserService(engine).get(resolved_key["user_id"])
            if row is not None and row["role"] == ROLE_OPERATOR:
                return candidates[-1]
    if candidates:
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "message": "Operator role required for management.",
                    "type": "auth_error",
                }
            },
        )
    raise HTTPException(
        status_code=401,
        detail={
            "error": {
                "message": "Sign in as an operator to manage this gateway.",
                "type": "auth_error",
            }
        },
    )


router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_operator)])


def _registry(request: Request):
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "message": "Provider registry not initialized",
                    "type": "config_error",
                }
            },
        )
    return registry


def _invalid(e: Exception) -> HTTPException:
    """Map registry/validation failures to a 400 with the reason."""
    return HTTPException(
        status_code=400,
        detail={"error": {"message": str(e), "type": "invalid_request_error"}},
    )


async def _audit(request: Request, principal: Principal, action: str,
                 resource_id: str | None = None) -> None:
    """Best-effort audit row for management mutations (Phase 2). The
    actor is the operator account the request authenticated as."""
    log = getattr(request.app.state, "audit_log", None)
    if log is None:
        return
    try:
        await log.record(
            action,
            actor_kind="operator",
            actor_user_id=principal.user_id,
            resource_type="provider" if action.startswith("provider.")
            else "routing",
            resource_id=resource_id,
        )
    except Exception:  # noqa: BLE001 - telemetry only
        logger.warning("audit write failed for %s", action, exc_info=True)


@router.get("/providers")
async def list_providers(request: Request):
    return {"providers": _registry(request).list()}


@router.post("/providers", status_code=201)
async def add_provider(request: Request, entry: dict,
                       principal: Principal = Depends(require_operator)):
    try:
        added = await _registry(request).add(entry)
    except (ValueError, ProviderRegistryError) as e:
        raise _invalid(e) from e
    await _audit(request, principal, "provider.added", added.get("name"))
    return {"provider": added}


@router.patch("/providers/{name}")
async def update_provider(name: str, request: Request, patch: dict,
                          principal: Principal = Depends(require_operator)):
    try:
        updated = await _registry(request).update(name, patch)
    except (ValueError, ProviderRegistryError) as e:
        raise _invalid(e) from e
    await _audit(request, principal, "provider.updated", name)
    return {"provider": updated}


@router.delete("/providers/{name}")
async def remove_provider(name: str, request: Request,
                          principal: Principal = Depends(require_operator)):
    try:
        await _registry(request).remove(name)
    except ProviderRegistryError as e:
        raise _invalid(e) from e
    await _audit(request, principal, "provider.removed", name)
    return {"removed": name}


@router.post("/providers/{name}/disable")
async def disable_provider(name: str, request: Request,
                           principal: Principal = Depends(require_operator)):
    try:
        provider = await _registry(request).disable(name)
    except ProviderRegistryError as e:
        raise _invalid(e) from e
    await _audit(request, principal, "provider.disabled", name)
    return {"provider": provider}


@router.post("/providers/{name}/enable")
async def enable_provider(name: str, request: Request,
                          principal: Principal = Depends(require_operator)):
    try:
        provider = await _registry(request).enable(name)
    except ProviderRegistryError as e:
        raise _invalid(e) from e
    await _audit(request, principal, "provider.enabled", name)
    return {"provider": provider}


@router.post("/providers/{name}/test")
async def test_provider(name: str, request: Request):
    report = await _registry(request).test(name)
    return {"name": name, **report}


@router.get("/routing")
async def get_routing(request: Request):
    return _registry(request).routing()


@router.put("/routing")
async def put_routing(routing_in: RoutingIn, request: Request):
    try:
        routing = await _registry(request).set_routing(
            routing_in.mode,
            pinned=routing_in.pinned,
            chain=routing_in.chain,
        )
    except (ValueError, ProviderRegistryError) as e:
        raise _invalid(e) from e
    return routing
