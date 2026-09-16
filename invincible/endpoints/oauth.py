# invincible/endpoints/oauth.py
"""Self-hosted OAuth 2.1 + PKCE authorization server for Invincible.

Powers /mcp with short-lived, revocable Bearer tokens instead of any
per-request shared secret. Dynamic client registration (RFC 7591), RFC 8414
metadata, RFC 9728 protected-resource metadata, and PKCE-only public clients
are implemented because that is what MCP-compatible clients (including the
Claude app's custom-connector flow) expect.

Consent trust boundary (Phase 2): approval requires a logged-in account
session - the same browser identity the dashboard runs under. Every user
approves their OWN clients; the token subject is that user, and with
INVINCIBLE_AGENT_ROUTING on confirmed tool execution routes to their paired
agent, never the server host. The old owner-secret login is gone; the
secret survives only as the account-session signing key (core.accounts
SessionManager).
"""
import html
import logging
import re
from urllib.parse import parse_qsl, quote, urlencode, urlparse

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.responses import RedirectResponse

from invincible.core.accounts import (
    SESSION_COOKIE as ACCOUNT_SESSION_COOKIE,
)
from invincible.core.accounts import resolve_session
from invincible.core.identity import LoginRateLimiter
from invincible.core.oauth_store import (
    ACCESS_TOKEN_TTL,
    OAuthError,
    OAuthStore,
    token_hash,
)

logger = logging.getLogger("invincible.oauth")

router = APIRouter()

# ACCESS_TOKEN_TTL / REFRESH_TOKEN_TTL come from core.oauth_store (single
# source of truth for token lifetimes; the store enforces them, the
# endpoint only reports them in the token response).


async def _audit(request: Request, action: str, **kwargs) -> None:
    """Best-effort audit write; never blocks the OAuth flow."""
    log = getattr(request.app.state, "audit_log", None)
    if log is None:
        return
    try:
        await log.record(action, actor_kind="user", **kwargs)
    except Exception:  # noqa: BLE001 - telemetry only
        logger.warning("audit write failed for %s", action, exc_info=True)

# MEDIUM-4 (2026-09-07 audit): dynamic client registration is open by
# design (the gate is consent, not registration) but needs a per-IP cap
# so one address cannot bloat oauth_clients with junk rows. Scoped
# "client-register" - deliberately separate from any login scope.
REGISTER_MAX_ATTEMPTS = 10
REGISTER_WINDOW_SECONDS = 15 * 60


def _register_limiter(request: Request) -> LoginRateLimiter:
    return LoginRateLimiter(
        request.app.state.engine,
        scope="client-register",
        max_attempts=REGISTER_MAX_ATTEMPTS,
        window_seconds=REGISTER_WINDOW_SECONDS,
    )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"

CONSENT_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Invincible - Connection request</title></head>
<body>
<h1>Connection request</h1>
<p><strong>{client_name}</strong> wants access to your Invincible instance
(via {redirect_uri}).</p>
<p>Approving as <strong>{identity}</strong> &mdash; tokens minted from this
consent act as that user.</p>
<p>Approving issues a short-lived access token for MCP tool calls. You can
revoke every token for this client at any time with
<code>invincible oauth revoke &lt;client_id&gt;</code> or from the MCP
page in the dashboard.</p>
<form method="post" action="/oauth/authorize" style="display:inline">
{hidden_fields}
<input type="hidden" name="action" value="approve">
<button type="submit">Approve</button>
</form>
&nbsp;
<form method="post" action="/oauth/authorize" style="display:inline">
{hidden_fields}
<input type="hidden" name="action" value="deny">
<button type="submit">Deny</button>
</form>
</body>
</html>
"""

ERROR_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Invincible - Request rejected</title></head>
<body>
<h1>Request rejected</h1>
<p>{message}</p>
</body>
</html>
"""

AUTHORIZE_PARAMS = (
    "response_type", "client_id", "redirect_uri", "code_challenge",
    "code_challenge_method", "state", "resource",
)


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


async def _session_user(request: Request) -> dict | None:
    """The logged-in dashboard account a consent approval is granted AS -
    the same identity the account pages run under. There is no other
    consent identity: the owner-secret cookie path was removed in Phase 2.

    Full principal resolution via ``resolve_session``: the cookie must
    verify AND match a live user row whose session_version still equals
    the minted one - a password-orphaned or deleted-account cookie is
    treated like a forged one (SECURITY.md limit 14), never as a login.
    """
    return await resolve_session(
        request.app.state.engine,
        request.cookies.get(ACCOUNT_SESSION_COOKIE),
    )


def _safe_query_value(value: str) -> bool:
    """Only URL-safe characters may be echoed into HTML or redirects."""
    return bool(re.fullmatch(r"[A-Za-z0-9._~\-=%:/?#@!$&'()*+,;\[\]]*", value))


