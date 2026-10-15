# invincible/core/voice.py
"""B3 in-app voice: mic-in transcription + spoken-Bmax synthesis (BYOK-only).

All audio goes through the caller's OWN connected provider credentials —
the server never ships audio to any third party except the provider that
user connected, and real provider keys never leave the server (clients
hold at most a short-lived scope-bound voice token, minted in-realm).

Two realms share this module (``core/`` business logic — no FastAPI
imports, mirroring ``core/chat_service.py``):

- ``/v1/*`` (per-user ``inv_`` keys) and the dashboard webchat
  (session cookies) both resolve ``byok`` via
  ``endpoints.byok.byok_attempt_source`` and call :func:`transcribe_audio`
  / :func:`synthesize_audio` with the resulting
  ``(candidates, key_resolver, ...)`` pair.
- Ephemeral tokens (:func:`mint_voice_token` / :func:`verify_voice_token`)
  are stateless HMAC-signed values (same key discipline as
  ``core.accounts.SessionManager``: ``INVINCIBLE_OWNER_SECRET`` via
  ``settings.owner_secret()``, fail closed when unset). No new tables,
  no migration — a restart invalidates nothing because there is nothing
  stored, and expiry bounds the theft window to minutes.

Text is the source of truth: transcription output is fed as a normal
user turn through ``chat_service.prepare_chat`` by the caller, and
synthesis input is the assistant text the chat pipeline already
persisted. Voice off (``INVINCIBLE_VOICE`` kill-switch or the per-user
``voice`` override) refuses fail-closed with 503 while text chat is
untouched.
"""
import hashlib
import hmac
import logging
import re
import time

import httpx

from invincible.core.config import resolve_timeout
from invincible.core.settings import settings
from invincible.core.user_settings_store import override_flag

logger = logging.getLogger("invincible.voice")

# Theft window: minutes, not hours (B3 ticket pins this).
VOICE_TOKEN_TTL_SECONDS = 300
VOICE_SCOPES = ("stt", "tts")
# DoS bounds: uploads that fit a push-to-talk utterance, TTS input that
# fits a chat bubble. The provider's own limits apply inside these.
MAX_AUDIO_BYTES = 5 * 1024 * 1024
MAX_TTS_CHARS = 4000
MAX_VOICE_NAME_CHARS = 64
MAX_DEVICE_CHARS = 128
# Device binds a token to one phone/browser. Dots are excluded so the
# token splits unambiguously on "." (mint sanitizes anything else).
_DEVICE_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# B4: audio surfaces exist on OpenAI only. Chat credentials carry a
# chat model_id (e.g. gpt-4o-mini, gemini-2.5-flash) that must never be
# sent as the audio model, and the legacy "baymax" voice name is not a
# valid OpenAI voice. Pin the audio models and map the voice name here
# so every caller (v1 + dashboard, STT + TTS) shares one rule.
VOICE_STT_MODEL = "whisper-1"
VOICE_TTS_MODEL = "tts-1"
DEFAULT_TTS_VOICE = "alloy"
OPENAI_AUDIO_BASE = "https://api.openai.com/v1"


def _ordered_voice_candidates(candidates) -> list:
    """OpenAI-base credentials first, everything else after.

    The order preserves routing inside each group. Non-OpenAI bases
    (groq/gemini/openrouter/nim/custom) usually 404 on ``/audio/*`` —
    trying OpenAI first avoids a spurious passthrough 404 when the user
    has both connected. Custom OpenAI-compatible hosts that do serve
    audio still get tried as fallback (their 404 is skipped, not fatal).
    """
    openai_first: list = []
    rest: list = []
    for candidate in candidates or []:
        base = str((candidate or {}).get("base_url", "")).rstrip("/").lower()
        if base == OPENAI_AUDIO_BASE:
            openai_first.append(candidate)
        else:
            rest.append(candidate)
    return openai_first + rest


