# invincible/endpoints/byok.py
"""Per-user BYOK provider connections (Platform Phase 9, PR-B).

Cookie-realm ONLY (``require_user_session`` - same realm as the rest of
the dashboard; ``inv_*`` API keys never authorize this surface). Every
route additionally fails CLOSED with 503 when INVINCIBLE_CREDENTIAL_KEY
is unset/malformed: stored user keys are never written or read without
the encryption master key.

Wire shapes: create/list/test/remove over ``/providers/mine`` plus the
``/dashboard/providers`` HTML page. Responses carry the one-way
``key_masked`` hint only - the plaintext key is never echoed, logged, or
audited after the initial write.
"""
import logging
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from invincible.core import credential_crypto
from invincible.core.config import resolve_timeout
from invincible.core.credential_store import (
    ByokCredentialStore,
    DuplicateCredentialError,
)
from invincible.core.principal import Principal
from invincible.core.provider_catalog import CATALOG, catalog_entry
from invincible.core.trimming import DEFAULT_MAX_CONTEXT
from invincible.core.url_safety import UnsafeUrlError, validate_public_https_url
from invincible.endpoints.accounts import (
    _audit,
    _page,
    _payload,
    _wants_html,
    require_user_session,
)
from invincible.endpoints.dashboard import _email

logger = logging.getLogger("invincible.byok")


def _require_byok_enabled() -> None:
    """Fail-closed gate: no usable master key, no BYOK surface at all."""
    if not credential_crypto.usable():
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "message": "BYOK provider connections are disabled: "
                               "INVINCIBLE_CREDENTIAL_KEY is not configured.",
                    "type": "config_error",
                }
            },
        )


router = APIRouter(dependencies=[Depends(_require_byok_enabled)])


def _bad_request(message: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"error": {"message": message, "type": "invalid_request_error"}},
    )


def _not_found() -> HTTPException:
    # Foreign and unknown ids are indistinguishable (anti-enumeration).
    return HTTPException(
        status_code=404,
        detail={"error": {"message": "No such provider credential.",
                          "type": "not_found_error"}},
    )


def _store(request: Request) -> ByokCredentialStore:
    return ByokCredentialStore(request.app.state.engine)


def _audit_meta(row: dict) -> dict:
    """Metadata safe for audit rows: identity only - never the key, and
    never the base URL (it may embed auth params)."""
    meta = {"provider_name": row["provider_name"]}
    if row.get("catalog_key"):
        meta["catalog_key"] = row["catalog_key"]
    return meta


async def byok_attempt_source(request: Request, principal: Principal):
    """Candidate pool + key resolver for a BYOK-scoped chat request
    (Platform Phase 9, PR-C).

    Returns ``None`` when the principal rides the operator pool unchanged
    (legacy gateway key / anonymous local identity). Otherwise returns
    ``(candidates, key_resolver)`` built entirely from this user's
    ``user_provider_credentials`` rows - an EMPTY candidate list means the
    user has connected nothing, and callers must fail fast with a clear
    400-class response. There is no fallback to the operator's shared
    ProviderRegistry in either direction.

    ``key_resolver(provider)`` is awaited once per attempt by the Router
    (lazy decryption - never eagerly for the whole list): it re-fetches
    the row, re-runs the SSRF guard on the stored URL (a DNS rebind
    between connect and use must not bypass it), and decrypts. Any
    unusable credential (vanished row, unsafe URL, undecryptable
    ciphertext) logs a warning and returns None, which the Router treats
    exactly like a missing env key - skip to the next attempt.
    """
    if principal.kind != "api_key":
        return None
    store = ByokCredentialStore(request.app.state.engine)
    rows = await store.routing_rows(principal.user_id)
    user_id = principal.user_id

    async def resolve_key(provider: dict) -> str | None:
        row = await store.get_for_user(
            provider.get("byok_credential_id"), user_id)
        if row is None:
            logger.warning("BYOK credential vanished mid-request; skipping")
            return None
        entry = catalog_entry(row.get("catalog_key"))
        if not (entry and row["base_url"] == entry["base_url"]):
            try:
                validate_public_https_url(row["base_url"])
            except UnsafeUrlError as e:
                logger.warning(
                    "BYOK base URL no longer safe (%s); skipping", e)
                return None
        try:
            return credential_crypto.decrypt(row["encrypted_api_key"])
        except (credential_crypto.CredentialKeyError,
                credential_crypto.CredentialDecryptError) as e:
            logger.warning("BYOK credential unusable: %s", e)
            return None

    candidates = []
    for index, row in enumerate(rows):
        entry = catalog_entry(row.get("catalog_key"))
        candidates.append({
            "name": row["provider_name"],
            "tier": index + 1,
            "base_url": row["base_url"],
            "model_id": row["model_id"],
            "max_context": (
                entry["max_context"] if entry else DEFAULT_MAX_CONTEXT),
            "enabled": True,
            "health_id": f"byok:{row['id']}",
            "byok_credential_id": row["id"],
        })
    return candidates, resolve_key


