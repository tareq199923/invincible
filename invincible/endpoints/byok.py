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
    UnknownCredentialError,
)
from invincible.core.memory_projection import source_color
from invincible.core.principal import Principal
from invincible.core.provider_catalog import CATALOG, catalog_entry
from invincible.core.trimming import DEFAULT_MAX_CONTEXT
from invincible.core.url_safety import UnsafeUrlError, validate_public_https_url
from invincible.core.user_settings_store import (
    UserSettingsStore,
    routing_config_from_user,
)
from invincible.endpoints.accounts import (
    _audit,
    _page,
    _payload,
    _wants_html,
    require_user_session,
)
from invincible.endpoints.dashboard import _email, templates

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


async def byok_attempt_source(
    request: Request, principal: Principal, model: str | None = None,
):
    """Candidate pool + key resolver + routing config + overrides for a
    BYOK-scoped chat request (Platform Phase 9, PR-C; Phase 1 grew the
    tuple with the user's own routing mode and pipeline overrides).

    Returns the ``(candidates, key_resolver, routing_config, overrides)``
    tuple built entirely from this user's ``user_provider_credentials`` +
    ``user_settings`` rows - an EMPTY candidate list means the user has
    connected nothing, and callers must fail fast with a clear 400-class
    response. There is no fallback to a shared pool in either
    direction.

    ``routing_config`` is the user's auto/pinned/chain mode over those
    candidates, with the request's ``model`` already applied (a chain
    step naming that model floats to the front, so the client's model
    choice still picks the entry point). Malformed stored settings
    degrade to auto - never raise on the request path.

    ``key_resolver(provider)`` is awaited once per attempt by the Router
    (lazy decryption - never eagerly for the whole list): it re-fetches
    the row, re-runs the SSRF guard on the stored URL (a DNS rebind
    between connect and use must not bypass it), and decrypts. Any
    unusable credential (vanished row, unsafe URL, undecryptable
    ciphertext) logs a warning and returns None, which the Router treats
    exactly like a missing env key - skip to the next attempt.
    """
    # Defensive: /v1/* only ever mints api_key principals now (the legacy
    # gateway-key/anonymous realms are gone), so a non-api_key kind means
    # something reached the BYOK path through an unexpected route.
    if principal.kind != "api_key":
        return None
    store = ByokCredentialStore(request.app.state.engine)
    rows = await store.routing_rows(principal.user_id)
    user_id = principal.user_id
    stored = await UserSettingsStore(request.app.state.engine).get(user_id)
    routing = routing_config_from_user(
        stored["routing"], rows, model)
    overrides = stored["overrides"]

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
    return candidates, resolve_key, routing, overrides