def _normalize_tts_voice(voice: object) -> str:
    """Map legacy/UI voice names onto real OpenAI voices."""
    name = str(voice or "").strip()[:MAX_VOICE_NAME_CHARS] or "baymax"
    if name.lower() == "baymax":
        return DEFAULT_TTS_VOICE
    return name


class VoiceError(Exception):
    """Already-mapped voice failure (status_code + OpenAI-style body)."""

    def __init__(self, status_code: int, message: str,
                 error_type: str = "voice_error"):
        self.status_code = status_code
        self.body = {"error": {"message": message, "type": error_type}}
        super().__init__(message)


class VoiceUpstreamError(VoiceError):
    """A 4xx the provider returned for our audio request (passthrough)."""


class VoiceGatewayError(VoiceError):
    """All voice attempts failed upstream (502/503 to the client)."""


def voice_allowed(overrides: dict | None) -> bool:
    """Kill-switch AND per-user preference in one predicate.

    The server default is the live-read ``settings.voice_enabled()``
    accessor (``INVINCIBLE_VOICE``); a stored per-user ``voice`` override
    wins for that user. Callers refuse fail-closed (503) when False —
    text chat never changes.
    """
    return override_flag(overrides, "voice", settings.voice_enabled)


def sanitize_device(raw: object) -> str:
    """Bind a token to one device string (phone/browser label).

    Anything outside ``[A-Za-z0-9_-]`` becomes ``_`` and the result is
    cut to :data:`MAX_DEVICE_CHARS`; blank input becomes ``"web"``.
    """
    text = str(raw or "").strip() or "web"
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", text)[:MAX_DEVICE_CHARS]
    return clean or "web"


def _voice_key() -> bytes | None:
    secret = settings.owner_secret()
    if not secret:
        return None
    return hashlib.sha256(secret.encode("utf-8")).digest()


def mint_voice_token(user_id: int, device: str, scope: str,
                     *, ttl_seconds: int = VOICE_TOKEN_TTL_SECONDS) -> str:
    """Mint one short-lived scope-bound token for ``user_id``.

    Raises :class:`VoiceError` (503) when voice is disabled server-wide
    or no owner secret is configured (fail closed), and 400 for a bad
    scope. The token never embeds any provider key — only
    ``(user, device, scope, expiry)`` plus the HMAC.
    """
    if not settings.voice_enabled():
        raise VoiceError(503, "Voice is disabled on this server.",
                         "voice_disabled")
    if scope not in VOICE_SCOPES:
        raise VoiceError(400, "scope must be one of: stt, tts.",
                         "invalid_request_error")
    key = _voice_key()
    if key is None:
        raise VoiceError(
            503, "Voice tokens are disabled: INVINCIBLE_OWNER_SECRET "
                 "is not configured.", "config_error")
    clean_device = sanitize_device(device)
    if not _DEVICE_RE.fullmatch(clean_device):
        raise VoiceError(400, "device label is invalid.",
                         "invalid_request_error")
    expiry = int(time.time()) + max(1, int(ttl_seconds))
    payload = f"vv1.{int(user_id)}.{scope}.{expiry}.{clean_device}"
    sig = hmac.new(key, payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_voice_token(token: str | None) -> dict | None:
    """``{"user_id", "scope", "device", "expiry"}`` for a live token.

    None for anything else: forged, expired, wrong scope, malformed, or
    no owner secret configured (fail closed). Timing-safe signature
    comparison; expiry compared against ``time.time()``.
    """
    key = _voice_key()
    if key is None or not token:
        return None
    parts = token.split(".")
    if len(parts) != 6 or parts[0] != "vv1":
        return None
    _, uid_raw, scope, expiry_raw, device, sig = parts
    if scope not in VOICE_SCOPES:
        return None
    if not _DEVICE_RE.fullmatch(device):
        return None
    payload = f"vv1.{uid_raw}.{scope}.{expiry_raw}.{device}"
    expected = hmac.new(key, payload.encode("ascii"),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        expiry = int(expiry_raw)
        user_id = int(uid_raw)
    except ValueError:
        return None
    if time.time() >= expiry:
        return None
    return {"user_id": user_id, "scope": scope, "device": device,
            "expiry": expiry}


def _http_client(owned_state_client):
    if owned_state_client is not None:
        return owned_state_client, False
    return httpx.AsyncClient(), True


def _upstream_excerpt(resp) -> str:
    """Short safe provider error excerpt for 4xx passthrough (no headers,
    no keys — response bodies only, truncated). Lets billing/quota 402s
    explain themselves instead of a bare HTTP number."""
    try:
        body = resp.json() if hasattr(resp, "json") else None
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])[:200].strip()
            if isinstance(err, str) and err.strip():
                return err.strip()[:200]
            for key in ("message", "detail"):
                if isinstance(body.get(key), str) and body[key].strip():
                    return str(body[key])[:200].strip()
    except Exception:
        pass
    try:
        text = resp.text if isinstance(getattr(resp, "text", ""), str) else ""
    except Exception:
        text = ""
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) > 200:
        text = text[:200].strip() + "…"
    return text


