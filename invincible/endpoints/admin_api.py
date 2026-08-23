# invincible/endpoints/admin_api.py
"""Management API: provider CRUD, enable/disable, connectivity tests, and
routing-mode control (Phase 13.5).

Authz model - decided explicitly:

- The surface is **fail-closed**: when INVINCIBLE_ADMIN_KEY is unset every
  route answers 503, so a default deployment exposes nothing manageable.
- The admin key is independent of GATEWAY_API_KEY on purpose. Chat clients
  (and therefore the LLMs they reach) must never be able to rewrite the
  provider list; holding the gateway key proves nothing for /api/v1/*.
- Comparison is timing-safe and the key is never logged or echoed.

Credentials remain env-var references only (``api_key_env``): this API
never accepts raw provider keys and never returns secret values.
"""
import hmac
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from invincible.core.provider_registry import ProviderRegistryError
from invincible.core.settings import settings

logger = logging.getLogger(__name__)


class RoutingIn(BaseModel):
    mode: str = "auto"
    pinned: dict | None = None
    chain: list[dict] | None = None


async def require_admin(request: Request) -> None:
    """Bearer INVINCIBLE_ADMIN_KEY check; 503 when unconfigured."""
    key = settings.admin_key()
    if not key:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "message": (
                        "Management API disabled: INVINCIBLE_ADMIN_KEY "
                        "is not set"
                    ),
                    "type": "config_error",
                }
            },
        )
    auth = request.headers.get("Authorization") or ""
    token = auth.removeprefix("Bearer ").strip()
    if not token or not hmac.compare_digest(
        token.encode("utf-8"), key.encode("utf-8")
    ):
        raise HTTPException(
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
            detail={
                "error": {
                    "message": "Invalid management credentials",
                    "type": "auth_error",
                }
            },
        )


router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_admin)])


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


@router.get("/providers")
async def list_providers(request: Request):
    return {"providers": _registry(request).list()}


@router.post("/providers", status_code=201)
async def add_provider(request: Request, entry: dict):
    try:
        added = await _registry(request).add(entry)
    except (ValueError, ProviderRegistryError) as e:
        raise _invalid(e) from e
    return {"provider": added}


@router.patch("/providers/{name}")
async def update_provider(name: str, request: Request, patch: dict):
    try:
        updated = await _registry(request).update(name, patch)
    except (ValueError, ProviderRegistryError) as e:
        raise _invalid(e) from e
    return {"provider": updated}


@router.delete("/providers/{name}")
async def remove_provider(name: str, request: Request):
    try:
        await _registry(request).remove(name)
    except ProviderRegistryError as e:
        raise _invalid(e) from e
    return {"removed": name}


@router.post("/providers/{name}/disable")
async def disable_provider(name: str, request: Request):
    try:
        provider = await _registry(request).disable(name)
    except ProviderRegistryError as e:
        raise _invalid(e) from e
    return {"provider": provider}


@router.post("/providers/{name}/enable")
async def enable_provider(name: str, request: Request):
    try:
        provider = await _registry(request).enable(name)
    except ProviderRegistryError as e:
        raise _invalid(e) from e
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
