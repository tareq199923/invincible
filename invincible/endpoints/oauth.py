# invincible/endpoints/oauth.py
"""Self-hosted OAuth 2.1 + PKCE authorization server for Invincible.

Replaces the old per-request X-MCP-Secret header on /mcp. The operator's
secret (INVINCIBLE_OWNER_SECRET, legacy alias MCP_SHARED_SECRET) is no
longer sent on every /mcp call; it is used once per browser session to log
in on /oauth/authorize, and real /mcp traffic is authorized with short-lived,
revocable Bearer tokens.

Purpose-built for a single operator - no external identity provider, no
hosted relay. Dynamic client registration (RFC 7591), RFC 8414 metadata,
RFC 9728 protected-resource metadata, and PKCE-only public clients are
implemented because that is what MCP-compatible clients (including the
Claude app's custom-connector flow) expect.
"""
import hashlib
import hmac
import html
import logging
import os
import re
import time
from urllib.parse import parse_qsl, urlencode, urlparse

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.responses import RedirectResponse

from invincible.core.oauth_store import OAuthError, OAuthStore

logger = logging.getLogger("invincible.oauth")

router = APIRouter()

OWNER_SECRET_ENV = "INVINCIBLE_OWNER_SECRET"
LEGACY_OWNER_SECRET_ENV = "MCP_SHARED_SECRET"
SESSION_COOKIE = "invincible_owner"
SESSION_TTL = 30 * 24 * 3600  # "remember this browser" session cookie TTL
ACCESS_TOKEN_TTL = 3600
REFRESH_TOKEN_TTL = 30 * 24 * 3600

_legacy_warned = False

LOGIN_FORM_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Invincible - Owner login</title></head>
<body>
<h1>Invincible</h1>
<p>This instance is asking you to approve a connection to its MCP tools.
Authenticate as the owner to continue.</p>
<form method="post" action="/oauth/authorize">
{preserved_params}
<label for="owner_secret">Owner secret</label>
<input type="password" id="owner_secret" name="owner_secret" autofocus required>
<button type="submit">Log in</button>
</form>
{error_block}
</body>
</html>
"""

CONSENT_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Invincible - Connection request</title></head>
<body>
<h1>Connection request</h1>
<p><strong>{client_name}</strong> wants access to your Invincible instance
(via {redirect_uri}).</p>
<p>Approving issues a short-lived access token for MCP tool calls. You can
revoke every token for this client at any time with
<code>invincible oauth revoke &lt;client_id&gt;</code>.</p>
<p>
<a href="{approve_query}">Approve</a> &nbsp;|&nbsp;
<a href="{deny_query}">Deny</a>
</p>
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


def owner_secret() -> str | None:
    """Return the owner-login secret, falling back to the legacy
    MCP_SHARED_SECRET alias (one-time deprecation notice) so existing .env
    values keep working."""
    global _legacy_warned
    secret = os.environ.get(OWNER_SECRET_ENV)
    if secret:
        return secret
    secret = os.environ.get(LEGACY_OWNER_SECRET_ENV)
    if secret and not _legacy_warned:
        _legacy_warned = True
        logger.warning(
            "%s set but %s is not - using it as the owner-login secret. "
            "Rename the key in your .env file.",
            LEGACY_OWNER_SECRET_ENV, OWNER_SECRET_ENV,
        )
    return secret


def _cookie_key() -> bytes:
    """HMAC key for the owner session cookie, derived from the owner secret
    so a restart keeps existing browser sessions valid (rotating the secret
    logs every browser out - that is the expected trade-off)."""
    return hashlib.sha256((owner_secret() or "").encode("utf-8")).digest()


def _sign_cookie() -> str:
    payload = str(int(time.time()))
    signature = hmac.new(
        _cookie_key(), payload.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{signature}"


def _verify_cookie(value: str) -> bool:
    try:
        payload, signature = value.split(".", 1)
        expected = hmac.new(
            _cookie_key(), payload.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return False
        created = int(payload)
    except (ValueError, TypeError):
        return False
    return time.time() - created < SESSION_TTL


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _has_valid_cookie(request: Request) -> bool:
    # No owner secret configured means no one can log in, so no cookie may
    # ever count as valid - the HMAC key would be sha256(b""), which anyone
    # can compute, making forged cookies trivial.
    if not owner_secret():
        return False
    cookie = request.cookies.get(SESSION_COOKIE)
    return bool(cookie and _verify_cookie(cookie))


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


@router.post("/oauth/register")
async def oauth_register(request: Request):
    """RFC 7591 dynamic client registration. Open by design - the real
    gate is the operator's consent on /oauth/authorize, not registration."""
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


