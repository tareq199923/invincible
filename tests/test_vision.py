# tests/test_vision.py
"""F12 multimodal image parts: the model truly sees screenshots.

Pure extract/attach/cap/estimate helpers are hermetic; router-level
tests drive ``make_router`` against ``httpx.MockTransport`` (no real
providers) asserting the ``image_url`` part is present exactly when
the provider is vision-flagged. The real mouse is never driven and no
window is ever opened."""

import base64
import json
import struct

import httpx

from invincible.compat.common import repair_tool_pairing
from invincible.core import vision
from invincible.core.trimming import estimate_tokens
from tests.conftest import default_providers, provider_body


def _png_b64(width: int, height: int) -> str:
    raw = (bytes((137, 80, 78, 71, 13, 10, 26, 10))
           + struct.pack(">I", 13) + b"IHDR"
           + struct.pack(">II", width, height))
    return base64.b64encode(raw).decode("ascii")


def _shot_message(call_id="call_1", width=100, height=50,
                  size=4096, mime="image/png", url="https://x.example/"):
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps({
            "status": "screenshot", "mime": mime,
            "data_b64": _png_b64(width, height), "bytes": size,
            "url": url,
        }),
    }


def test_png_dimensions_valid_and_garbage():
    assert vision.png_dimensions(_png_b64(100, 50)) == (100, 50)
    assert vision.png_dimensions("!!!not-base64!!!") is None
    assert vision.png_dimensions(
        base64.b64encode(b"just some bytes").decode()) is None
    assert vision.png_dimensions(
        base64.b64encode(b"short").decode()) is None
    assert vision.png_dimensions("") is None
    assert vision.png_dimensions(None) is None
    zero = base64.b64encode(
        bytes((137, 80, 78, 71, 13, 10, 26, 10))
        + struct.pack(">I", 13) + b"IHDR"
        + struct.pack(">II", 0, 50)).decode()
    assert vision.png_dimensions(zero) is None


def test_image_token_estimate_tile_formula_and_fallback():
    # 100x50: no 2048 shrink; short side 50 -> x15.36 => 1536x768;
    # 3x2 tiles of 512 => 85 + 170*6.
    assert vision.image_token_estimate(
        "image/png", _png_b64(100, 50)) == 1105
    assert vision.image_token_estimate(
        "image/png", "garbage") == vision.VISION_TOKEN_FALLBACK
    assert vision.image_token_estimate(
        "image/png", None) == vision.VISION_TOKEN_FALLBACK
    assert vision.image_url_token_estimate(
        "data:image/png;base64," + _png_b64(100, 50)) == 1105
    assert vision.image_url_token_estimate(
        "https://cdn.example/shot.png") == vision.VISION_TOKEN_FALLBACK
    assert vision.image_url_token_estimate(None) == (
        vision.VISION_TOKEN_FALLBACK)


def test_extract_replaces_blob_copy_on_write():
    message = _shot_message()
    before = json.dumps(message, sort_keys=True)
    user = {"role": "user", "content": "what do you see?"}
    new_messages, sendable = vision.extract_images([user, message])
    # Untouched messages pass through as the same objects.
    assert new_messages[0] is user
    # Rewritten tool message is a NEW object; the caller's keeps the blob.
    assert new_messages[1] is not message
    assert json.dumps(message, sort_keys=True) == before
    assert "data_b64" in message["content"]
    summary = json.loads(new_messages[1]["content"])
    assert summary["status"] == "screenshot"
    assert summary["mime"] == "image/png"
    assert summary["bytes"] == 4096
    assert summary["url"] == "https://x.example/"
    assert "data_b64" not in summary
    assert "note" not in summary
    assert set(sendable) == {"call_1"}
    assert sendable["call_1"]["bytes"] == 4096


def test_extract_ignores_non_screenshots():
    plain = {"role": "tool", "tool_call_id": "c1",
             "content": json.dumps({"status": "ok"})}
    wrong_mime = _shot_message("c2", mime="image/jpeg")
    no_id = {"role": "tool", "content": json.dumps(
        {"status": "screenshot"})}
    broken = {"role": "tool", "tool_call_id": "c4",
              "content": "{not json"}
    already_list = {"role": "tool", "tool_call_id": "c5",
                    "content": [{"type": "text", "text": "hi"}]}
    user = {"role": "user", "content": "hi"}
    originals = [plain, wrong_mime, no_id, broken, already_list, user]
    new_messages, sendable = vision.extract_images(originals)
    assert sendable == {}
    for before, after in zip(originals, new_messages, strict=True):
        assert after is before


def test_extract_oversize_dropped_with_note():
    big = _shot_message(size=vision.VISION_MAX_IMAGE_BYTES + 1)
    new_messages, sendable = vision.extract_images([big])
    assert sendable == {}
    summary = json.loads(new_messages[0]["content"])
    assert "data_b64" not in summary
    assert "over" in summary["note"]
    assert str(vision.VISION_MAX_IMAGE_BYTES) in summary["note"]


def test_extract_count_cap_newest_wins():
    messages = [_shot_message(f"call_{i}") for i in range(6)]
    new_messages, sendable = vision.extract_images(messages)
    assert sorted(sendable) == [f"call_{i}" for i in range(2, 6)]
    for kept in new_messages[2:]:
        assert "note" not in json.loads(kept["content"])
    for dropped in new_messages[:2]:
        note = json.loads(dropped["content"])["note"]
        assert "per-request cap" in note


