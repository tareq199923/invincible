# invincible/endpoints/dashboard.py
"""Phase 5 dashboard surface: server-rendered pages plus a small
cookie-realm management API under the browser-session realm
(require_user_session - session cookies only; this realm never
authorizes /v1/* chat or /mcp).

PR-5A shipped the overview; PR-5B added session/task views over the
shared projection; PR-5C adds memory management: browse/search/add
(explicit-layer) and audited ownership-predicated deletes. The
INVINCIBLE_MEMORY kill-switch gates only CREATION - existing rows stay
browsable/deletable so the toggle never traps data.
"""
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from invincible.core.accounts import (
    MIN_PASSWORD_LEN,
    ProjectService,
    SessionManager,
    UserService,
)
from invincible.core.identity import ApiKeyStore
from invincible.core.principal import Principal
from invincible.core.projection import (
    build_session_projection,
    fetch_session_view,
)
from invincible.core.settings import settings
from invincible.endpoints.accounts import (
    _audit,
    _page,
    _payload,
    _wants_html,
    require_user_session,
)

logger = logging.getLogger("invincible.dashboard")

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_MEMORY_PAGE_SIZE = 20
_MEMORY_MAX_CHARS = 2000
_MEMORY_KINDS = ("note", "fact", "preference", "decision", "task")
_MEMORY_LAYERS = ("explicit", "auto")


def _engine(request: Request):
    return request.app.state.engine


def _missing_page(request: Request, title: str, message: str,
                  status_code: int = 404):
    """Generic result page; the SAME body renders for unknown AND foreign
    resources so existence never leaks across users."""
    return templates.TemplateResponse(
        request, "device_result.html",
        {"title": title, "message": message},
        status_code=status_code,
    )


def _state(request: Request, attr: str):
    value = getattr(request.app.state, attr, None)
    if value is None:
        raise HTTPException(
            status_code=503,
            detail={"error": {"message": f"{attr} not initialized on "
                                          f"this server",
                              "type": "config_error"}},
        )
    return value


def _checked_layer(layer: str | None) -> str | None:
    if layer is not None and layer not in _MEMORY_LAYERS:
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": "layer must be 'explicit' or "
                                         "'auto'.",
                              "type": "invalid_request_error"}},
        )
    return layer


async def _email(engine, principal: Principal) -> str:
    user = await UserService(engine).get(principal.user_id)
    return user["email"] if user else "unknown"


