# invincible/endpoints/accounts.py
"""Phase 3 account surface: browser auth, projects, API-key management,
read-only session listing, device-code pairing, and GitHub login.

Realms stay separate by design:

- Browser sessions are HMAC-signed HttpOnly cookies (core.accounts
  SessionManager) and NEVER authorize /v1/* chat or /mcp - those keep
  their own realms (gateway key / inv_ keys / OAuth bearers).
- Management here accepts a live browser session OR an ``inv_`` API key
  belonging to the same user. MCP bearer tokens and the legacy gateway
  key are deliberately rejected: holding either proves nothing for
  account administration.
- /auth/login has its OWN persistent lockout scope ("auth-login") so
  hammering it never locks the owner-consent form, and vice versa.

GitHub login is an OAuth App authorization-code flow. GitHub OAuth Apps
have no PKCE, so CSRF is handled with a signed single-use state cookie;
only VERIFIED GitHub emails may auto-link or auto-register.
"""
import logging
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from invincible.core.accounts import (
    DEFAULT_POLL_INTERVAL,
    DEVICE_CODE_TTL,
    GITHUB_STATE_COOKIE,
    MIN_PASSWORD_LEN,
    SESSION_COOKIE,
    SESSION_TTL,
    AccountError,
    DeviceCodeStore,
    GitHubOAuth,
    IdentityStore,
    ProjectService,
    SessionManager,
    UserService,
    resolve_session,
    sign_value,
    verify_signed_value,
)
from invincible.core.identity import (
    ApiKeyStore,
    LoginRateLimiter,
    ensure_default_project,
)
from invincible.core.principal import Principal
from invincible.core.settings import settings
from invincible.endpoints.auth import extract_token
from invincible.endpoints.oauth import _client_ip, _parse_form

logger = logging.getLogger("invincible.accounts")

router = APIRouter()

AUTH_LOGIN_MAX_ATTEMPTS = 5
AUTH_LOGIN_WINDOW_SECONDS = 15 * 60

# Jinja2 templates ship inside the package (no static pipeline; forms POST
# to the same /auth/* endpoints the API uses).
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _engine(request: Request):
    return request.app.state.engine


def _limiter(request: Request) -> LoginRateLimiter:
    return LoginRateLimiter(
        _engine(request),
        scope="auth-login",
        max_attempts=AUTH_LOGIN_MAX_ATTEMPTS,
        window_seconds=AUTH_LOGIN_WINDOW_SECONDS,
    )


async def _audit(request: Request, action: str, **kwargs) -> None:
    """Best-effort audit write; never blocks the account flow."""
    log = getattr(request.app.state, "audit_log", None)
    if log is None:
        return
    try:
        await log.record(action, actor_kind="user", **kwargs)
    except Exception:  # noqa: BLE001 - telemetry only
        logger.warning("audit write failed for %s", action, exc_info=True)


def _error_response(exc: AccountError) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": exc.code, "message": exc.message},
         **exc.extra},
        status_code=exc.status_code,
    )


async def _payload(request: Request) -> dict:
    """Accept JSON bodies from scripts/clients and form posts from the UI."""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
        except Exception:
            return {}
        return body if isinstance(body, dict) else {}
    return await _parse_form(request)


def _wants_html(request: Request) -> bool:
    """Form-encoded posts come from the browser pages and get redirects /
    rendered errors back; everything else keeps structured JSON."""
    return "application/x-www-form-urlencoded" in (
        request.headers.get("content-type", ""))


def _safe_next(target: str) -> str | None:
    """Only same-origin relative paths may drive a post-login bounce."""
    if target.startswith("/") and not target.startswith("//"):
        return target
    return None


def _page(template_name: str, request: Request, **context):
    return templates.TemplateResponse(
        request, template_name, context)


# ---------------------------------------------------------------------------
# Realm dependencies


async def require_user_session(request: Request) -> Principal:
    user = await resolve_session(
        _engine(request), request.cookies.get(SESSION_COOKIE))
    if user is None:
        # Signature-valid-but-stale cookies (password changed, account
        # deleted) land here too - the same path as a forged or expired
        # one (SECURITY.md limit 14).
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Sign in required.",
                              "type": "auth_error"}},
        )
    project_id = await ensure_default_project(_engine(request), user["id"])
    return Principal(user_id=user["id"], project_id=project_id, kind="session")