async def transcribe_audio(candidates, key_resolver, audio: bytes, *,
                           filename: str = "audio.webm",
                           model: str | None = None,
                           client=None) -> str:
    """Transcribe PTT audio with the caller's own BYOK credential.

    ``candidates``/``key_resolver`` are the first two items of the
    ``byok_attempt_source`` tuple (routing already applied by the
    caller ordering). Posts ``multipart/form-data`` to the first usable
    credential's ``/audio/transcriptions`` endpoint and returns the
    stripped transcript text. Raises :class:`VoiceError`: 400 for empty
    input (checked by the caller too — defense in depth), 502/503 when
    no credential works or the provider has no transcription surface
    (fail-closed text-only fallback: the client keeps the typed path).
    """
    if not audio:
        raise VoiceError(400, "Audio is required.", "invalid_request_error")
    if len(audio) > MAX_AUDIO_BYTES:
        raise VoiceError(400, f"Audio must be at most {MAX_AUDIO_BYTES} "
                              "bytes.", "invalid_request_error")
    http, owns = _http_client(client)
    try:
        tried = 0
        saw_404 = False
        for candidate in _ordered_voice_candidates(candidates):
            try:
                api_key = await key_resolver(candidate)
            except Exception:
                logger.warning("Voice key resolution failed; skipping")
                continue
            if not api_key:
                continue
            tried += 1
            base_url = str(candidate.get("base_url", "")).rstrip("/")
            url = f"{base_url}/audio/transcriptions"
            body_model = (str(model).strip() or VOICE_STT_MODEL
                          if model is not None else VOICE_STT_MODEL)
            try:
                resp = await http.post(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    files={"file": (filename, audio,
                                    "application/octet-stream")},
                    data={"model": body_model},
                    timeout=resolve_timeout({}),
                )
            except httpx.RequestError as e:
                logger.warning("Voice transcription transport error: %s", e)
                continue
            if resp.status_code == 200:
                try:
                    text = str((resp.json() or {}).get("text") or "").strip()
                except Exception:
                    text = ""
                if text:
                    return text
                raise VoiceError(400, "Transcription returned no text.",
                                 "invalid_request_error")
            if resp.status_code == 404:
                # No audio surface on this provider (groq/gemini/
                # openrouter/nim/custom): try the next credential instead
                # of aborting with "Transcription refused (HTTP 404)".
                saw_404 = True
                logger.warning("Voice transcription HTTP 404 on %s; "
                               "trying next", base_url)
                continue
            if 400 <= resp.status_code < 500:
                excerpt = _upstream_excerpt(resp)
                detail = (f"Transcription refused (HTTP {resp.status_code}"
                          f": {excerpt})." if excerpt else
                          f"Transcription refused (HTTP {resp.status_code}).")
                raise VoiceUpstreamError(resp.status_code, detail)
            logger.warning("Voice transcription HTTP %s; trying next",
                           resp.status_code)
        if not tried:
            if saw_404:
                raise VoiceGatewayError(
                    502, "This provider has no transcription surface; "
                         "connect OpenAI for mic (text chat is unchanged).",
                    "gateway_error")
            raise VoiceError(400, "No connected provider credential can "
                                  "transcribe audio.",
                             "invalid_request_error")
        if saw_404:
            raise VoiceGatewayError(
                502, "This provider has no transcription surface; "
                     "connect OpenAI for mic (text chat is unchanged).",
                "gateway_error")
        raise VoiceGatewayError(
            502, "Transcription failed upstream; type instead "
                 "(text chat is unchanged).", "gateway_error")
    finally:
        if owns:
            await http.aclose()