@router.get("/dashboard")
async def overview_page(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    sessions_store = _state(request, "sessions")
    engine = _engine(request)
    email = await _email(engine, principal)
    projects = await ProjectService(engine).list(principal.user_id)
    keys = await ApiKeyStore(engine).list(principal.user_id)
    recent_sessions = await sessions_store.list_for_user(
        principal.user_id, limit=10)
    task_heads = await _state(request, "continuity").list_for_user(
        principal.user_id)
    memories_total = await _state(request, "memory").count_for_user(
        principal.user_id)
    usage = _usage_totals(
        await _state(request, "runs").usage_summary(principal.user_id,
                                                    days=7))
    active_keys = sum(1 for k in keys if k["revoked_at"] is None)
    # First-run signpost: chat can only succeed once BOTH a provider is
    # connected AND a key exists (BYOK-only - the operator pool is never
    # a fallback), so the banner stays until the pair is complete.
    from invincible.core.credential_store import ByokCredentialStore

    has_provider = bool(
        await ByokCredentialStore(engine).list_for_user(principal.user_id))
    return _page(
        "dashboard.html", request,
        user_email=email,
        counts={
            "projects": len(projects),
            "sessions":
                await sessions_store.count_for_user(principal.user_id),
            "api_keys": active_keys,
            "tasks": len(task_heads),
            "memories": memories_total,
            "usage_7d": usage["input_tokens"] + usage["output_tokens"],
        },
        needs_setup=not (has_provider and active_keys > 0),
        recent_sessions=recent_sessions,
    )


# ---------------------------------------------------------------------------
# Sessions


@router.get("/dashboard/sessions")
async def sessions_page(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    sessions_store = _state(request, "sessions")
    rows = await sessions_store.list_for_user(principal.user_id, limit=100)
    return _page(
        "sessions.html", request,
        user_email=await _email(_engine(request), principal),
        sessions_rows=rows,
    )


@router.get("/dashboard/sessions/{session_pk}")
async def session_detail_page(
    session_pk: int,
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    sessions_store = _state(request, "sessions")
    runs_store = _state(request, "runs")
    continuity = _state(request, "continuity")

    # Ownership predicate first: a surrogate id another user owns is
    # indistinguishable from one that does not exist.
    owned = await sessions_store.lookup_by_pk(
        session_pk, user_id=principal.user_id,
        project_id=principal.project_id)
    if owned is None:
        return _missing_page(request, "Unknown session",
                             "This session does not exist or is not yours.")
    client_session_id, owner = owned

    session_row, turns = await fetch_session_view(
        sessions_store, client_session_id,
        owner=(principal.user_id, principal.project_id))
    projection = await build_session_projection(
        sessions_store, runs_store, continuity,
        session_id=client_session_id,
        session_row=session_row,
        turns=turns,
        session_pk=session_pk,
        limit=200,
    )

    labels = {n["id"]: n["label"] for n in projection["nodes"]}
    failovers = [
        {"from": labels[e["source"]], "to": labels[e["target"]]}
        for e in projection["edges"] if e["kind"] == "failover_from"
    ]
    checkpoints = [n for n in projection["nodes"]
                   if n["kind"] == "checkpoint"]
    # Normalize for the template: turn nodes carry no ts key at all.
    activity = sorted(
        ({"label": n["label"], "ts": n.get("ts"), "kind": n["kind"]}
         for n in projection["nodes"] if n["id"] != "session"),
        key=lambda n: (n["ts"] is None, -(n["ts"] or 0)),
    )
    return _page(
        "session_detail.html", request,
        user_email=await _email(_engine(request), principal),
        session_pk=session_pk,
        client_session_id=client_session_id,
        created_at=session_row["created_at"],
        updated_at=session_row["updated_at"],
        summary=projection["summary"],
        tasks=projection["summary"]["tasks"],
        checkpoints=checkpoints,
        failovers=failovers,
        activity=activity,
    )


# ---------------------------------------------------------------------------
# Tasks (cross-session)


@router.get("/dashboard/tasks")
async def tasks_page(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    continuity = _state(request, "continuity")
    heads = await continuity.list_for_user(principal.user_id)
    return _page(
        "tasks.html", request,
        user_email=await _email(_engine(request), principal),
        task_heads=heads,
    )


# ---------------------------------------------------------------------------
# Memory management (Phase 5 PR-5C)


@router.get("/dashboard/memory")
async def memory_page(
    request: Request,
    q: str = "",
    layer: str | None = None,
    kind: str | None = None,
    offset: int = 0,
    principal: Principal = Depends(require_user_session),
):
    store = _state(request, "memory")
    layer = _checked_layer(layer)
    query = q.strip()
    if query:
        rows = await store.search_for_user(
            principal.user_id, query, layer=layer, kind=kind,
            limit=_MEMORY_PAGE_SIZE)
        total = len(rows)  # search results are a single ranked page
        offset = 0
    else:
        total = await store.count_for_user(
            principal.user_id, layer=layer, kind=kind)
        rows = await store.list_for_user(
            principal.user_id, layer=layer, kind=kind,
            limit=_MEMORY_PAGE_SIZE, offset=max(0, offset))
    return _page(
        "memory.html", request,
        user_email=await _email(_engine(request), principal),
        memories=rows,
        total=total,
        q=query,
        layer=layer or "",
        kind=kind or "",
        kinds=_MEMORY_KINDS,
        page_size=_MEMORY_PAGE_SIZE,
        offset=offset,
    )


@router.get("/memories")
async def list_memories(
    request: Request,
    q: str = "",
    layer: str | None = None,
    kind: str | None = None,
    limit: int = 50,
    offset: int = 0,
    principal: Principal = Depends(require_user_session),
):
    store = _state(request, "memory")
    layer = _checked_layer(layer)
    query = q.strip()
    if query:
        # Lexical search: single ranked page, no offset pagination.
        rows = await store.search_for_user(
            principal.user_id, query, layer=layer, kind=kind, limit=limit)
        total = len(rows)
    else:
        rows = await store.list_for_user(
            principal.user_id, layer=layer, kind=kind, limit=limit,
            offset=offset)
        total = await store.count_for_user(
            principal.user_id, layer=layer, kind=kind)
    return {"memories": rows, "total": total}


@router.post("/memories")
async def create_memory(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    # The kill-switch gates CREATION only (browse/delete stay available
    # so toggling off never traps already-saved data).
    if not settings.memory_enabled():
        return JSONResponse(
            {"error": {"code": "memory_disabled",
                       "message": "INVINCIBLE_MEMORY is off; saving new "
                                  "memories is disabled."}},
            status_code=503,
        )
    body = await _payload(request)
    content = str(body.get("content", "")).strip()
    kind = str(body.get("kind") or "note")
    if not content:
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": "Memory content is required.",
                              "type": "invalid_request_error"}},
        )
    if len(content) > _MEMORY_MAX_CHARS:
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": f"Memory content must be at most "
                                         f"{_MEMORY_MAX_CHARS} characters.",
                              "type": "invalid_request_error"}},
        )
    if kind not in _MEMORY_KINDS:
        kind = "note"
    made_id = await _state(request, "memory").save_memory(
        user_id=principal.user_id, content=content, layer="explicit",
        kind=kind, confidence=1.0)
    await _audit(request, "memory.created", actor_user_id=principal.user_id,
                 resource_type="memory", resource_id=str(made_id))
    if _wants_html(request):
        return RedirectResponse("/dashboard/memory", status_code=303)
    return JSONResponse({"id": made_id, "kind": kind}, status_code=201)