def test_attach_capable_builds_content_list():
    message = _shot_message()
    new_messages, sendable = vision.extract_images([message])
    attached = vision.attach_images(
        new_messages, sendable, capable=True)
    assert attached[0] is not new_messages[0]
    content = attached[0]["content"]
    assert isinstance(content, list) and len(content) == 2
    assert content[0]["type"] == "text"
    assert "data_b64" not in content[0]["text"]
    part = content[1]
    assert part["type"] == "image_url"
    assert part["image_url"]["url"].startswith("data:image/png;base64,")
    assert part["image_url"]["url"].endswith(_png_b64(100, 50))
    assert attached[0]["role"] == "tool"
    assert attached[0]["tool_call_id"] == "call_1"


def test_attach_incapable_is_identity():
    message = _shot_message()
    new_messages, sendable = vision.extract_images([message])
    assert vision.attach_images(
        new_messages, sendable, capable=False) is new_messages
    assert vision.attach_images(new_messages, {}, capable=True) is (
        new_messages)
    other = {"role": "tool", "tool_call_id": "nope", "content": "x"}
    out = vision.attach_images(
        [other], {"call_1": {"mime": "image/png", "data_b64": "eA==",
                             "bytes": 1}}, capable=True)
    assert out[0] is other


def test_provider_vision_fail_closed():
    assert vision.provider_vision({"vision": True}) is True
    assert vision.provider_vision({}) is False
    assert vision.provider_vision({"vision": False}) is False
    assert vision.provider_vision({"vision": "true"}) is False
    assert vision.provider_vision({"vision": 1}) is False
    assert vision.provider_vision(None) is False
    assert vision.provider_vision("vision") is False


def test_estimate_tokens_image_aware():
    big_b64 = _png_b64(100, 50) * 500
    message = {
        "role": "tool", "tool_call_id": "c1",
        "content": [
            {"type": "text", "text": "screenshot of page"},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64," + big_b64}},
        ],
    }
    assert estimate_tokens(message) < 5000
    text_only = {"role": "user", "content": "hello world"}
    assert estimate_tokens(text_only) == max(
        1, len(json.dumps(text_only)) // 4)


def test_stage_is_identity_without_screenshots():
    messages = [
        {"role": "user", "content": "run ls"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "execute_bash",
                                      "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1",
         "content": json.dumps({"status": "ok", "x": 1})},
    ]
    before = json.dumps(messages, sort_keys=True)
    extracted, sendable = vision.extract_images(messages)
    attached = vision.attach_images(extracted, sendable, capable=True)
    assert json.dumps(attached, sort_keys=True) == before


def test_stage_keeps_tool_pairing():
    messages = [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "screenshot",
                                      "arguments": "{}"}}]},
        _shot_message(),
    ]
    extracted, sendable = vision.extract_images(messages)
    attached = vision.attach_images(extracted, sendable, capable=True)
    repaired = repair_tool_pairing(attached)
    assert [m.get("role") for m in repaired] == ["assistant", "tool"]
    assert repaired[1]["tool_call_id"] == "call_1"


async def test_router_sends_image_part_when_flagged(make_router):
    providers = default_providers()
    providers[0]["vision"] = True
    messages = [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "screenshot",
                                      "arguments": "{}"}}]},
        _shot_message(),
    ]
    snapshot = json.dumps(messages)
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha"))

    router = make_router(
        providers=providers, handlers={"alpha.example.com": alpha_handler})
    result = await router.route_request(messages)
    assert result["choices"][0]["message"]["content"] == "hello"
    sent = captured[0]["messages"][1]
    assert isinstance(sent["content"], list)
    assert sent["content"][1]["type"] == "image_url"
    assert sent["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,")
    # The caller's history stays verbatim (send-time only).
    assert json.dumps(messages) == snapshot
    assert "data_b64" in messages[1]["content"]
    await router.close()


async def test_router_text_only_when_unflagged(make_router):
    messages = [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "screenshot",
                                      "arguments": "{}"}}]},
        _shot_message(),
    ]
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha"))

    router = make_router(handlers={"alpha.example.com": alpha_handler})
    result = await router.route_request(messages)
    assert result["choices"][0]["message"]["content"] == "hello"
    wire = json.dumps(captured[0])
    assert "image_url" not in wire
    assert "data_b64" not in wire
    sent = captured[0]["messages"][1]
    summary = json.loads(sent["content"])
    assert summary["status"] == "screenshot"
    assert summary["bytes"] == 4096
    await router.close()


async def test_router_large_blob_survives_compression_stage(make_router):
    # Load-bearing ordering find: compression truncates tool results to
    # 4000 chars, so extraction must run FIRST — a >4000-char blob must
    # arrive as a byte-exact image, not head/tail-sliced garbage.
    providers = default_providers()
    providers[0]["vision"] = True
    big_data = _png_b64(100, 50) + "A" * 8000
    assert len(big_data) > 4000
    messages = [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "screenshot",
                                      "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1",
         "content": json.dumps({
             "status": "screenshot", "mime": "image/png",
             "data_b64": big_data, "bytes": len(big_data),
             "url": "https://x.example/"})},
    ]
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha"))

    router = make_router(
        providers=providers, handlers={"alpha.example.com": alpha_handler})
    await router.route_request(messages)
    sent_url = captured[0]["messages"][1]["content"][1][
        "image_url"]["url"]
    assert sent_url == "data:image/png;base64," + big_data
    raw = base64.b64decode(sent_url.partition(",")[2], validate=True)
    assert raw[:8] == bytes((137, 80, 78, 71, 13, 10, 26, 10))
    await router.close()


def test_router_vision_rejects_non_bool(provider_config):
    from invincible.core.router import Router

    providers = default_providers()
    providers[0]["vision"] = "yes"
    try:
        Router(config_path=provider_config(providers))
    except ValueError as e:
        assert "'vision' must be a boolean" in str(e)
    else:
        raise AssertionError("non-bool vision accepted")
