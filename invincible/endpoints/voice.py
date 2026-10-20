# invincible/endpoints/voice.py
"""B3 in-app voice: PTT transcription + spoken-Bmax synthesis + ephemeral tokens.

Realm discipline (per AGENTS.md §6 — realms never mix):

- ``/v1/voice/token``, ``/v1/audio/transcriptions``,
  ``/v1/audio/speech`` authenticate with a per-user ``inv_`` API key
  (``endpoints/auth.py::require_auth``) — the same realm as every other
  ``/v1/*`` surface. The two audio routes ALSO accept a live ephemeral
  voice token (``vv1.…``) minted by ``/v1/voice/token`` for the matching
  scope, so a voice session can run on a minutes-lived credential
  instead of the long-lived key.
- ``/dashboard/voice/*`` authenticate with the account session cookie
  (``require_user_session``) — the same realm as webchat. ``inv_`` keys
  never authorize them.

All synthesis/transcription runs server-side against the caller's OWN
BYOK credentials (``byok_attempt_source``); audio is never shipped to
any third party except that user's connected provider, and provider
keys never enter a response. Transcribed text is returned to the client,
which feeds it into the normal chat pipeline (``/v1/chat/completions``
or ``/dashboard/chat/stream``) — so history, memory, continuity, and
persistence behave exactly like typed text. Synthesized audio renders
ALREADY-PERSISTED assistant text; the text bubble stays the source of
truth.

Zero tool-registry surface (no new tools — goldens/drift untouched).
Voice off (``INVINCIBLE_VOICE`` kill-switch or the per-user ``voice``
override) refuses fail-closed with 503; text chat is unchanged.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from invincible.core.agent_registry import VOICE_SIGNAL_STATES
from invincible.core.identity import ensure_default_project
from invincible.core.principal import Principal
from invincible.core.voice import (
    MAX_DEVICE_CHARS,
    MAX_TTS_CHARS,
    VOICE_SCOPES,
    VoiceError,
    mint_voice_token,
    synthesize_audio,
    transcribe_audio,
    verify_voice_token,
    voice_allowed,
)
from invincible.endpoints.accounts import _audit, _payload, require_user_session
from invincible.endpoints.auth import extract_token, require_auth
from invincible.endpoints.byok import byok_attempt_source

logger = logging.getLogger("invincible.voice")

router = APIRouter()

_VOICE_OFF_BODY = {"error": {"message": "Voice is disabled on this server.",
                             "type": "voice_disabled"}}


def _voice_error_response(exc: VoiceError) -> JSONResponse:
    return JSONResponse(content=exc.body, status_code=exc.status_code)


def _voice_client(request: Request):
    """Injectable upstream client for tests (MockTransport).

    Prefers an explicit ``voice_http_client``, falls back to the BYOK
    probe client tests already inject, else None (fresh client per call).
    """
    return (getattr(request.app.state, "voice_http_client", None)
            or getattr(request.app.state, "byok_http_client", None))


async def _voice_principal(request: Request, scope: str) -> Principal | None:
    """Resolve a ``vv1.…`` bearer for ``scope`` to a BYOK-capable principal.

    None for anything else (missing, ``inv_``-shaped, forged, expired,
    wrong scope, or owner-secret unset — all fail closed). The resolved
    principal reuses ``kind="api_key"`` so ``byok_attempt_source`` maps
    it onto the token owner's own credentials; the token itself carries
    no provider key.
    """
    token = extract_token(request)
    if not token or not token.startswith("vv1."):
        return None
    resolved = verify_voice_token(token)
    if resolved is None or resolved["scope"] != scope:
        return None
    project_id = await ensure_default_project(
        request.app.state.engine, resolved["user_id"])
    return Principal(user_id=resolved["user_id"], project_id=project_id,
                     kind="api_key", api_key_id=None)


async def _require_v1_voice(request: Request, scope: str) -> Principal:
    """``inv_`` key OR matching-scope voice token (401 otherwise)."""
    token = extract_token(request)
    if token and not token.startswith("vv1."):
        return await require_auth(request)
    principal = await _voice_principal(request, scope)
    if principal is None:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Missing authentication token",
                              "type": "auth_error"}},
        )
    return principal


async def _overrides(request: Request, principal: Principal) -> dict:
    byok = await byok_attempt_source(request, principal)
    return {} if byok is None else byok[3]


async def _refuse_when_voice_off(request: Request,
                                 principal: Principal) -> JSONResponse | None:
    if not voice_allowed(await _overrides(request, principal)):
        return JSONResponse(content=_VOICE_OFF_BODY, status_code=503)
    return None


@router.post("/v1/voice/token")
async def mint_v1_voice_token(
    request: Request,
    principal: Principal = Depends(require_auth),
):
    """Mint one short-lived scope-bound token (``inv_`` realm, owner-bound).

    Body: ``{"device": "<phone label>", "scope": "stt"|"tts"}``. The
    token authenticates ``(user, device, scope, expiry)`` only — it
    embeds no provider key and authorizes only the matching audio route.
    TTL is minutes (``VOICE_TOKEN_TTL_SECONDS``). 503 when voice is off
    or no owner secret is configured (fail closed).
    """
    try:
        body = await _payload(request)
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    scope = str(body.get("scope") or "").strip()
    device = str(body.get("device") or "web").strip()
    if scope not in VOICE_SCOPES:
        return JSONResponse(
            {"error": {"message": "scope must be one of: stt, tts.",
                       "type": "invalid_request_error"}}, status_code=400)
    if len(device) > MAX_DEVICE_CHARS:
        return JSONResponse(
            {"error": {"message": "device label is too long.",
                       "type": "invalid_request_error"}}, status_code=400)
    refused = await _refuse_when_voice_off(request, principal)
    if refused is not None:
        return refused
    try:
        token = mint_voice_token(principal.user_id, device, scope)
    except VoiceError as e:
        return _voice_error_response(e)
    await _audit(request, "voice.token.minted",
                 actor_user_id=principal.user_id,
                 resource_type="voice_token",
                 meta={"scope": scope})
    return {"token": token, "scope": scope, "expires_in": 300,
            "token_type": "voice"}


@router.post("/v1/audio/transcriptions")
async def v1_transcribe(request: Request):
    """Transcribe PTT audio (``inv_`` key or ``stt`` voice token).

    Raw audio bytes in the request body (a recorded utterance, e.g.
    ``Content-Type: audio/webm``; optional ``?filename=`` overrides the
    upstream filename). Returns ``{"text": "..."}`` — the client sends
    that text to ``POST /v1/chat/completions`` like typed input, so the
    whole chat pipeline (history, memory, continuity, persistence)
    applies unchanged. Audio is transcribed ONLY by the caller's own
    connected provider; nothing is retained server-side.
    """
    principal = await _require_v1_voice(request, "stt")
    refused = await _refuse_when_voice_off(request, principal)
    if refused is not None:
        return refused
    try:
        audio = await request.body()
    except Exception:
        audio = b""
    if not audio:
        return JSONResponse(
            {"error": {"message": "Audio body is required.",
                       "type": "invalid_request_error"}}, status_code=400)
    byok = await byok_attempt_source(request, principal)
    if byok is None or not byok[0]:
        return JSONResponse(
            {"error": {"message": "No connected provider credential can "
                                 "transcribe audio.",
                       "type": "invalid_request_error"}}, status_code=400)
    filename = str(request.query_params.get("filename", "") or
                   "audio.webm").strip()[:128] or "audio.webm"
    try:
        text = await transcribe_audio(byok[0], byok[1], bytes(audio),
                                      filename=filename,
                                      client=_voice_client(request))
    except VoiceError as e:
        return _voice_error_response(e)
    return {"text": text}


@router.post("/v1/audio/speech")
async def v1_speak(request: Request):
    """Synthesize persisted assistant text (``inv_`` key or ``tts`` token).

    Body: ``{"input": "<assistant text>", "voice": "baymax"}``. Streams
    the provider's audio bytes back; the text bubble the client already
    holds stays the source of truth (nothing new is persisted here).
    502 when the caller's provider has no speech surface (fail-closed:
    the client keeps the text bubble).
    """
    principal = await _require_v1_voice(request, "tts")
    refused = await _refuse_when_voice_off(request, principal)
    if refused is not None:
        return refused
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse(
            {"error": {"message": "Request body must be JSON.",
                       "type": "invalid_request_error"}}, status_code=400)
    text = str(body.get("input") or "").strip()
    if not text:
        return JSONResponse(
            {"error": {"message": "input text is required.",
                       "type": "invalid_request_error"}}, status_code=400)
    if len(text) > MAX_TTS_CHARS:
        return JSONResponse(
            {"error": {"message": f"input must be at most {MAX_TTS_CHARS} "
                                  "characters.",
                       "type": "invalid_request_error"}}, status_code=400)
    voice = str(body.get("voice") or "baymax").strip() or "baymax"
    model = body.get("model")
    model = str(model).strip() or None if model is not None else None
    byok = await byok_attempt_source(request, principal)
    if byok is None or not byok[0]:
        return JSONResponse(
            {"error": {"message": "No connected provider credential can "
                                 "synthesize speech.",
                       "type": "invalid_request_error"}}, status_code=400)
    try:
        audio, content_type = await synthesize_audio(
            byok[0], byok[1], text, voice=voice, model=model,
            client=_voice_client(request))
    except VoiceError as e:
        return _voice_error_response(e)
    return Response(content=audio, media_type=content_type)


@router.post("/dashboard/voice/token")
async def mint_dashboard_voice_token(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    """Mint one short-lived scope-bound token (session-cookie realm)."""
    try:
        body = await _payload(request)
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    scope = str(body.get("scope") or "").strip()
    device = str(body.get("device") or "web").strip()
    if scope not in VOICE_SCOPES:
        return JSONResponse(
            {"error": {"message": "scope must be one of: stt, tts.",
                       "type": "invalid_request_error"}}, status_code=400)
    if len(device) > MAX_DEVICE_CHARS:
        return JSONResponse(
            {"error": {"message": "device label is too long.",
                       "type": "invalid_request_error"}}, status_code=400)
    refused = await _refuse_when_voice_off(request, principal)
    if refused is not None:
        return refused
    try:
        token = mint_voice_token(principal.user_id, device, scope)
    except VoiceError as e:
        return _voice_error_response(e)
    await _audit(request, "voice.token.minted",
                 actor_user_id=principal.user_id,
                 resource_type="voice_token",
                 meta={"scope": scope})
    return {"token": token, "scope": scope, "expires_in": 300,
            "token_type": "voice"}


@router.post("/dashboard/voice/transcribe")
async def dashboard_transcribe(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    """Transcribe PTT audio (session-cookie realm, webchat composer).

    Raw audio bytes in the request body (see ``/v1/audio/transcriptions``
    for the contract); the returned text is sent to
    ``POST /dashboard/chat/stream`` like typed input.
    """
    refused = await _refuse_when_voice_off(request, principal)
    if refused is not None:
        return refused
    try:
        audio = await request.body()
    except Exception:
        audio = b""
    if not audio:
        return JSONResponse(
            {"error": {"message": "Audio body is required.",
                       "type": "invalid_request_error"}}, status_code=400)
    byok = await byok_attempt_source(request, principal)
    if byok is None or not byok[0]:
        return JSONResponse(
            {"error": {"message": "No connected provider credential can "
                                 "transcribe audio.",
                       "type": "invalid_request_error"}}, status_code=400)
    filename = str(request.query_params.get("filename", "") or
                   "audio.webm").strip()[:128] or "audio.webm"
    try:
        text = await transcribe_audio(byok[0], byok[1], bytes(audio),
                                      filename=filename,
                                      client=_voice_client(request))
    except VoiceError as e:
        return _voice_error_response(e)
    return {"text": text}


@router.post("/dashboard/voice/synthesize")
async def dashboard_synthesize(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    """Synthesize persisted webchat assistant text (session-cookie realm)."""
    refused = await _refuse_when_voice_off(request, principal)
    if refused is not None:
        return refused
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse(
            {"error": {"message": "Request body must be JSON.",
                       "type": "invalid_request_error"}}, status_code=400)
    text = str(body.get("input") or "").strip()
    if not text:
        return JSONResponse(
            {"error": {"message": "input text is required.",
                       "type": "invalid_request_error"}}, status_code=400)
    if len(text) > MAX_TTS_CHARS:
        return JSONResponse(
            {"error": {"message": f"input must be at most {MAX_TTS_CHARS} "
                                  "characters.",
                       "type": "invalid_request_error"}}, status_code=400)
    voice = str(body.get("voice") or "baymax").strip() or "baymax"
    model = body.get("model")
    model = str(model).strip() or None if model is not None else None
    byok = await byok_attempt_source(request, principal)
    if byok is None or not byok[0]:
        return JSONResponse(
            {"error": {"message": "No connected provider credential can "
                                 "synthesize speech.",
                       "type": "invalid_request_error"}}, status_code=400)
    try:
        audio, content_type = await synthesize_audio(
            byok[0], byok[1], text, voice=voice, model=model,
            client=_voice_client(request))
    except VoiceError as e:
        return _voice_error_response(e)
    return Response(content=audio, media_type=content_type)


@router.post("/dashboard/voice/state")
async def dashboard_voice_state(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    """Relay the browser's voice state to this user's paired desktop (F22).

    Display-only control message for the companion face: the webchat orb
    posts ``listening``/``speaking``/``idle`` as it changes, and the
    agent runner paints the matching face. Nothing is persisted, queued
    as work, or executed, and the text bubble stays the source of truth.
    WS-first with a latest-wins poll fallback; a missing agent simply
    answers ``relayed: False``. Voice-off stays fail-closed (503), so a
    muted server never animates a face.
    """
    refused = await _refuse_when_voice_off(request, principal)
    if refused is not None:
        return refused
    try:
        body = await _payload(request)
    except Exception:
        body = None
    state = str(body.get("state") or "").strip().lower() \
        if isinstance(body, dict) else ""
    if state not in VOICE_SIGNAL_STATES:
        return JSONResponse(
            {"error": {"message": "state must be one of: listening, "
                                  "speaking, idle.",
                       "type": "invalid_request_error"}}, status_code=400)
    registry = getattr(request.app.state, "agent_registry", None)
    relayed = False
    if registry is not None:
        envelope = registry.post_voice_state(principal.user_id, state)
        if envelope is not None:
            relayed = await registry.push_ws(
                principal.user_id,
                {"type": "voice_state", "state": state,
                 "voice_id": envelope["voice_id"]},
            )
            if relayed:
                # Drop only this copy: a newer signal may have replaced
                # the slot while the socket send was in flight.
                registry.drop_voice_state(
                    principal.user_id, envelope["voice_id"])
    return {"ok": True, "state": state, "relayed": bool(relayed)}
