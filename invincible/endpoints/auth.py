# invincible/endpoints/auth.py
"""Request authentication for the /v1/* chat surface.

Single realm: the Authorization header (or ``x-api-key``) must carry a
per-user ``inv_`` API key. An unrevoked key whose sha256 equals the
token resolves to that key's user + default project
(``kind="api_key"``); anything else is 401. Every request routes only
through the authenticated user's own connected credentials.

Lives outside ``main`` so route modules can declare
``Depends(require_auth)`` without importing the app module.
"""
from fastapi import HTTPException, Request

from invincible.core.identity import ensure_default_project
from invincible.core.principal import Principal


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


async def require_auth(request: Request) -> Principal:
    token = extract_token(request)
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

    raise _auth_error(
        "Missing authentication token"
        if not token
        else "Invalid authentication token"
    )