@router.delete("/memories/{memory_id}")
async def delete_memory(
    memory_id: int,
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    deleted = await _state(request, "memory").delete(
        memory_id, user_id=principal.user_id)
    if not deleted:
        # Foreign and unknown ids are indistinguishable (anti-enumeration).
        raise HTTPException(
            status_code=404,
            detail={"error": {"message": "No such memory.",
                              "type": "not_found_error"}},
        )
    await _audit(request, "memory.deleted", actor_user_id=principal.user_id,
                 resource_type="memory", resource_id=str(memory_id))
    if request.headers.get("HX-Request") == "true":
        # HTMX row removal: empty 204 lets hx-swap="delete" drop the row.
        return Response(status_code=204)
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Usage (Phase 5 PR-5D, cookie realm per locked decision)


def _usage_totals(rows: list[dict]) -> dict:
    """Fold summary rows into whole-window totals (single source)."""
    return {
        "attempts": sum(r["attempts"] for r in rows),
        "failovers": sum(r["failovers"] for r in rows),
        "input_tokens": sum(r["input_tokens"] for r in rows),
        "output_tokens": sum(r["output_tokens"] for r in rows),
    }


@router.get("/usage")
async def usage_json(
    request: Request,
    days: int = 7,
    principal: Principal = Depends(require_user_session),
):
    runs_store = _state(request, "runs")
    rows = await runs_store.usage_summary(principal.user_id, days=days)
    return {
        "days": max(1, min(days, 90)),
        "totals": _usage_totals(rows),
        "rows": rows,
    }


@router.get("/dashboard/usage")
async def usage_page(
    request: Request,
    days: int = 7,
    principal: Principal = Depends(require_user_session),
):
    runs_store = _state(request, "runs")
    window = max(1, min(days, 90))
    rows = await runs_store.usage_summary(principal.user_id, days=window)
    totals = _usage_totals(rows)

    by_day: dict[str, dict] = {}
    by_provider: dict[tuple[str, str], dict] = {}
    for row in rows:
        day = by_day.setdefault(
            row["day"], {"day": row["day"], "attempts": 0, "tokens": 0})
        day["attempts"] += row["attempts"]
        day["tokens"] += row["input_tokens"] + row["output_tokens"]
        key = (row["provider_name"], row["model_id"])
        provider = by_provider.setdefault(key, {
            "provider_name": row["provider_name"],
            "model_id": row["model_id"],
            "attempts": 0, "failovers": 0,
            "input_tokens": 0, "output_tokens": 0,
        })
        provider["attempts"] += row["attempts"]
        provider["failovers"] += row["failovers"]
        provider["input_tokens"] += row["input_tokens"]
        provider["output_tokens"] += row["output_tokens"]

    days_list = sorted(by_day.values(), key=lambda d: d["day"])
    peak = max((d["tokens"] for d in days_list), default=0) or 1
    for d in days_list:
        d["pct"] = int(round(100 * d["tokens"] / peak))

    return _page(
        "usage.html", request,
        user_email=await _email(_engine(request), principal),
        window=window,
        totals=totals,
        days_rows=days_list,
        provider_rows=sorted(
            by_provider.values(),
            key=lambda p: (-(p["input_tokens"] + p["output_tokens"]),
                           p["provider_name"], p["model_id"])),
    )


# ---------------------------------------------------------------------------
# Settings (Phase 5 PR-5E)


def _pw_error_text(code: str | None) -> str | None:
    """Fixed messages for the bounded pw_error codes POST /auth/password
    redirects back with - query strings are never rendered verbatim."""
    if code == "weak_password":
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    if code == "wrong_password":
        return "Current password is incorrect."
    if code == "password_exists":
        return "This account already has a password; use the change form."
    return None


@router.get("/dashboard/settings")
async def settings_page(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    engine = _engine(request)
    # The test client builds app.state by hand and may not carry a
    # registry; a missing one renders as an empty panel, never a 503.
    registry = getattr(request.app.state, "registry", None)
    provider_rows = []
    routing_mode = ""
    if registry is not None:
        provider_rows = [
            {"name": p.get("name", ""), "tier": p.get("tier"),
             "enabled": bool(p.get("enabled", True))}
            for p in registry.list()
        ]
        routing_mode = str((registry.routing() or {}).get("mode") or "")
    flags = [
        ("Gateway key set", bool(settings.gateway_api_key())),
        ("Browser sessions", SessionManager.available()),
        ("GitHub login", bool(settings.github_client_id())),
        ("Memory writes", settings.memory_enabled()),
        ("Continuity engine", settings.continuity_enabled()),
        ("Send-time compression", settings.compression_enabled()),
    ]
    return _page(
        "settings.html", request,
        user_email=await _email(engine, principal),
        has_password=await UserService(engine).has_password(
            principal.user_id),
        min_password_len=MIN_PASSWORD_LEN,
        pw_error=_pw_error_text(request.query_params.get("pw_error")),
        pw_saved=request.query_params.get("pw_saved") == "1",
        provider_rows=provider_rows,
        routing_mode=routing_mode,
        flags=flags,
    )


# ---------------------------------------------------------------------------
# Guided setup (Omniroute-style onboarding): connect a provider, mint a
# key, copy the client config. Two keys total - this page is the signpost
# connecting the machinery that already exists.


@router.get("/dashboard/setup")
async def setup_page(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    engine = _engine(request)
    from invincible.core.credential_store import ByokCredentialStore

    provider_rows = await ByokCredentialStore(engine).list_for_user(
        principal.user_id)
    keys = await ApiKeyStore(engine).list(principal.user_id)
    active_keys = [k for k in keys if k["revoked_at"] is None]
    # The one-time raw key display rides the query string no-where; the
    # account page's create flow already shows it once. Here we only
    # answer "does a key exist".
    base_url = str(request.base_url).rstrip("/")
    return _page(
        "setup.html", request,
        user_email=await _email(engine, principal),
        has_provider=bool(provider_rows),
        has_key=bool(active_keys),
        api_key_prefix=(active_keys[0]["prefix"] if active_keys else None),
        base_url=base_url,
        catalog_keys=[
            {"key": key, "label": entry["label"]}
            for key, entry in sorted(
                _provider_catalog_items(), key=lambda kv: kv[1]["label"])
        ],
        new_key=request.query_params.get("new_key"),
    )


def _provider_catalog_items():
    from invincible.core.provider_catalog import CATALOG

    return CATALOG.items()


# ---------------------------------------------------------------------------
# MCP grants (Phase 3; Q1 decision 2026-08-30: /mcp stays OAuth-ONLY. This
# page manages the OAuth clients/tokens behind /mcp - it never accepts inv_
# keys itself and nothing here touches require_mcp_auth.)


@router.get("/dashboard/mcp")
async def mcp_page(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    store = _state(request, "oauth_store")
    # Pre-Phase-5 grants were attached to the system local owner; the
    # dashboard user manages their own clients plus that local-era pool.
    from invincible.core.db import ensure_local_owner

    local_uid, _ = await ensure_local_owner(_engine(request))
    clients = await store.list_clients_manageable(
        [principal.user_id, local_uid])
    manageable = {c["client_id"] for c in clients}
    active = [
        t for t in await store.list_active_tokens()
        if not t["revoked"] and t["client_id"] in manageable
    ]
    by_client: dict[str, int] = {}
    for token in active:
        by_client[token["client_id"]] = by_client.get(token["client_id"], 0) + 1
    rows = [
        {**c, "active_tokens": by_client.get(c["client_id"], 0)}
        for c in clients
    ]
    return _page(
        "mcp.html", request,
        user_email=await _email(_engine(request), principal),
        clients=rows,
        revoked=request.query_params.get("revoked") == "1",
    )


@router.delete("/dashboard/mcp/clients/{client_id}/tokens")
async def revoke_mcp_client_tokens(
    client_id: str,
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    store = _state(request, "oauth_store")
    # Ownership predicate: foreign and unknown clients are
    # indistinguishable (anti-enumeration). Local-owner-era (pre-Phase-5)
    # and unowned clients stay manageable from any dashboard session.
    from invincible.core.db import ensure_local_owner

    client_row = await store.get_client(client_id)
    if client_row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"message": "No such MCP client.",
                              "type": "not_found_error"}},
        )
    owner = client_row["owner_user_id"]
    if owner is not None:
        local_uid, _ = await ensure_local_owner(_engine(request))
        if owner not in (principal.user_id, local_uid):
            raise HTTPException(
                status_code=404,
                detail={"error": {"message": "No such MCP client.",
                                  "type": "not_found_error"}},
            )
    count = await store.revoke_client_tokens(client_id)
    await _audit(request, "oauth.tokens_revoked",
                 actor_user_id=principal.user_id,
                 resource_type="oauth_client", resource_id=client_id,
                 meta={"count": count})
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=204, headers={
            "HX-Redirect": "/dashboard/mcp?revoked=1"})
    return {"revoked": count}
