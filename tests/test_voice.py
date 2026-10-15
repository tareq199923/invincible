# tests/test_voice.py
"""B3 voice: mic-in PTT transcription + spoken-Bmax synthesis + ephemeral tokens.

Hermetic only: fake audio bytes, MockTransport provider audio surfaces
(no real mic, network, or providers). Real BYOK keys never leave the
server — every voice response is scanned for key material.
"""
import hashlib
import hmac
import time

import httpx
import pytest

from invincible.core.credential_store import ByokCredentialStore
from invincible.core.identity import ApiKeyStore
from invincible.core.voice import (
    VOICE_TOKEN_TTL_SECONDS,
    sanitize_device,
    verify_voice_token,
    voice_allowed,
)
from invincible.main import app
from tests.conftest import (
    TEST_OWNER_SECRET,
    default_providers,
    provider_body,
    register_account,
    v1_user,
)


def _auth(raw_key):
    return {"Authorization": f"Bearer {raw_key}"}


def _audio_transport(*, transcript="hello bmax", speech_bytes=b"FAKEAUDIO",
                     transcribe_status=200, speech_status=200):
    """MockTransport answering the OpenAI audio surfaces on mock hosts."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/audio/transcriptions"):
            if transcribe_status != 200:
                return httpx.Response(transcribe_status,
                                      json={"error": "nope"})
            return httpx.Response(200, json={"text": transcript})
        if path.endswith("/audio/speech"):
            if speech_status != 200:
                return httpx.Response(speech_status,
                                      json={"error": "nope"})
            return httpx.Response(200, content=speech_bytes,
                                  headers={"content-type": "audio/mpeg"})
        return httpx.Response(500, json={"error": "unexpected path"})

    return httpx.MockTransport(handler)


@pytest.fixture
async def voice_client():
    """Install a MockTransport voice upstream client; always clean up."""
    clients = []

    def _set(**kwargs):
        client = httpx.AsyncClient(transport=_audio_transport(**kwargs))
        app.state.voice_http_client = client
        clients.append(client)
        return client

    yield _set
    for client in clients:
        await client.aclose()
    app.state.voice_http_client = None


def _craft_token(user_id, scope, expiry, device="web"):
    key = hashlib.sha256(TEST_OWNER_SECRET.encode("utf-8")).digest()
    payload = f"vv1.{user_id}.{scope}.{expiry}.{device}"
    sig = hmac.new(key, payload.encode("ascii"),
                   hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


async def _mint(client, raw_key, scope="stt", device="phone-1"):
    return await client.post("/v1/voice/token",
                             json={"device": device, "scope": scope},
                             headers=_auth(raw_key))


# --- token mint ------------------------------------------------------------


async def test_mint_requires_auth(client, byok_env):
    resp = await client.post("/v1/voice/token",
                             json={"device": "d", "scope": "stt"})
    assert resp.status_code == 401


async def test_mint_rejects_bad_scope(client, byok_env):
    _, raw = await v1_user(client, "mint-scope@example.com")
    resp = await client.post("/v1/voice/token",
                             json={"device": "d", "scope": "realtime"},
                             headers=_auth(raw))
    assert resp.status_code == 400


async def test_mint_ok_shape_and_verify(client, byok_env):
    uid, raw = await v1_user(client, "mint-ok@example.com")
    resp = await _mint(client, raw)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope"] == "stt"
    assert body["expires_in"] == VOICE_TOKEN_TTL_SECONDS
    assert body["token"].startswith("vv1.")
    resolved = verify_voice_token(body["token"])
    assert resolved is not None
    assert resolved["user_id"] == uid
    assert resolved["scope"] == "stt"
    assert resolved["device"] == "phone-1"
    # Real provider keys never ride home in the mint response.
    assert "test-key-" not in resp.text


async def test_mint_device_too_long(client, byok_env):
    _, raw = await v1_user(client, "mint-dev@example.com")
    resp = await client.post("/v1/voice/token",
                             json={"device": "x" * 200, "scope": "stt"},
                             headers=_auth(raw))
    assert resp.status_code == 400


async def test_mint_refused_when_voice_killed(client, byok_env, monkeypatch):
    _, raw = await v1_user(client, "mint-kill@example.com")
    monkeypatch.setenv("INVINCIBLE_VOICE", "0")
    resp = await _mint(client, raw)
    assert resp.status_code == 503


async def test_mint_refused_without_owner_secret(client, byok_env,
                                                 monkeypatch):
    _, raw = await v1_user(client, "mint-nosec@example.com")
    monkeypatch.delenv("INVINCIBLE_OWNER_SECRET", raising=False)
    resp = await _mint(client, raw)
    assert resp.status_code == 503


async def test_mint_refused_when_user_muted(client, byok_env):
    from invincible.core.user_settings_store import UserSettingsStore

    uid, raw = await v1_user(client, "mint-muted@example.com")
    await UserSettingsStore(app.state.engine).save_overrides(
        uid, {"voice": False})
    resp = await _mint(client, raw)
    assert resp.status_code == 503


async def test_mint_binds_caller_not_victim(client, byok_env):
    """Cross-user mint is impossible: the token always names the caller."""
    uid_a, _ = await v1_user(client, "mint-a@example.com")
    uid_b, raw_b = await v1_user(client, "mint-b@example.com")
    assert uid_a != uid_b
    resp = await _mint(client, raw_b)
    assert resp.status_code == 200
    resolved = verify_voice_token(resp.json()["token"])
    assert resolved["user_id"] == uid_b


# --- token verification ----------------------------------------------------


def test_verify_rejects_malformed():
    assert verify_voice_token(None) is None
    assert verify_voice_token("") is None
    assert verify_voice_token("vv1.1.stt") is None
    assert verify_voice_token("vv1.1.realtime.9999999999.web.abc") is None


def test_verify_rejects_expired():
    token = _craft_token(7, "stt", int(time.time()) - 10)
    assert verify_voice_token(token) is None


def test_verify_rejects_tampered_user():
    live = _craft_token(7, "stt", int(time.time()) + 300)
    parts = live.split(".")
    parts[1] = "8"  # swap the user id, keep the signature
    assert verify_voice_token(".".join(parts)) is None


def test_sanitize_device_bounds():
    assert sanitize_device("") == "web"
    assert sanitize_device("Pixel 8 pro!") == "Pixel_8_pro_"
    assert len(sanitize_device("x" * 500)) <= 128


def test_voice_allowed_override_wins(monkeypatch):
    monkeypatch.setenv("INVINCIBLE_VOICE", "0")
    assert voice_allowed({"voice": True}) is True
    monkeypatch.setenv("INVINCIBLE_VOICE", "1")
    assert voice_allowed({"voice": False}) is False


# --- transcription (v1 realm) ----------------------------------------------


async def test_transcribe_happy_path(client, byok_env, voice_client):
    voice_client()
    _, raw = await v1_user(client, "stt-happy@example.com")
    resp = await client.post(
        "/v1/audio/transcriptions",
        content=b"fake-audio-bytes",
        headers={**_auth(raw), "Content-Type": "audio/webm"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"text": "hello bmax"}
    assert "test-key-" not in resp.text


async def test_transcribe_via_voice_token_only(client, byok_env,
                                               voice_client):
    """A minutes-lived stt token authenticates without the inv_ key."""
    voice_client()
    _, raw = await v1_user(client, "stt-token@example.com")
    token = (await _mint(client, raw, scope="stt")).json()["token"]
    resp = await client.post(
        "/v1/audio/transcriptions",
        content=b"fake-audio-bytes",
        headers={**_auth(token), "Content-Type": "audio/webm"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"text": "hello bmax"}


async def test_transcribe_rejects_wrong_scope_token(client, byok_env,
                                                    voice_client):
    voice_client()
    _, raw = await v1_user(client, "stt-scope@example.com")
    token = (await _mint(client, raw, scope="tts")).json()["token"]
    resp = await client.post(
        "/v1/audio/transcriptions",
        content=b"fake-audio-bytes",
        headers={**_auth(token), "Content-Type": "audio/webm"})
    assert resp.status_code == 401


async def test_transcribe_rejects_tampered_token(client, byok_env,
                                                 voice_client):
    voice_client()
    uid, raw = await v1_user(client, "stt-tamper@example.com")
    token = (await _mint(client, raw, scope="stt")).json()["token"]
    parts = token.split(".")
    parts[1] = str(uid + 9999)
    resp = await client.post(
        "/v1/audio/transcriptions",
        content=b"x",
        headers={**_auth(".".join(parts)), "Content-Type": "audio/webm"})
    assert resp.status_code == 401


async def test_transcribe_rejects_expired_token(client, byok_env,
                                                voice_client):
    voice_client()
    uid, _ = await v1_user(client, "stt-exp@example.com")
    token = _craft_token(uid, "stt", int(time.time()) - 5)
    resp = await client.post(
        "/v1/audio/transcriptions",
        content=b"x",
        headers={**_auth(token), "Content-Type": "audio/webm"})
    assert resp.status_code == 401


async def test_transcribe_missing_and_empty(client, byok_env, voice_client):
    voice_client()
    _, raw = await v1_user(client, "stt-bad@example.com")
    resp = await client.post("/v1/audio/transcriptions", headers=_auth(raw))
    assert resp.status_code == 400
    resp = await client.post(
        "/v1/audio/transcriptions",
        content=b"",
        headers={**_auth(raw), "Content-Type": "audio/webm"})
    assert resp.status_code == 400


async def test_transcribe_no_credentials(client, byok_env, voice_client):
    voice_client()
    _, raw = await v1_user(client, "stt-nocred@example.com", providers=[])
    resp = await client.post(
        "/v1/audio/transcriptions",
        content=b"x",
        headers={**_auth(raw), "Content-Type": "audio/webm"})
    assert resp.status_code == 400


async def test_transcribe_voice_killed(client, byok_env, voice_client,
                                       monkeypatch):
    voice_client()
    _, raw = await v1_user(client, "stt-killed@example.com")
    monkeypatch.setenv("INVINCIBLE_VOICE", "0")
    resp = await client.post(
        "/v1/audio/transcriptions",
        content=b"x",
        headers={**_auth(raw), "Content-Type": "audio/webm"})
    assert resp.status_code == 503


async def test_transcribe_upstream_failure_is_502(client, byok_env,
                                                  voice_client):
    voice_client(transcribe_status=500)
    _, raw = await v1_user(client, "stt-502@example.com")
    resp = await client.post(
        "/v1/audio/transcriptions",
        content=b"x",
        headers={**_auth(raw), "Content-Type": "audio/webm"})
    assert resp.status_code == 502


async def test_transcribe_rejects_oversize(client, byok_env, voice_client):
    voice_client()
    _, raw = await v1_user(client, "stt-big@example.com")
    resp = await client.post(
        "/v1/audio/transcriptions",
        content=b"x" * (6 * 1024 * 1024),
        headers={**_auth(raw), "Content-Type": "audio/webm"})
    assert resp.status_code == 400


# --- synthesis (v1 realm) ----------------------------------------------------


async def test_speak_happy_path(client, byok_env, voice_client):
    voice_client()
    _, raw = await v1_user(client, "tts-happy@example.com")
    resp = await client.post("/v1/audio/speech",
                             json={"input": "Hello there.", "voice": "baymax"},
                             headers=_auth(raw))
    assert resp.status_code == 200, resp.text
    assert resp.content == b"FAKEAUDIO"
    assert resp.headers["content-type"].startswith("audio/")
    assert b"test-key-" not in resp.content


async def test_speak_via_voice_token_only(client, byok_env, voice_client):
    voice_client()
    _, raw = await v1_user(client, "tts-token@example.com")
    token = (await _mint(client, raw, scope="tts")).json()["token"]
    resp = await client.post("/v1/audio/speech",
                             json={"input": "Hello there."},
                             headers=_auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.content == b"FAKEAUDIO"


async def test_speak_rejects_stt_token(client, byok_env, voice_client):
    voice_client()
    _, raw = await v1_user(client, "tts-scope@example.com")
    token = (await _mint(client, raw, scope="stt")).json()["token"]
    resp = await client.post("/v1/audio/speech",
                             json={"input": "Hello there."},
                             headers=_auth(token))
    assert resp.status_code == 401


async def test_speak_bad_input(client, byok_env, voice_client):
    voice_client()
    _, raw = await v1_user(client, "tts-bad@example.com")
    resp = await client.post("/v1/audio/speech", json={"input": "  "},
                             headers=_auth(raw))
    assert resp.status_code == 400
    resp = await client.post("/v1/audio/speech",
                             json={"input": "x" * 5000},
                             headers=_auth(raw))
    assert resp.status_code == 400
    resp = await client.post("/v1/audio/speech", json={"nope": 1},
                             headers=_auth(raw))
    assert resp.status_code == 400


async def test_speak_no_speech_surface_is_502(client, byok_env,
                                              voice_client):
    """Fail-closed text-only fallback: 502, the text bubble stands."""
    voice_client(speech_status=404)
    _, raw = await v1_user(client, "tts-404@example.com")
    resp = await client.post("/v1/audio/speech",
                             json={"input": "Hello there."},
                             headers=_auth(raw))
    assert resp.status_code == 502


async def test_speak_voice_killed(client, byok_env, voice_client,
                                  monkeypatch):
    voice_client()
    _, raw = await v1_user(client, "tts-killed@example.com")
    monkeypatch.setenv("INVINCIBLE_VOICE", "0")
    resp = await client.post("/v1/audio/speech",
                             json={"input": "Hello there."},
                             headers=_auth(raw))
    assert resp.status_code == 503


# --- dashboard realm ---------------------------------------------------------


async def test_dashboard_voice_token_and_audio(client, byok_env,
                                               voice_client):
    """Session-cookie realm: token mint + transcribe + synthesize."""
    voice_client()
    await register_account(client, "dash-voice@example.com")
    store = ByokCredentialStore(app.state.engine)
    login = await client.post(
        "/auth/login",
        json={"email": "dash-voice@example.com", "password": "longenough1"})
    uid = login.json()["id"]
    for provider in default_providers():
        await store.create(
            user_id=uid, provider_name=provider["name"],
            model_id=provider["model_id"], base_url=provider["base_url"],
            api_key=f"test-key-{provider['name']}")
    mint = await client.post("/dashboard/voice/token",
                             json={"device": "laptop", "scope": "tts"})
    assert mint.status_code == 200, mint.text
    assert "test-key-" not in mint.text
    heard = await client.post(
        "/dashboard/voice/transcribe",
        content=b"fake-audio-bytes",
        headers={"Content-Type": "audio/webm"})
    assert heard.status_code == 200, heard.text
    assert heard.json() == {"text": "hello bmax"}
    spoken = await client.post("/dashboard/voice/synthesize",
                               json={"input": "Hi."})
    assert spoken.status_code == 200, spoken.text
    assert spoken.content == b"FAKEAUDIO"


async def test_dashboard_voice_rejects_apikey_realm(client, byok_env,
                                                    voice_client):
    """Realms never mix: inv_ keys authorize nothing on /dashboard/voice/*."""
    voice_client()
    await register_account(client, "dash-realm@example.com")
    login = await client.post(
        "/auth/login",
        json={"email": "dash-realm@example.com", "password": "longenough1"})
    uid = login.json()["id"]
    raw = (await ApiKeyStore(app.state.engine).create(uid,
                                                      label="t"))["raw"]
    # Drop the session cookie: only the inv_ key remains.
    client.cookies.clear()
    resp = await client.post("/dashboard/voice/token",
                             json={"device": "d", "scope": "stt"},
                             headers=_auth(raw))
    assert resp.status_code == 401


async def test_dashboard_ignores_voice_bearer(client, byok_env,
                                              voice_client):
    """A victim's voice token buys nothing in another user's session."""
    voice_client()
    _, raw_victim = await v1_user(client, "victim@example.com")
    victim_token = (await _mint(client, raw_victim,
                                scope="stt")).json()["token"]
    await register_account(client, "other@example.com")
    # Attacker holds a valid session as themselves (zero credentials) but
    # presents the victim's voice token: the cookie realm wins, so the
    # request runs as the attacker and fails on THEIR credentials.
    resp = await client.post(
        "/dashboard/voice/transcribe",
        content=b"x",
        headers={**_auth(victim_token), "Content-Type": "audio/webm"})
    assert resp.status_code == 400
    assert "No connected provider" in resp.text


# --- B4: audio failover + model pinning ---------------------------------------


def _candidates(*bases):
    return [
        {"name": f"p{i}", "base_url": base, "model_id": "chat-model-x"}
        for i, base in enumerate(bases)
    ]


async def _resolver(candidate):
    return "k"


def _client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_transcribe_skips_404_then_succeeds():
    from invincible.core.voice import transcribe_audio

    seen_models: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "gemini" in str(request.url):
            return httpx.Response(404, json={"error": "nope"})
        seen_models.append(request.content)
        return httpx.Response(200, json={"text": "hi from openai"})

    client = _client_for(handler)
    try:
        text = await transcribe_audio(
            _candidates("https://generativelanguage.googleapis.com/v1beta/openai",
                        "https://api.openai.com/v1"),
            _resolver, b"audio-bytes", client=client)
    finally:
        await client.aclose()
    assert text == "hi from openai"
    # Audio model pinned, never the chat model_id.
    assert seen_models and b"whisper-1" in seen_models[0]
    assert b"chat-model-x" not in seen_models[0]


async def test_transcribe_groq_uses_free_turbo_model():
    from invincible.core.voice import transcribe_audio

    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return httpx.Response(200, json={"text": "hi from groq"})

    client = _client_for(handler)
    try:
        text = await transcribe_audio(
            _candidates("https://api.groq.com/openai/v1"), _resolver,
            b"audio-bytes", client=client)
    finally:
        await client.aclose()
    assert text == "hi from groq"
    assert seen and b"whisper-large-v3-turbo" in seen[0]
    assert b"chat-model-x" not in seen[0]


async def test_transcribe_all_404_is_clear_502():
    from invincible.core.voice import VoiceGatewayError, transcribe_audio

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "nope"})

    client = _client_for(handler)
    try:
        try:
            await transcribe_audio(
                _candidates("https://api.groq.com/openai/v1"), _resolver,
                b"audio-bytes", client=client)
        except VoiceGatewayError as e:
            assert e.status_code == 502
            assert "no transcription surface" in str(e.body)
        else:
            raise AssertionError("expected VoiceGatewayError")
    finally:
        await client.aclose()


async def test_synthesize_baymax_maps_to_alloy_and_skips_404():
    from invincible.core.voice import synthesize_audio

    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        if "groq" in str(request.url):
            return httpx.Response(404, json={"error": "nope"})
        bodies.append(_json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, content=b"AUDIO",
                              headers={"content-type": "audio/mpeg"})

    client = _client_for(handler)
    try:
        data, ctype = await synthesize_audio(
            _candidates("https://api.groq.com/openai/v1",
                        "https://api.openai.com/v1"),
            _resolver, "Hello.", voice="baymax", client=client)
    finally:
        await client.aclose()
    assert data == b"AUDIO"
    assert ctype.startswith("audio/")
    assert bodies and bodies[0]["voice"] == "alloy"
    assert bodies[0]["model"] == "tts-1"


async def test_synthesize_all_404_is_clear_502():
    from invincible.core.voice import VoiceGatewayError, synthesize_audio

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "nope"})

    client = _client_for(handler)
    try:
        try:
            await synthesize_audio(
                _candidates("https://api.groq.com/openai/v1"), _resolver,
                "Hello.", client=client)
        except VoiceGatewayError as e:
            assert e.status_code == 502
            assert "no speech surface" in str(e.body)
        else:
            raise AssertionError("expected VoiceGatewayError")
    finally:
        await client.aclose()


def test_ordered_voice_candidates_openai_first():
    from invincible.core.voice import _ordered_voice_candidates

    ordered = _ordered_voice_candidates(_candidates(
        "https://api.groq.com/openai/v1", "https://api.openai.com/v1"))
    assert ordered[0]["base_url"] == "https://api.openai.com/v1"


def test_voice_orb_replaces_mic_mute_buttons():
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    chat = (repo / "invincible" / "templates" / "chat.html").read_text(
        encoding="utf-8")
    phone = (repo / "invincible" / "templates" / "phone.html").read_text(
        encoding="utf-8")
    assert 'id="voice-orb"' in chat
    assert 'id="phone-orb"' in phone
    assert "voice-orb.js" in chat and "voice-orb.css" in chat
    assert "voice-orb.js" in phone and "voice-orb.css" in phone
    # Old separate buttons are gone (one orb, tap-to-stop).
    assert "chat-mic" not in chat and "chat-mute" not in chat
    assert "phone-mic" not in phone and "phone-mute" not in phone
    assert (repo / "invincible" / "templates" / "static" /
            "voice-orb.js").exists()
    assert (repo / "invincible" / "templates" / "static" /
            "voice-orb.css").exists()
    orb_js = (repo / "invincible" / "templates" / "static" /
              "voice-orb.js").read_text(encoding="utf-8")
    # B7 free fallback: device-local speech, no fetch, no keys.
    assert "SpeechRecognition" in orb_js
    assert "speechSynthesis" in orb_js
    assert "Free mode" in orb_js


def test_speak_only_on_mic_turns():
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    chat = (repo / "invincible" / "templates" / "chat.html").read_text(
        encoding="utf-8")
    phone = (repo / "invincible" / "templates" / "phone.html").read_text(
        encoding="utf-8")
    # B8: typed turns stay silent; only orb-initiated turns speak.
    for html in (chat, phone):
        assert "voiceNext" in html
        assert "turnShouldSpeak" in html
    assert "if (shouldSpeak) speak(finalText);" in chat
    assert "if (turnShouldSpeak) speak(answer);" in phone


async def test_transcribe_402_surfaces_billing_excerpt():
    from invincible.core.voice import VoiceUpstreamError, transcribe_audio

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            402, json={"error": {"message": "Insufficient credits."}})

    client = _client_for(handler)
    try:
        try:
            await transcribe_audio(
                _candidates("https://api.openai.com/v1"), _resolver,
                b"audio-bytes", client=client)
        except VoiceUpstreamError as e:
            assert e.status_code == 402
            assert "Insufficient credits" in str(e.body)
            assert "test-key-" not in str(e.body)
        else:
            raise AssertionError("expected VoiceUpstreamError")
    finally:
        await client.aclose()


# --- fail-safe: voice off leaves text chat unchanged -------------------------


async def test_text_chat_untouched_when_voice_killed(client, byok_env,
                                                     router_setter,
                                                     monkeypatch):
    def chat_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=provider_body("alpha", "typed hi"))

    router_setter({"alpha.example.com": chat_handler,
                   "beta.example.com": chat_handler,
                   "gamma.example.com": chat_handler})
    monkeypatch.setenv("INVINCIBLE_VOICE", "0")
    _, raw = await v1_user(client, "fail-safe@example.com")
    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}],
              "model": "alpha-model"},
        headers=_auth(raw))
    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["content"] == "typed hi"
