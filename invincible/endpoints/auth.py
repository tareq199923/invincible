# invincible/endpoints/auth.py
"""Request authentication for the /v1/* chat surface (Platform Phase 1).

Dual-realm resolution - the order is fixed and unambiguous:

1. ``GATEWAY_API_KEY`` timing-safe match -> system local owner
   (``kind="legacy"``);
2. else an unrevoked API key whose sha256 equals the token -> that key's
   user + default project (``kind="api_key"``);
3. else, when the gateway key is UNSET, the documented fail-open local
   identity (``kind="anonymous"``; same loud startup warning as before);
4. otherwise 401.

A token that somehow matches both realms resolves as legacy (step 1
wins); a dedicated test mints that collision and pins the outcome.
Lives outside ``main`` so route modules can declare
``Depends(require_auth)`` without importing the app module.
"""
import hmac

from fastapi import FastAPI, HTTPException, Request

from invincible.core.db import ensure_local_owner
from invincible.core.identity import ensure_default_project
from invincible.core.principal import Principal
from invincible.core.settings import settings


def extract_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization")
    if auth and auth.startswith("Bearer "):
        return auth.removeprefix("Bearer ")
    return request.headers.get("x-api-key")


def _auth_error(message: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"error": {"message": message, "type": "auth_error"}},
    )


async def local_principal(app: FastAPI) -> Principal:
    user_id, project_id = await ensure_local_owner(app.state.engine)
    return Principal(user_id=user_id, project_id=project_id, kind="legacy")


async def require_auth(request: Request) -> Principal:
    token = extract_token(request)
    gateway_key = settings.gateway_api_key()

    if (
        gateway_key
        and token
        and hmac.compare_digest(
            token.encode("utf-8"), gateway_key.encode("utf-8")
        )
    ):
        return await local_principal(request.app)

    api_keys = getattr(request.app.state, "api_keys", None)
    if token and api_keys is not None:
        resolved = await api_keys.resolve(token)
        if resolved is not None:
            project_id = await ensure_default_project(
                request.app.state.engine, resolved["user_id"]
            )
            return Principal(
                user_id=resolved["user_id"],
                project_id=project_id,
                kind="api_key",
                api_key_id=resolved["id"],
            )

    if not gateway_key:
        # Documented fail-open local mode (loud startup warning in main).
        principal = await local_principal(request.app)
        return Principal(
            user_id=principal.user_id,
            project_id=principal.project_id,
            kind="anonymous",
        )

    raise _auth_error(
        "Missing authentication token"
        if not token
        else "Invalid authentication token"
    )