async def _parse_form(request: Request) -> dict:
    """Parse an application/x-www-form-urlencoded body without adding the
    python-multipart dependency (which Starlette's request.form() needs)."""
    body = await request.body()
    try:
        return {
            key: value
            for key, value in parse_qsl(body.decode("utf-8"), keep_blank_values=True)
        }
    except (UnicodeDecodeError, ValueError):
        return {}


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata(request: Request):
    base = _base_url(request)
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
    }


@router.get("/.well-known/oauth-protected-resource")
async def protected_resource_metadata(request: Request):
    base = _base_url(request)
    return {
        "resource": f"{base}/mcp",
        "canonical_uri": f"{base}/mcp",
        "authorization_servers": [base],
    }


@router.get("/.well-known/oauth-protected-resource/{rest:path}")
async def protected_resource_metadata_path_form(request: Request, rest: str):
    """RFC 9728 5.1 path-form discovery: a client may ask for resource
    metadata by appending the resource's path component, e.g.
    ``/.well-known/oauth-protected-resource/mcp``.

    Only the MCP resource is published today, so unknown suffixes return
    404. An empty path component (trailing slash on the well-known URL)
    is treated the same as the root form.
    """
    if rest.strip("/") not in ("", "mcp"):
        return JSONResponse({"error": "not_found"}, status_code=404)
    return await protected_resource_metadata(request)


@router.post("/oauth/register")
async def oauth_register(request: Request):
    """RFC 7591 dynamic client registration. Open by design - the real
    gate is the account-session consent on /oauth/authorize, not
    registration.
    MEDIUM-4: per-IP fixed-window cap (every attempt counts, successful
    or not - each is a potential oauth_clients row) so the table cannot
    be bloated by one address hammering the endpoint."""
    ip = _client_ip(request)
    limiter = _register_limiter(request)
    locked_for = await limiter.locked_out(ip)
    if locked_for is not None:
        await _audit(request, "oauth.register_limited",
                     resource_type="client_ip", resource_id=ip)
        return JSONResponse(
            {"error": "rate_limited",
             "error_description":
                 f"Too many registrations; retry in {locked_for}s."},
            status_code=429,
        )
    await limiter.record_failure(ip)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": "invalid_request",
             "error_description": "request body must be JSON"},
            status_code=400,
        )
    if not isinstance(body, dict):
        return JSONResponse(
            {"error": "invalid_request",
             "error_description": "request body must be an object"},
            status_code=400,
        )
    redirect_uris = body.get("redirect_uris")
    client_name = body.get("client_name", "")
    if not isinstance(client_name, str):
        client_name = ""
    store: OAuthStore = request.app.state.oauth_store
    try:
        registration = await store.register_client(redirect_uris, client_name)
    except OAuthError as exc:
        return JSONResponse(
            {"error": exc.error, "error_description": exc.description or ""},
            status_code=400,
        )
    return JSONResponse(registration, status_code=201)


async def _authorize_context(request: Request, source) -> dict | None:
    """Validate an authorization request. Returns the normalized params
    (plus the registered client) or None, in which case the caller must
    answer with an error page - an unregistered redirect_uri is never
    redirected to. `source` is the query string (GET) or the login-form
    body (POST)."""
    params = {key: source.get(key, "") for key in AUTHORIZE_PARAMS}
    if params["response_type"] != "code":
        return None
    if params["code_challenge_method"] not in ("S256", ""):
        return None
    if params["code_challenge"] and not _safe_query_value(params["code_challenge"]):
        return None
    store: OAuthStore = request.app.state.oauth_store
    client = await store.get_client(params["client_id"])
    if client is None:
        return None
    redirect_uri = params["redirect_uri"]
    if redirect_uri not in client["redirect_uris"]:
        return None
    parsed = urlparse(redirect_uri)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        return None
    return {**params, "_client": client}


def _reject(message: str) -> HTMLResponse:
    return HTMLResponse(
        ERROR_HTML.format(message=html.escape(message)),
        status_code=400,
    )


def _hidden_fields(params: dict) -> str:
    """Render the authorize params as hidden form inputs for the consent
    forms. html.escape covers the quoting; values were already validated
    by _authorize_context."""
    return "".join(
        f'<input type="hidden" name="{key}" value="{html.escape(value)}">'
        for key, value in params.items()
        if key in AUTHORIZE_PARAMS and value
    )


@router.get("/oauth/authorize")
async def oauth_authorize(request: Request):
    """Account-session gate followed by the consent page. Approving or
    denying is only possible via the POST forms - a GET carrying an `action` is
    rejected, so a cross-site navigation can never grant consent (the
    SameSite=Lax session cookie is sent on top-level GET navigations, which
    made the old GET links CSRF-able). Anonymous browsers bounce to /login
    with the authorize URL as the same-origin ``next`` target, so the
    approval funnel survives the login round-trip."""
    context = await _authorize_context(request, request.query_params)
    if context is None:
        return _reject("Invalid or unregistered authorization request.")
    if "action" in request.query_params:
        return HTMLResponse(
            ERROR_HTML.format(
                message="Consent actions must be submitted with the "
                "Approve/Deny buttons (POST), not links."
            ),
            status_code=405,
        )
    session_user = await _session_user(request)
    if session_user is None:
        target = request.url.path
        if request.url.query:
            target += f"?{request.url.query}"
        return RedirectResponse(
            f"/login?next={quote(target, safe='')}", status_code=302
        )

    client = context["_client"]
    client_name = client["client_name"] or client["client_id"]

    return HTMLResponse(
        CONSENT_HTML.format(
            client_name=html.escape(client_name),
            redirect_uri=html.escape(context["redirect_uri"]),
            identity=html.escape(session_user["email"]),
            hidden_fields=_hidden_fields(context),
        )
    )