async def synthesize_audio(candidates, key_resolver, text: str, *,
                           voice: str = "baymax",
                           model: str | None = None,
                           client=None) -> tuple[bytes, str]:
    """Synthesize assistant text with the caller's own BYOK credential.

    Returns ``(audio_bytes, content_type)`` from the first usable
    credential's ``/audio/speech`` endpoint. The input is the ALREADY
    PERSISTED assistant text — voice is rendering, never the record.
    Raises :class:`VoiceError`: 400 for blank/overlong input, 404-voiced
    502 when the provider has no speech surface (fail-closed: the client
    keeps the text bubble), 503 when voice is off (checked by the
    caller too — defense in depth).
    """
    clean = str(text or "").strip()
    if not clean:
        raise VoiceError(400, "Text is required.", "invalid_request_error")
    if len(clean) > MAX_TTS_CHARS:
        raise VoiceError(400, f"Text must be at most {MAX_TTS_CHARS} "
                              "characters.", "invalid_request_error")
    voice_name = _normalize_tts_voice(voice)
    http, owns = _http_client(client)
    try:
        tried = 0
        saw_404 = False
        for candidate in _ordered_voice_candidates(candidates):
            try:
                api_key = await key_resolver(candidate)
            except Exception:
                logger.warning("Voice key resolution failed; skipping")
                continue
            if not api_key:
                continue
            tried += 1
            base_url = str(candidate.get("base_url", "")).rstrip("/")
            url = f"{base_url}/audio/speech"
            body_model = (str(model).strip() or VOICE_TTS_MODEL
                          if model is not None else VOICE_TTS_MODEL)
            try:
                resp = await http.post(
                    url,
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                    json={"model": body_model, "input": clean,
                          "voice": voice_name},
                    timeout=resolve_timeout({}),
                )
            except httpx.RequestError as e:
                logger.warning("Voice synthesis transport error: %s", e)
                continue
            if resp.status_code == 200 and resp.content:
                ctype = (resp.headers.get("content-type") or "audio/mpeg"
                         ).split(";")[0].strip() or "audio/mpeg"
                # Provider keys must never ride home in audio metadata:
                # only the bytes + a generic content type leave the server.
                return bytes(resp.content), ctype
            if resp.status_code == 404:
                # No speech surface here: try the next credential.
                saw_404 = True
                logger.warning("Voice synthesis HTTP 404 on %s; "
                               "trying next", base_url)
                continue
            if 400 <= resp.status_code < 500:
                excerpt = _upstream_excerpt(resp)
                detail = (f"Speech refused (HTTP {resp.status_code}"
                          f": {excerpt})." if excerpt else
                          f"Speech refused (HTTP {resp.status_code}).")
                raise VoiceUpstreamError(resp.status_code, detail)
            logger.warning("Voice synthesis HTTP %s; trying next",
                           resp.status_code)
        if not tried:
            if saw_404:
                raise VoiceGatewayError(
                    502, "This provider has no speech surface; text chat "
                         "is unchanged.", "gateway_error")
            raise VoiceError(400, "No connected provider credential can "
                                  "synthesize speech.",
                             "invalid_request_error")
        if saw_404:
            raise VoiceGatewayError(
                502, "This provider has no speech surface; text chat "
                     "is unchanged.", "gateway_error")
        raise VoiceGatewayError(
            502, "Speech failed upstream; the text reply stands.",
            "gateway_error")
    finally:
        if owns:
            await http.aclose()