async def require_account_admin(request: Request) -> Principal:
    """Browser session OR the user's own ``inv_`` API key. MCP bearers and
    the legacy gateway key never pass (resolve() matches inv_ hashes only)."""
    principal: Principal | None = None
    user = await resolve_session(
        _engine(request), request.cookies.get(SESSION_COOKIE))
    if user is not None:
        principal = await _session_principal(request, user)
    token = extract_token(request)
    if principal is None and token:
        resolved_key = await ApiKeyStore(_engine(request)).resolve(token)
        if resolved_key is not None:
            project_id = await ensure_default_project(
                _engine(request), resolved_key["user_id"])
            principal = Principal(
                user_id=resolved_key["user_id"], project_id=project_id,
                kind="api_key", api_key_id=resolved_key["id"],
            )
    if principal is None:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Sign in or present an API key.",
                              "type": "auth_error"}},
        )
    return principal


async def _session_principal(request: Request, user: dict) -> Principal:
    """Session Principal for a fully resolved user row (already passed
    ``resolve_session``: exists + session_version matches)."""
    project_id = await ensure_default_project(_engine(request), user["id"])
    return Principal(user_id=user["id"], project_id=project_id, kind="session")


async def _set_session_cookie_for(request: Request, response,
                                  user_id: int) -> None:
    # Embed the user's CURRENT session_version so any later password
    # change (which bumps it) immediately orphans this cookie.
    user = await UserService(_engine(request)).get(user_id)
    version = user["session_version"] if user else 0
    response.set_cookie(
        SESSION_COOKIE,
        SessionManager.create(user_id, version),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


# ---------------------------------------------------------------------------
# Registration + password login


@router.get("/login")
async def login_page(request: Request):
    if SessionManager.verify(request.cookies.get(SESSION_COOKIE)) is not None:
        return RedirectResponse("/account", status_code=302)
    github_error = "github_error" in request.query_params
    return _page(
        "login.html", request,
        error="GitHub sign-in failed; try again or use your password."
        if github_error else None,
        github_enabled=settings.github_client_id() is not None,
        # Same-origin bounce target: where the 401 handler sent us from,
        # replayed into the POST handler's existing `next` handling.
        next_target=_safe_next(request.query_params.get("next", "")),
    )


@router.get("/register")
async def register_page(request: Request):
    return _page("register.html", request,
                 min_password_len=MIN_PASSWORD_LEN, error=None)


@router.post("/auth/register")
async def register(request: Request):
    if not SessionManager.available():
        return JSONResponse(
            {"error": {"code": "sessions_disabled",
                       "message": "Set INVINCIBLE_OWNER_SECRET to enable "
                                  "account sessions."}},
            status_code=503,
        )
    body = await _payload(request)
    service = UserService(_engine(request))
    try:
        user = await service.register(
            str(body.get("email", "")), str(body.get("password", "")))
    except AccountError as exc:
        if _wants_html(request):
            return _page(
                "register.html", request, error=exc.message,
                min_password_len=MIN_PASSWORD_LEN,
            )
        return _error_response(exc)
    project_id = await ensure_default_project(_engine(request), user["id"])
    await _audit(request, "auth.registered", actor_user_id=user["id"],
                 resource_type="user", resource_id=str(user["id"]),
                 meta={"role": user.get("role")})
    if _wants_html(request):
        response = RedirectResponse("/account", status_code=303)
        await _set_session_cookie_for(request, response, user["id"])
        return response
    response = JSONResponse(
        {"id": user["id"], "email": user["email"],
         "project_id": project_id},
        status_code=201,
    )
    await _set_session_cookie_for(request, response, user["id"])
    return response


@router.post("/auth/login")
async def login(request: Request):
    if not SessionManager.available():
        return JSONResponse(
            {"error": {"code": "sessions_disabled",
                       "message": "Set INVINCIBLE_OWNER_SECRET to enable "
                                  "account sessions."}},
            status_code=503,
        )
    ip = _client_ip(request)
    limiter = _limiter(request)
    locked_for = await limiter.locked_out(ip)
    if locked_for is not None:
        await _audit(request, "auth.login_locked_out",
                     resource_type="client_ip", resource_id=ip)
        message = (f"Too many failed attempts; retry in {locked_for}s.")
        if _wants_html(request):
            return _page("login.html", request, error=message,
                         github_enabled=False)
        return JSONResponse(
            {"error": {"code": "locked_out", "message": message}},
            status_code=429,
        )
    body = await _payload(request)
    user = await UserService(_engine(request)).authenticate(
        str(body.get("email", "")), str(body.get("password", "")))
    if user is None:
        await limiter.record_failure(ip)
        # Enumeration-safe: unknown email and wrong password are identical.
        await _audit(request, "auth.login_failed",
                     resource_type="client_ip", resource_id=ip)
        message = "Invalid email or password."
        if _wants_html(request):
            return _page("login.html", request, error=message,
                         github_enabled=settings.github_client_id()
                         is not None)
        return JSONResponse(
            {"error": {"code": "invalid_credentials",
                       "message": message}},
            status_code=401,
        )
    await limiter.reset(ip)
    project_id = await ensure_default_project(_engine(request), user["id"])
    await _audit(request, "auth.logged_in", actor_user_id=user["id"],
                 resource_type="user", resource_id=str(user["id"]))
    if _wants_html(request):
        target = _safe_next(str(body.get("next", ""))) or "/account"
        response = RedirectResponse(target, status_code=303)
        await _set_session_cookie_for(request, response, user["id"])
        return response
    response = JSONResponse({"id": user["id"], "email": user["email"],
                             "project_id": project_id})
    await _set_session_cookie_for(request, response, user["id"])
    return response


@router.post("/auth/logout")
async def logout(request: Request):
    resolved = SessionManager.verify(request.cookies.get(SESSION_COOKIE))
    uid = resolved[0] if resolved else None
    if _wants_html(request):
        response = RedirectResponse("/login", status_code=303)
    else:
        response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    if uid is not None:
        await _audit(request, "auth.logged_out", actor_user_id=uid,
                     resource_type="user", resource_id=str(uid))
    return response


# ---------------------------------------------------------------------------
# Password set / change (Phase 5 PR-5E)


@router.post("/auth/password")
async def auth_password(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    """Set a first password or change an existing one. Which flow applies
    follows the STORED account state (``password_hash`` NULL vs set),
    never caller-chosen fields: omitting ``current_password`` cannot flip
    the semantics."""
    body = await _payload(request)
    uid = principal.user_id
    service = UserService(_engine(request))
    try:
        if await service.has_password(uid):
            await service.change_password(
                uid,
                str(body.get("current_password", "")),
                str(body.get("new_password", "")),
            )
            action = "password.changed"
        else:
            await service.set_password(
                uid, str(body.get("new_password", "")))
            action = "password.set"
    except AccountError as exc:
        if _wants_html(request):
            # Bounded code; the settings page renders it from a fixed map.
            return RedirectResponse(
                f"/dashboard/settings?pw_error={exc.code}", status_code=303)
        return _error_response(exc)
    await _audit(request, action, actor_user_id=uid,
                 resource_type="user", resource_id=str(uid))
    # The acting browser keeps its login: a fresh cookie carrying the
    # NEW session_version is issued here. Every OTHER cookie for this
    # user - minted before the bump - now fails resolution.
    if _wants_html(request):
        response = RedirectResponse(
            "/dashboard/settings?pw_saved=1", status_code=303)
    else:
        response = JSONResponse(
            {"ok": True,
             "action": "set" if action == "password.set" else "changed"})
    await _set_session_cookie_for(request, response, uid)
    return response


async def _account_page(request: Request, principal: Principal, **extra):
    """Shared renderer for GET /account and the browser form branches
    (Phase 2: the one-time raw API-key display rides on this)."""
    engine = _engine(request)
    user = await UserService(engine).get(principal.user_id)
    linked = await IdentityStore(engine).account_ids_for(
        principal.user_id, "github")
    return _page(
        "account.html", request,
        email=user["email"] if user else "unknown",
        user_email=user["email"] if user else None,
        projects=await ProjectService(engine).list(principal.user_id),
        api_keys=await ApiKeyStore(engine).list(principal.user_id),
        github_linked=bool(linked),
        **extra,
    )


@router.get("/account")
async def account_page(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    return await _account_page(request, principal)


@router.get("/auth/me")
async def me(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    user = await UserService(_engine(request)).get(principal.user_id)
    return {
        "id": principal.user_id,
        "email": user["email"] if user else None,
        "kind": principal.kind,
        "project_id": principal.project_id,
    }


# ---------------------------------------------------------------------------
# Projects


@router.get("/projects")
async def list_projects(
    request: Request,
    include_archived: bool = False,
    principal: Principal = Depends(require_user_session),
):
    projects = await ProjectService(_engine(request)).list(
        principal.user_id, include_archived=include_archived)
    return {"projects": projects}


@router.post("/projects")
async def create_project(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    body = await _payload(request)
    try:
        made = await ProjectService(_engine(request)).create(
            principal.user_id, str(body.get("name", "")))
    except AccountError as exc:
        return _error_response(exc)
    await _audit(request, "project.created", actor_user_id=principal.user_id,
                 resource_type="project", resource_id=str(made["id"]))
    return JSONResponse(made, status_code=201)


@router.patch("/projects/{project_id}")
async def rename_project(
    project_id: int,
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    body = await _payload(request)
    try:
        renamed = await ProjectService(_engine(request)).rename(
            principal.user_id, project_id, str(body.get("name", "")))
    except AccountError as exc:
        return _error_response(exc)
    await _audit(request, "project.renamed", actor_user_id=principal.user_id,
                 resource_type="project", resource_id=str(project_id))
    return renamed


@router.post("/projects/{project_id}/archive")
async def archive_project(
    project_id: int,
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    try:
        archived = await ProjectService(_engine(request)).archive(
            principal.user_id, project_id)
    except AccountError as exc:
        return _error_response(exc)
    await _audit(request, "project.archived", actor_user_id=principal.user_id,
                 resource_type="project", resource_id=str(project_id))
    return archived


# ---------------------------------------------------------------------------
# API keys


@router.get("/api-keys")
async def list_api_keys(
    request: Request,
    principal: Principal = Depends(require_account_admin),
):
    keys = await ApiKeyStore(_engine(request)).list(principal.user_id)
    return {"api_keys": keys}


@router.post("/api-keys")
async def create_api_key(
    request: Request,
    principal: Principal = Depends(require_account_admin),
):
    body = await _payload(request)
    record = await ApiKeyStore(_engine(request)).create(
        principal.user_id, label=str(body.get("label", "")))
    await _audit(request, "auth.api_key_created",
                 actor_user_id=principal.user_id,
                 resource_type="api_key", resource_id=record["prefix"],
                 meta={"label": record["label"]})
    if _wants_html(request):
        # The raw key is shown EXACTLY ONCE, rendered into the page -
        # never in a redirect URL (browsers and history would keep it).
        return await _account_page(request, principal, new_key=record)
    return JSONResponse(record, status_code=201)


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(
    key_id: int,
    request: Request,
    principal: Principal = Depends(require_account_admin),
):
    store = ApiKeyStore(_engine(request))
    owned = [
        key for key in await store.list(principal.user_id)
        if key["id"] == key_id
    ]
    if not owned:
        raise HTTPException(
            status_code=404,
            detail={"error": {"message": "No such API key.",
                              "type": "not_found_error"}},
        )
    revoked = await store.revoke(key_id)
    if revoked:
        await _audit(request, "auth.api_key_revoked",
                     actor_user_id=principal.user_id,
                     resource_type="api_key", resource_id=owned[0]["prefix"])
    if request.headers.get("HX-Request") == "true":
        # HTMX row removal: empty 204 lets hx-swap="delete" drop the row.
        return Response(status_code=204)
    return {"revoked": revoked}


# ---------------------------------------------------------------------------
# Sessions (read-only)


@router.get("/sessions")
async def list_sessions(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    store = getattr(request.app.state, "sessions", None)
    if store is None:
        raise HTTPException(
            status_code=503,
            detail={"error": {"message": "Session storage unavailable.",
                              "type": "config_error"}},
        )
    rows = await store.list_for_user(principal.user_id,
                                     project_id=principal.project_id)
    return {"sessions": rows}


# ---------------------------------------------------------------------------
# Device-code pairing (RFC 8628-flavored)


@router.post("/auth/device/code")
async def device_code_start(request: Request):
    interval = DEFAULT_POLL_INTERVAL
    ttl = DEVICE_CODE_TTL
    request_data = await DeviceCodeStore(_engine(request)).create(
        interval=interval, ttl=ttl)
    base = str(request.base_url).rstrip("/")
    return {
        "device_code": request_data["device_code"],
        "user_code": request_data["user_code"],
        "verification_uri": f"{base}/login",
        # RFC 8628 verification_uri_complete: the code embedded in the
        # URL, so the client can open one link and the user just clicks
        # Approve - no code typing, no URL editing. Without this the
        # flow dead-ends: /login has no code-entry form and redirects
        # signed-in users to /account.
        "verification_uri_complete":
            f"{base}/auth/devices/{request_data['user_code']}",
        "expires_in": request_data["expires_in"],
        "interval": request_data["interval"],
    }


def _device_result(request: Request, title: str, message: str,
                   status_code: int = 200):
    return templates.TemplateResponse(
        request, "device_result.html",
        {"title": title, "message": message},
        status_code=status_code,
    )


@router.get("/auth/devices")
async def device_lookup(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    """Form target for the Account page's "Pair a device" box: takes
    ?code= and redirects to the approval page. Exists because
    /auth/devices/{code} is a path param a plain HTML form can't build -
    and because pairing a browser-less machine means reading a code off
    that machine's screen, with nowhere to type it otherwise."""
    code = (request.query_params.get("code") or "").strip()
    if not code:
        return RedirectResponse("/account", status_code=303)
    return RedirectResponse(
        f"/auth/devices/{code.upper()}", status_code=303)


@router.get("/auth/devices/{user_code}")
async def device_page(
    user_code: str,
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    pending = await DeviceCodeStore(_engine(request)).get_by_user_code(
        user_code)
    if pending is None:
        return _device_result(request, "Unknown or expired code",
                              "Start the pairing flow again.", 404)
    user = await UserService(_engine(request)).get(principal.user_id)
    email = user["email"] if user else "unknown"
    return _page(
        "device.html", request,
        email=email,
        user_code=user_code.strip().upper(),
        error=None,
    )


@router.post("/auth/devices/{user_code}/approve")
async def device_approve(
    user_code: str,
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    approved = await DeviceCodeStore(_engine(request)).approve(
        user_code, principal.user_id)
    if not approved:
        return _device_result(request, "Unknown or expired code",
                              "Start the pairing flow again.", 404)
    await _audit(request, "device.approved", actor_user_id=principal.user_id,
                 resource_type="device_code",
                 resource_id=user_code.strip().upper())
    return _device_result(request, "Device approved",
                          "Return to your terminal - your key is being "
                          "issued.")


@router.post("/auth/devices/{user_code}/deny")
async def device_deny(
    user_code: str,
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    denied = await DeviceCodeStore(_engine(request)).deny(user_code)
    if not denied:
        return _device_result(request, "Unknown or expired code",
                              "Start the pairing flow again.", 404)
    await _audit(request, "device.denied", actor_user_id=principal.user_id,
                 resource_type="device_code",
                 resource_id=user_code.strip().upper())
    return _device_result(request, "Device denied",
                          "The pairing request was rejected.")


@router.post("/auth/device/token")
async def device_token(request: Request):
    form = await _parse_form(request)
    raw = str(form.get("device_code", ""))
    try:
        result = await DeviceCodeStore(_engine(request)).poll(raw)
    except AccountError as exc:
        status = 403 if exc.code == "access_denied" else 400
        return JSONResponse(
            {"error": exc.code, "error_description": exc.message,
             **({"interval": exc.extra["interval"]}
                if "interval" in exc.extra else {})},
            status_code=status,
        )
    if result["status"] == "pending":
        return JSONResponse(
            {"error": "authorization_pending",
             "error_description": "Approve the request in your browser.",
             "interval": result["interval"]},
            status_code=400,
        )
    key = result["api_key"]
    await _audit(request, "device.key_issued", actor_user_id=result["user_id"],
                 resource_type="api_key", resource_id=key["prefix"])
    return {
        "access_token": key["raw"],
        "token_type": "invincible_api_key",
        "prefix": key["prefix"],
        "user_id": result["user_id"],
    }


# ---------------------------------------------------------------------------
# GitHub login


def _github_callback_url(request: Request) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/auth/github/callback"


@router.get("/auth/github/login")
async def github_login(request: Request):
    gh = GitHubOAuth.from_settings()
    if gh is None:
        return _device_result(
            request, "GitHub login disabled",
            "Set INVINCIBLE_GITHUB_CLIENT_ID and "
            "INVINCIBLE_GITHUB_CLIENT_SECRET to enable it.", 503)
    state = secrets.token_urlsafe(16)
    signed = sign_value(state, ttl_seconds=600)
    if signed is None:
        return _device_result(
            request, "Sessions disabled",
            "Set INVINCIBLE_OWNER_SECRET before enabling logins.", 503)
    try:
        url = gh.build_authorize_url(state, _github_callback_url(request))
    finally:
        await gh.aclose()
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        GITHUB_STATE_COOKIE, signed, max_age=600, httponly=True,
        samesite="lax", secure=request.url.scheme == "https", path="/",
    )
    return response


@router.get("/auth/github/callback")
async def github_callback(request: Request):
    gh = GitHubOAuth.from_settings()
    if gh is None:
        return _device_result(request, "GitHub login disabled",
                              "Client credentials are not configured.", 503)

    def _error(_reason: str) -> RedirectResponse:
        response = RedirectResponse("/login?github_error=1", status_code=302)
        response.delete_cookie(GITHUB_STATE_COOKIE, path="/")
        return response

    cookie = request.cookies.get(GITHUB_STATE_COOKIE)
    query_state = request.query_params.get("state", "")
    state_ok = verify_signed_value(cookie, query_state)
    code = request.query_params.get("code", "")
    if not state_ok or not code:
        await gh.aclose()
        await _audit(request, "github.login_failed",
                     resource_type="reason",
                     resource_id="state_mismatch" if not state_ok
                     else "missing_code")
        return _error("state")

    try:
        access_token = await gh.exchange_code(code, _github_callback_url(request))
        profile = await gh.fetch_profile(access_token)
        verified_email = await gh.primary_verified_email(access_token)
    except AccountError as exc:
        await _audit(request, "github.login_failed",
                     resource_type="reason", resource_id=exc.code)
        return _error(exc.code)
    finally:
        await gh.aclose()

    identities = IdentityStore(_engine(request))
    users = UserService(_engine(request))

    user_id = await identities.get_user("github", profile["id"])
    linked = False
    if user_id is None:
        if verified_email is None:
            # Only VERIFIED GitHub emails may auto-link/register.
            await _audit(request, "github.login_failed",
                         resource_type="reason",
                         resource_id="no_verified_email")
            return _error("no_verified_email")
        existing = await users.get_by_email(verified_email)
        if existing is not None:
            # Refuse when a DIFFERENT GitHub identity already owns this
            # account - a second provider id must never attach silently.
            linked_ids = await identities.account_ids_for(
                int(existing["id"]), "github")
            if linked_ids and profile["id"] not in linked_ids:
                await _audit(request, "github.login_failed",
                             actor_user_id=int(existing["id"]),
                             resource_type="reason",
                             resource_id="identity_conflict")
                return _error("identity_conflict")
            user_id = int(existing["id"])
            linked = True
        else:
            created = await users.register_without_password(verified_email)
            user_id = created["id"]
    await identities.link(user_id, "github", profile["id"])

    if linked:
        await _audit(request, "github.linked", actor_user_id=user_id,
                     resource_type="identity",
                     resource_id=f"github:{profile['id']}")
    await _audit(request, "auth.logged_in", actor_user_id=user_id,
                 resource_type="user", resource_id=str(user_id),
                 meta={"provider": "github"})
    await ensure_default_project(_engine(request), user_id)
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(GITHUB_STATE_COOKIE, path="/")
    await _set_session_cookie_for(request, response, user_id)
    return response