def _login_page(
    params: dict, error_block: str = "", status_code: int = 200
) -> HTMLResponse:
    hidden = "".join(
        f'<input type="hidden" name="{key}" value="{html.escape(value)}">'
        for key, value in params.items()
        if key in AUTHORIZE_PARAMS and value
    )
    return HTMLResponse(
        LOGIN_FORM_HTML.format(preserved_params=hidden, error_block=error_block),
        status_code=status_code,
    )


def _consent_url(params: dict, action: str) -> str:
    query = urlencode(
        {key: value for key, value in params.items()
         if key in AUTHORIZE_PARAMS and value}
        | {"action": action}
    )
    return f"/oauth/authorize?{query}"


@router.get("/oauth/authorize")
async def oauth_authorize(request: Request):
    """Owner-login gate followed by the consent page. Approve/Deny links
    carry the original parameters plus an `action` flag."""
    context = await _authorize_context(request, request.query_params)
    if context is None:
        return _reject("Invalid or unregistered authorization request.")
    if not owner_secret():
        return HTMLResponse(
            ERROR_HTML.format(
                message="No owner secret is configured; set "
                "INVINCIBLE_OWNER_SECRET and restart. Authorization is "
                "disabled until then."
            ),
            status_code=503,
        )
    if not _has_valid_cookie(request):
        return _login_page(context)

    client = context["_client"]
    client_name = client["client_name"] or client["client_id"]
    action = request.query_params.get("action", "")

    if action == "approve":
        store: OAuthStore = request.app.state.oauth_store
        code = await store.create_code(
            client["client_id"], context["redirect_uri"], context["code_challenge"],
        )
        return _redirect_with_params(
            context["redirect_uri"], {"code": code, "state": context["state"]},
        )
    if action == "deny":
        return _redirect_with_params(
            context["redirect_uri"],
            {"error": "access_denied", "state": context["state"]},
        )

    return HTMLResponse(
        CONSENT_HTML.format(
            client_name=html.escape(client_name),
            redirect_uri=html.escape(context["redirect_uri"]),
            approve_query=_consent_url(context, "approve"),
            deny_query=_consent_url(context, "deny"),
        )
    )


def _redirect_with_params(redirect_uri: str, params: dict) -> RedirectResponse:
    query = {key: value for key, value in params.items() if value}
    separator = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        f"{redirect_uri}{separator}{urlencode(query)}", status_code=302
    )


@router.post("/oauth/authorize")
async def oauth_authorize_login(request: Request):
    """Owner-login form submission. On the correct secret, sets the signed
    session cookie and bounces back to the GET consent flow."""
    form = await _parse_form(request)
    context = await _authorize_context(request, form)
    if context is None:
        return _reject("Invalid or unregistered authorization request.")
    attempted = str(form.get("owner_secret", ""))
    expected = owner_secret() or ""
    if not expected:
        return _login_page(
            context,
            "<p style='color:#900'>No owner secret is configured; "
            "set INVINCIBLE_OWNER_SECRET and restart.</p>",
            status_code=503,
        )
    if not hmac.compare_digest(
        hashlib.sha256(attempted.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    ):
        return _login_page(
            context, "<p style='color:#900'>Incorrect owner secret.</p>",
            status_code=401,
        )
    query = urlencode(
        {key: value for key, value in context.items()
         if key in AUTHORIZE_PARAMS and value}
    )
    response = RedirectResponse(f"/oauth/authorize?{query}", status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        _sign_cookie(),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


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
            await store.consume_code(code, client_id, redirect_uri, verifier)
        except OAuthError as exc:
            return _token_error(exc.error, exc.description or "")
        pair = await store.issue_token_pair(client_id)
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
    await store.revoke(token)
    return Response(status_code=200)