async def _probe(request: Request, base_url: str, api_key: str) -> dict:
    """Read-only GET against the provider's /models endpoint (no tokens
    burned), mirroring ProviderRegistry.test()'s report shape. The httpx
    client is injectable via ``app.state.byok_http_client`` so tests run
    on MockTransport."""
    client = getattr(request.app.state, "byok_http_client", None)
    owns_client = client is None
    client = client or httpx.AsyncClient()
    started = time.monotonic()
    try:
        resp = await client.get(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=resolve_timeout({}),
        )
        latency_ms = round((time.monotonic() - started) * 1000)
        detail = "" if resp.status_code == 200 else f"HTTP {resp.status_code}"
        return {
            "ok": resp.status_code == 200,
            "status": resp.status_code,
            "latency_ms": latency_ms,
            "detail": detail,
        }
    except httpx.RequestError as e:
        return {
            "ok": False,
            "status": None,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "detail": type(e).__name__,
        }
    finally:
        if owns_client:
            await client.aclose()


@router.get("/dashboard/providers")
async def providers_page(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    rows = await _store(request).list_for_user(principal.user_id)
    # Phase 9 PR-D: the catalog renders as connect cards over the
    # operator-supplied constants; a card whose catalog_key is already
    # connected shows a connected state instead of a blank form.
    catalog = [
        {"key": key, "label": entry["label"],
         "base_url": entry["base_url"], "model_id": entry["model_id"]}
        for key, entry in CATALOG.items()
    ]
    connected_keys = {r["catalog_key"] for r in rows if r.get("catalog_key")}
    return _page(
        "providers.html", request,
        user_email=await _email(request.app.state.engine, principal),
        rows=rows,
        catalog=catalog,
        connected_keys=connected_keys,
        connected=request.query_params.get("connected") == "1",
        tested=request.query_params.get("tested"),
        test_error=request.query_params.get("test_error") == "1",
    )


@router.get("/providers/mine")
async def list_mine(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    rows = await _store(request).list_for_user(principal.user_id)
    return {"providers": rows, "count": len(rows)}


@router.post("/providers/mine", status_code=201)
async def connect_provider(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    body = await _payload(request)
    provider_name = str(body.get("provider_name") or "").strip()
    api_key = str(body.get("api_key") or "").strip()
    catalog_key = body.get("catalog_key") or None
    if catalog_key is not None:
        catalog_key = str(catalog_key).strip() or None

    if not provider_name:
        raise _bad_request("provider_name is required")
    if len(provider_name) > 80:
        raise _bad_request("provider_name must be at most 80 characters")
    if not api_key:
        raise _bad_request("api_key is required")
    if len(api_key) > 4096:
        raise _bad_request("api_key is too long")

    entry = catalog_entry(catalog_key)
    if catalog_key is not None and entry is None:
        raise _bad_request(f"Unknown catalog_key '{catalog_key}'")

    base_url = str(body.get("base_url") or "").strip() or (
        entry["base_url"] if entry else ""
    )
    model_id = str(body.get("model_id") or "").strip() or (
        entry["model_id"] if entry else ""
    )
    if not base_url:
        raise _bad_request("base_url is required")
    if not model_id:
        raise _bad_request("model_id is required")

    # SSRF guard: catalog entries skip the check only while the stored
    # URL equals the operator-supplied constant; any user-edited URL is
    # fully custom input and validated as such.
    uses_catalog_constant = bool(entry) and base_url == entry["base_url"]
    if not uses_catalog_constant:
        try:
            validate_public_https_url(base_url)
        except UnsafeUrlError as e:
            raise _bad_request(f"base URL rejected: {e}") from None

    try:
        row = await _store(request).create(
            user_id=principal.user_id,
            provider_name=provider_name,
            model_id=model_id,
            base_url=base_url,
            api_key=api_key,
            catalog_key=catalog_key,
        )
    except DuplicateCredentialError as e:
        raise _bad_request(str(e)) from None

    await _audit(
        request, "byok.credential.created",
        actor_user_id=principal.user_id,
        resource_type="user_provider_credential",
        resource_id=str(row["id"]),
        meta=_audit_meta(row),
    )
    if _wants_html(request):
        # Browser form posts redirect back to the page with a bounded
        # flash flag; JSON clients keep the 201-row wire shape.
        return RedirectResponse(
            "/dashboard/providers?connected=1", status_code=303)
    return row


@router.post("/providers/mine/{credential_id}/test")
async def test_provider(
    credential_id: int,
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    store = _store(request)
    row = await store.get_for_user(credential_id, principal.user_id)
    if row is None:
        raise _not_found()

    # Re-check on EVERY use, not just creation - a DNS rebind between
    # "add" and "test" must not bypass the guard.
    try:
        validate_public_https_url(row["base_url"])
    except UnsafeUrlError:
        await store.update_test_outcome(credential_id, "failed")
        await _audit(request, "byok.credential.tested",
                     actor_user_id=principal.user_id,
                     resource_type="user_provider_credential",
                     resource_id=str(credential_id),
                     meta={**_audit_meta(row), "outcome": "blocked_url"})
        if request.headers.get("HX-Request") == "true":
            return Response(status_code=204, headers={
                "HX-Redirect": "/dashboard/providers?test_error=1"})
        raise _bad_request(
            "base URL rejected: it now resolves to a blocked address"
        ) from None

    try:
        api_key = credential_crypto.decrypt(row["encrypted_api_key"])
    except credential_crypto.CredentialKeyError as e:
        logger.warning("BYOK test refused: %s", e)
        raise HTTPException(
            status_code=503,
            detail={"error": {"message": "BYOK provider connections are "
                                         "disabled.", "type": "config_error"}},
        ) from None
    except credential_crypto.CredentialDecryptError as e:
        logger.warning("BYOK credential undecryptable: %s", e)
        raise HTTPException(
            status_code=503,
            detail={"error": {
                "message": "Stored credential cannot be decrypted under the "
                           "configured INVINCIBLE_CREDENTIAL_KEY; re-connect "
                           "the provider.",
                "type": "config_error"}},
        ) from None

    report = await _probe(request, row["base_url"], api_key)
    credential_status = "ok" if report["ok"] else "failed"
    await store.update_test_outcome(credential_id, credential_status)
    await _audit(
        request, "byok.credential.tested",
        actor_user_id=principal.user_id,
        resource_type="user_provider_credential",
        resource_id=str(credential_id),
        meta={**_audit_meta(row), "outcome": credential_status},
    )
    if request.headers.get("HX-Request") == "true":
        # The Test button is an HTMX post; bounce back to the page with a
        # bounded flash flag instead of rendering the JSON report.
        return Response(status_code=204, headers={
            "HX-Redirect":
                f"/dashboard/providers?tested={credential_status}"})
    return {**report, "credential_status": credential_status}


@router.delete("/providers/mine/{credential_id}")
async def delete_provider(
    credential_id: int,
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    store = _store(request)
    row = await store.get_for_user(credential_id, principal.user_id)
    if row is None or not await store.delete(credential_id,
                                             principal.user_id):
        raise _not_found()
    await _audit(request, "byok.credential.deleted",
                 actor_user_id=principal.user_id,
                 resource_type="user_provider_credential",
                 resource_id=str(credential_id),
                 meta=_audit_meta(row))
    if request.headers.get("HX-Request") == "true":
        # HTMX row removal: empty 204 lets hx-swap="delete" drop the row.
        return Response(status_code=204)
    return {"deleted": True}
