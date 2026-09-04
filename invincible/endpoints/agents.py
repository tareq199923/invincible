# invincible/endpoints/agents.py
"""Agent-facing endpoints: long-poll + result submission (Phase 10).

Transport for confirmed MCP tool jobs between the server and the
user's paired agent (``invincible agent`` on their own machine). Two
realms meet here, and they never mix:

- ``POST /agent/poll`` and ``POST /agent/result`` authenticate with an
  inv_ API key (the same credential ``invincible login`` persists to
  ~/.invincible/config.json) via ``require_agent_auth`` - a deliberately
  narrow dependency, NOT endpoints/auth.py's require_auth: the gateway
  key realm and the fail-open anonymous local mode must never reach
  agent dispatch. A key resolves to exactly one user, and registry
  queues are keyed by that user - routing is the isolation.
- ``GET /agent/status`` authenticates with a dashboard session cookie
  (``resolve_session``, the same resolver every account page uses) and
  reports that user's agent liveness for the MCP setup page badge.

Replay indistinguishability: submit_result returns plain accepted
True/False with no reason attached, mirroring PendingActionStore.take's
treatment of mismatched subjects - unknown, timed-out,
already-resolved, and wrong-owner job_ids all look identical to the
caller.
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from invincible.core.accounts import resolve_session
from invincible.core.settings import AGENT_POLL_HOLD_SECONDS

router = APIRouter()


async def require_agent_auth(request: Request) -> int:
    """Resolve the inv_ Bearer key to its owning user_id, or 401.

    ApiKeyStore.resolve (core/identity.py) is the single source of
    truth for key validity and revocation; an unrevoked key implies a
    completed device pairing. The store touches last_used_at
    best-effort on its own.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer key")
    raw = auth[len("Bearer "):].strip()
    api_keys = getattr(request.app.state, "api_keys", None)
    resolved = (
        await api_keys.resolve(raw) if api_keys is not None else None
    )
    if resolved is None:
        raise HTTPException(status_code=401, detail="Unknown or revoked key")
    return int(resolved["user_id"])


@router.post("/agent/poll")
async def agent_poll(request: Request,
                     user_id: int = Depends(require_agent_auth)) -> dict:
    """Long-poll: answer with the next confirmed job for this agent's
    user, or ``{"job": null}`` after the hold window. Every call is a
    heartbeat, so liveness tracks connection health, not execution.
    """
    registry = request.app.state.agent_registry
    job = await registry.poll(user_id, AGENT_POLL_HOLD_SECONDS)
    if job is None:
        return {"job": None}
    return {"job": job}


@router.post("/agent/result")
async def agent_result(request: Request,
                       user_id: int = Depends(require_agent_auth)) -> dict:
    """Submit a job result. Accepted False covers unknown, timed-out,
    already-resolved, and wrong-owner job_ids indistinguishably; a
    mismatched result can never resolve a future twice."""
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400,
                            detail="Invalid JSON body") from exc
    job_id = body.get("job_id") if isinstance(body, dict) else None
    result = body.get("result") if isinstance(body, dict) else None
    if not job_id or not isinstance(result, dict):
        raise HTTPException(
            status_code=400,
            detail="Body must be {job_id, result(dict)}",
        )
    registry = request.app.state.agent_registry
    accepted = registry.submit_result(user_id, job_id, result)
    return {"accepted": bool(accepted)}


@router.get("/agent/status")
async def agent_status(request: Request) -> dict:
    """Agent liveness for the signed-in dashboard user (session-cookie
    realm, same resolver as every account page)."""
    user = await resolve_session(
        request.app.state.engine, request.cookies.get("invincible_session")
    )
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    registry = request.app.state.agent_registry
    return {"agent_online": registry.online(int(user["id"]))}
