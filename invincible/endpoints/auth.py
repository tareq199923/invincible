# invincible/endpoints/auth.py
"""Request authentication for the /v1/* chat surface (Platform Phase 1).

Dual-realm resolution - the order is fixed and unambiguous:

1. ``GATEWAY_API_KEY`` timing-safe match -> system local owner
   (``kind="legacy"``);
2. else an unrevoked API key whose sha256 equals the token -> that key's
   user + default project (``kind="api_key"``);
3. else, when the gateway key is UNSET, the documented fail-open local
   identity (``kind="anonymous"``; same loud startup warning as before) -
   but ONLY while at most one human account exists. Once a second human
   registers, "local mode" is meaningless on a multi-user instance and
   the anonymous principal is refused (multi-tenant audit Step 2);
4. otherwise 401.

A token that somehow matches both realms resolves as legacy (step 1
wins); a dedicated test mints that collision and pins the outcome.
Lives outside ``main`` so route modules can declare
``Depends(require_auth)`` without importing the app module.
"""
import hmac

from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import func, select

from invincible.core.db import ensure_local_owner, users
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


async def local_principal(app: FastAPI,
                          kind: str = "legacy") -> Principal:
    user_id, project_id = await ensure_local_owner(app.state.engine)
    return Principal(user_id=user_id, project_id=project_id, kind=kind)


async def _human_user_count(engine) -> int:
    """Number of non-system (human) accounts - the cheap check that gates
    anonymous local mode. The system local owner does not count."""
    async with engine.connect() as conn:
        return int((await conn.execute(
            select(func.count()).select_from(users)
            .where(users.c.is_system.is_(False))
        )).scalar_one())


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
        # Documented fail-open local mode (loud startup warning in main),
        # valid only while the instance is genuinely single-tenant: once
        # more than one human account exists the anonymous principal is
        # refused - it would silently ride the local owner's data and
        # provider pool (multi-tenant audit Step 2 / LOW-1).
        engine = getattr(request.app.state, "engine", None)
        if engine is not None and await _human_user_count(engine) > 1:
            raise _auth_error(
                "Authentication required: this server has multiple user "
                "accounts"
            )
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