def _redirect_with_params(redirect_uri: str, params: dict) -> RedirectResponse:
    query = {key: value for key, value in params.items() if value}
    separator = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        f"{redirect_uri}{separator}{urlencode(query)}", status_code=302
    )


@router.post("/oauth/authorize")
async def oauth_authorize_consent(request: Request):
    """POST /oauth/authorize is the consent form (``action=approve|deny`` +
    authorize params). A live account session is required; POST is what
    makes this safe from CSRF - the SameSite=Lax session cookie is not
    sent on cross-site POSTs. Any other body shape (no action, leftover
    owner-secret fields from the removed login form) is a plain 400.
    """
    form = await _parse_form(request)
    context = await _authorize_context(request, form)
    if context is None:
        return _reject("Invalid or unregistered authorization request.")

    action = form.get("action", "")
    if action not in ("approve", "deny"):
        return _reject("Unknown consent action.")

    session_user = await _session_user(request)
    if session_user is None:
        return HTMLResponse(
            ERROR_HTML.format(message="Not authenticated. Log in first."),
            status_code=401,
        )

    if action == "approve":
        store: OAuthStore = request.app.state.oauth_store
        # Phase 2: the approving identity is always the logged-in account;
        # tokens minted from this consent act as that user.
        uid = session_user["id"]
        await store.attach_owner(context["_client"]["client_id"], uid)
        code = await store.create_code(
            context["_client"]["client_id"],
            context["redirect_uri"],
            context["code_challenge"],
            subject_user_id=uid,
        )
        await _audit(
            request, "oauth.grant_approved",
            actor_user_id=uid,
            resource_type="oauth_client",
            resource_id=context["_client"]["client_id"],
        )
        return _redirect_with_params(
            context["redirect_uri"], {"code": code, "state": context["state"]},
        )
    return _redirect_with_params(
        context["redirect_uri"],
        {"error": "access_denied", "state": context["state"]},
    )


def _token_error(message: str, desc: str = "") -> JSONResponse:
    return JSONResponse(
        {"error": message, "error_description": desc}, status_code=400
    )


def _token_response(pair: dict) -> JSONResponse:
    return JSONResponse({
        "access_token": pair["access_token"],
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL,
        "refresh_token": pair["refresh_token"],
    })


@router.post("/oauth/token")
async def oauth_token(request: Request):
    form = await _parse_form(request)
    grant_type = str(form.get("grant_type", ""))
    store: OAuthStore = request.app.state.oauth_store

    if grant_type == "authorization_code":
        code = str(form.get("code", ""))
        client_id = str(form.get("client_id", ""))
        redirect_uri = str(form.get("redirect_uri", ""))
        verifier = str(form.get("code_verifier", ""))
        if not code or not client_id or not redirect_uri or not verifier:
            return _token_error(
                "invalid_request",
                "code, client_id, redirect_uri and code_verifier are required",
            )
        try:
            subject = await store.consume_code_subject(
                code, client_id, redirect_uri, verifier
            )
        except OAuthError as exc:
            return _token_error(exc.error, exc.description or "")
        pair = await store.issue_token_pair(client_id, subject)
        await _audit(request, "oauth.token_issued",
                     actor_user_id=subject,
                     resource_type="oauth_client",
                     resource_id=client_id,
                     meta={"grant_type": "authorization_code"})
        return _token_response(pair)

    if grant_type == "refresh_token":
        refresh = str(form.get("refresh_token", ""))
        if not refresh:
            return _token_error("invalid_request", "refresh_token is required")
        try:
            pair = await store.rotate_refresh(refresh)
        except OAuthError as exc:
            return _token_error(exc.error, exc.description or "")
        return _token_response(pair)

    return _token_error(
        "unsupported_grant_type", f"unsupported grant_type: {grant_type}"
    )


@router.post("/oauth/revoke")
async def oauth_revoke(request: Request):
    """RFC 7009 revocation. Always answers 200 - an unknown or already
    revoked token counts as successfully revoked."""
    form = await _parse_form(request)
    token = str(form.get("token", ""))
    if not token:
        return _token_error("invalid_request", "token is required")
    store: OAuthStore = request.app.state.oauth_store
    revoked = await store.revoke(token)
    if revoked:
        await _audit(request, "oauth.token_revoked",
                     resource_type="oauth_token",
                     resource_id=token_hash(token)[:12])
    return Response(status_code=200)