async def _probe(request: Request, base_url: str, api_key: str) -> dict:
    """Read-only GET against the provider's /models endpoint (no tokens
    burned). The httpx client is injectable via
    ``app.state.byok_http_client`` so tests run on MockTransport."""
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
        ok = resp.status_code == 200
        if ok:
            # A 200 that is not JSON is a website/WAF page, not an API
            # (seen in production: a base URL pointing at the provider's
            # homepage, and an Aliyun WAF challenge, both answered 200
            # HTML). Without this check the badge said "ok" for a
            # credential that could never serve a completion.
            ctype = (resp.headers.get("content-type") or "").split(";")[0]
            if ctype != "application/json":
                ok = False
                detail = f"not an API (content-type {ctype or 'unknown'})"
        return {
            "ok": ok,
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


def _row_response(request: Request, row: dict, status: str) -> Response:
    """Re-rendered provider row for the HTMX Test button
    (hx-target="closest tr" + hx-swap="outerHTML"): the status badge
    updates in place, no full-page reload. Built from explicit public
    fields only - the encrypted key never reaches a template context."""
    r = {
        "id": row["id"],
        "provider_name": row["provider_name"],
        "model_id": row["model_id"],
        "base_url": row["base_url"],
        "key_masked": row["key_masked"],
        "catalog_key": row.get("catalog_key"),
        "status": status,
        "color": source_color(row.get("catalog_key") or row["provider_name"]),
    }
    return templates.TemplateResponse(request, "_provider_row.html", {"r": r})


@router.get("/dashboard/providers")
async def providers_page(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    rows = await _store(request).list_for_user(principal.user_id)
    for r in rows:
        r["color"] = source_color(r.get("catalog_key") or r["provider_name"])
    # Phase 1 self-service routing: the stored auto/chain/pinned mode plus
    # per-step model pre-fills for the routing form. Stored JSON is never
    # trusted for shape - anything malformed renders as the auto default.
    stored = await UserSettingsStore(
        request.app.state.engine).routing_for(principal.user_id)
    routing_mode = stored.get("mode") if isinstance(stored, dict) else None
    if routing_mode not in ("auto", "chain", "pinned"):
        routing_mode = "auto"
    chain_prefill = {}
    if isinstance(stored.get("chain"), list):
        for step in stored["chain"]:
            if (isinstance(step, dict)
                    and isinstance(step.get("credential_id"), int)
                    and isinstance(step.get("model"), str)
                    and step["model"].strip()):
                chain_prefill[step["credential_id"]] = step["model"]
    pinned = stored.get("pinned") if isinstance(stored.get("pinned"), dict) else {}
    pinned_credential_id = (
        pinned.get("credential_id")
        if isinstance(pinned.get("credential_id"), int) else None)
    pinned_model = (
        pinned.get("model") if isinstance(pinned.get("model"), str) else "")
    # Phase 9 PR-D: the catalog renders as connect cards over the
    # operator-supplied constants; a card whose catalog_key is already
    # connected shows a connected state instead of a blank form. The
    # color is a deterministic per-provider hue for the card mark.
    catalog = [
        {"key": key, "label": entry["label"],
         "base_url": entry["base_url"], "model_id": entry["model_id"],
         "color": source_color(key)}
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
        routing_mode=routing_mode,
        chain_prefill=chain_prefill,
        pinned_credential_id=pinned_credential_id,
        pinned_model=pinned_model,
        routing_saved=request.query_params.get("routing_saved") == "1",
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
    # URL equals the packaged catalog constant; any user-edited URL is
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
        await store.update_test_outcome(credential_id, "failed",
                                        user_id=principal.user_id)
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
    await store.update_test_outcome(credential_id, credential_status,
                                    user_id=principal.user_id)
    await _audit(
        request, "byok.credential.tested",
        actor_user_id=principal.user_id,
        resource_type="user_provider_credential",
        resource_id=str(credential_id),
        meta={**_audit_meta(row), "outcome": credential_status},
    )
    if request.headers.get("HX-Request") == "true":
        # The Test button swaps its row in place (T0-3): the re-rendered
        # <tr> carries the updated status badge, so no page reload and no
        # redirect flash. The ?tested= banners stay reachable for direct
        # (non-htmx) browser hits.
        return _row_response(request, row, credential_status)
    return {**report, "credential_status": credential_status}


@router.post("/providers/mine/{credential_id}/move")
async def move_provider(
    credential_id: int,
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    """Swap a credential with its neighbor in the user's order (Phase 1
    self-service). Two callers, both form-encoded: the HTMX ▲/▼ buttons in
    the connected-providers table (hx-vals ``direction``; the response
    re-renders the whole ``<tbody>`` partial), and the chain-step buttons in
    the routing form, which ride the form's submit via ``formaction`` (the
    endpoint ignores the extra routing fields). Non-HTMX callers get a
    redirect back to the routing section."""
    store = _store(request)
    rows = await store.list_for_user(principal.user_id)
    ids = [r["id"] for r in rows]
    if credential_id not in ids:
        raise _not_found()
    body = await _payload(request)
    direction = str(body.get("direction") or "").strip().lower()
    if direction not in ("up", "down"):
        raise _bad_request("direction must be 'up' or 'down'")
    index = ids.index(credential_id)
    if direction == "up" and index > 0:
        ids[index - 1], ids[index] = ids[index], ids[index - 1]
    elif direction == "down" and index < len(ids) - 1:
        ids[index], ids[index + 1] = ids[index + 1], ids[index]
    try:
        await store.reorder(principal.user_id, ids)
    except UnknownCredentialError:
        # A credential vanished between the list and the write.
        raise _not_found() from None
    row = next(r for r in rows if r["id"] == credential_id)
    await _audit(
        request, "byok.credential.moved",
        actor_user_id=principal.user_id,
        resource_type="user_provider_credential",
        resource_id=str(credential_id),
        meta={**_audit_meta(row), "direction": direction},
    )
    if request.headers.get("HX-Request") == "true":
        # Wholesale tbody swap: every row re-renders in its new order, so
        # the ▲/▼ edge-disabled states stay correct too.
        rows = await store.list_for_user(principal.user_id)
        for r in rows:
            r["color"] = source_color(r.get("catalog_key") or r["provider_name"])
        return templates.TemplateResponse(
            request, "_provider_rows.html", {"rows": rows})
    return RedirectResponse("/dashboard/providers#routing", status_code=303)


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


# ---------------------------------------------------------------------------
# Per-user routing mode (Phase 1 self-service): auto / chain / pinned over
# the user's OWN connected credentials, referencing credential ids.


def _routing_model(value) -> str:
    model = str(value or "").strip()
    if not model:
        raise _bad_request("every routing step needs a model")
    if len(model) > 200:
        raise _bad_request("Routing step models must be at most 200 characters.")
    return model


def _routing_credential_id(value, owned: set[int]) -> int:
    try:
        credential_id = int(value)
    except (TypeError, ValueError):
        raise _bad_request("Routing step credential ids must be numbers") from None
    if credential_id not in owned:
        # Not a distinguishing message: foreign and unknown ids render the
        # same as "you disconnected that provider" (anti-enumeration).
        raise _bad_request(
            "Routing step references a provider that is not connected")
    return credential_id


def _chain_steps(body: dict, owned: set[int]) -> list[dict]:
    """Chain steps from a JSON body (``chain`` list) or the routing form
    (``chain_{i}_credential_id`` / ``chain_{i}_model`` pairs, rendered in
    the user's sort order)."""
    raw = body.get("chain")
    if isinstance(raw, list):
        steps = [
            {"credential_id": _routing_credential_id(s.get("credential_id"), owned),
             "model": _routing_model(s.get("model"))}
            for s in raw if isinstance(s, dict)
        ]
    else:
        steps = []
        index = 0
        while f"chain_{index}_credential_id" in body:
            steps.append({
                "credential_id": _routing_credential_id(
                    body[f"chain_{index}_credential_id"], owned),
                "model": _routing_model(body.get(f"chain_{index}_model")),
            })
            index += 1
    if not steps:
        raise _bad_request("A chain needs at least one step")
    return steps


def _pinned_step(body: dict, owned: set[int]) -> dict:
    if isinstance(body.get("pinned"), dict):
        pinned = body["pinned"]
        return {
            "credential_id": _routing_credential_id(
                pinned.get("credential_id"), owned),
            "model": _routing_model(pinned.get("model")),
        }
    if not body.get("pinned_credential_id"):
        raise _bad_request("Pinned routing needs a provider and a model")
    return {
        "credential_id": _routing_credential_id(
            body.get("pinned_credential_id"), owned),
        "model": _routing_model(body.get("pinned_model")),
    }


@router.get("/routing/mine")
async def get_routing(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    stored = await UserSettingsStore(
        request.app.state.engine).routing_for(principal.user_id)
    return {"routing": stored}


@router.post("/routing/mine")
async def save_routing(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    body = await _payload(request)
    mode = str(body.get("mode") or "").strip()
    if mode not in ("auto", "pinned", "chain"):
        raise _bad_request("mode must be one of auto, pinned, chain")
    owned = {
        r["id"] for r in await _store(request).list_for_user(principal.user_id)
    }
    if mode == "auto":
        routing = {"mode": "auto"}
    elif mode == "pinned":
        routing = {"mode": "pinned", "pinned": _pinned_step(body, owned)}
    else:
        routing = {"mode": "chain", "chain": _chain_steps(body, owned)}
    await UserSettingsStore(request.app.state.engine).save_routing(
        principal.user_id, routing)
    await _audit(
        request, "routing.updated",
        actor_user_id=principal.user_id,
        resource_type="user_settings",
        resource_id=str(principal.user_id),
        meta={"mode": mode},
    )
    if _wants_html(request):
        return RedirectResponse(
            "/dashboard/providers?routing_saved=1", status_code=303)
    return {"routing": routing}
