# invincible/core/vision.py
"""Multimodal image parts (F12): the model truly sees screenshots.

Today screenshot results persist as tool-message JSON text carrying a
multi-megabyte ``data_b64`` blob, which the router forwards verbatim:
the model receives truncated base64 *text* (it cannot see images) and
the blob's ~700K phantom tokens evict everything but the newest turn
from context. This module is the send-time-only fix (same discipline
as compression/relay: persisted history stays verbatim):

- :func:`extract_images` (pre-trim): pluck screenshot images out of
  tool-message content, replacing each blob with a compact text
  summary. Trim then budgets on text, not base64.
- :func:`attach_images` (post-trim, per provider): re-attach the held
  images as OpenAI ``image_url`` content parts inside the same tool
  messages (pairing-neutral: same message, same position) when the
  provider is vision-flagged; otherwise the summaries stand as the
  text-only fallback.

Budgeting: at most :data:`VISION_MAX_IMAGES` images per payload
(newest wins); each image at most :data:`VISION_MAX_IMAGE_BYTES`
bytes — over either cap the image is dropped with a note in its
summary, never truncated into garbage. Token estimates use the PNG
dimensions (parsed stdlib-only from the IHDR chunk, no Pillow) with
the standard high-detail tile formula; unreadable dims fall back to
:data:`VISION_TOKEN_FALLBACK`.

Pure functions, no I/O, never raise on malformed input (a bad image
is an omitted image with a note, never a failed turn).
"""
import base64
import binascii
import json
import math
import struct

# Screenshot results we forward as images (same allowlist the B0.3
# browser preview uses: PNG captures only).
VISION_MIME_ALLOW = ("image/png",)

# Newest N images per payload win; older ones are omitted with a note.
VISION_MAX_IMAGES = 4
# Per-image byte cap (the 2MB capture cap is far too big for context).
VISION_MAX_IMAGE_BYTES = 512 * 1024
# Token estimate when PNG dimensions are unreadable.
VISION_TOKEN_FALLBACK = 1500

_PNG_SIGNATURE = bytes((137, 80, 78, 71, 13, 10, 26, 10))


def png_dimensions(data_b64: str) -> tuple[int, int] | None:
    """``(width, height)`` from a base64 PNG's IHDR chunk (stdlib-only).

    None for anything unparsable (not base64, not a PNG, truncated
    header). Never raises.
    """
    if not isinstance(data_b64, str) or not data_b64:
        return None
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(raw) < 24 or raw[:8] != _PNG_SIGNATURE:
        return None
    try:
        width, height = struct.unpack(">II", raw[16:24])
    except struct.error:
        return None
    if width <= 0 or height <= 0:
        return None
    return (width, height)


def image_token_estimate(mime: str, data_b64: str,
                         size_bytes: int = 0) -> int:
    """Token cost of one image part (high-detail tile formula).

    Dimensions come from the PNG header when readable; otherwise the
    flat fallback. Non-allowlisted input still returns the fallback
    (callers gate on mime; estimation never refuses). Never raises.
    """
    _ = (mime, size_bytes)
    dims = png_dimensions(data_b64)
    if dims is None:
        return VISION_TOKEN_FALLBACK
    width, height = dims
    # Fit inside 2048, then shortest side to 768, then 512px tiles.
    scale = min(1.0, 2048.0 / max(width, height))
    width, height = width * scale, height * scale
    scale = 768.0 / min(width, height) if min(width, height) > 0 else 1.0
    tiles = (math.ceil(width * scale / 512.0)
             * math.ceil(height * scale / 512.0))
    return 85 + 170 * max(tiles, 1)


def image_url_token_estimate(url: str) -> int:
    """Token cost of an ``image_url`` part's ``url`` value.

    ``data:{mime};base64,…`` URIs estimate from the payload; anything
    else (http URLs, malformed) takes the flat fallback. Never raises.
    """
    if not isinstance(url, str) or not url.startswith("data:"):
        return VISION_TOKEN_FALLBACK
    try:
        header, _, data = url.partition(",")
        mime = header[5:].split(";")[0]
        return image_token_estimate(mime, data)
    except Exception:
        return VISION_TOKEN_FALLBACK


def _screenshot_candidate(message: dict) -> dict | None:
    """Parsed screenshot payload for a tool message, else None."""
    if not isinstance(message, dict):
        return None
    if message.get("role") != "tool":
        return None
    call_id = message.get("tool_call_id")
    if not isinstance(call_id, str) or not call_id:
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    try:
        result = json.loads(content)
    except ValueError:
        return None
    if not isinstance(result, dict):
        return None
    if result.get("status") != "screenshot":
        return None
    if result.get("mime") not in VISION_MIME_ALLOW:
        return None
    data = result.get("data_b64")
    size = result.get("bytes")
    if not isinstance(data, str) or not data:
        return None
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        return None
    return {"tool_call_id": call_id, "mime": result["mime"],
            "data_b64": data, "bytes": size,
            "url": result.get("url")}


def _summary_text(candidate: dict, note: str | None = None) -> str:
    """Compact text summary replacing a blob (never carries image data)."""
    summary: dict = {"status": "screenshot",
                     "mime": candidate["mime"],
                     "bytes": candidate["bytes"]}
    url = candidate.get("url")
    if isinstance(url, str) and url:
        summary["url"] = url
    if note:
        summary["note"] = note
    return json.dumps(summary, ensure_ascii=False)


def extract_images(messages: list) -> tuple[list, dict]:
    """Strip screenshot blobs to summaries; hold images for send-time.

    Returns ``(new_messages, sendable)`` where ``sendable`` maps
    ``tool_call_id`` to the held image. Messages without screenshots
    pass through as the SAME objects (copy-on-write: only rewritten
    tool messages are rebuilt, so persisted history is untouched).
    Caps: over :data:`VISION_MAX_IMAGE_BYTES` an image is omitted with
    a note; over :data:`VISION_MAX_IMAGES` the oldest are omitted with
    a note (newest wins).
    """
    found: list[tuple[int, dict]] = []
    for index, message in enumerate(messages):
        candidate = _screenshot_candidate(message)
        if candidate is not None:
            found.append((index, candidate))
    sendable_ids: set[str] = set()
    for candidate in [c for _, c in found[-VISION_MAX_IMAGES:]]:
        if candidate["bytes"] <= VISION_MAX_IMAGE_BYTES:
            sendable_ids.add(candidate["tool_call_id"])
    sendable: dict[str, dict] = {}
    new_messages: list = []
    for message in messages:
        candidate = _screenshot_candidate(message)
        if candidate is None:
            new_messages.append(message)
            continue
        call_id = candidate["tool_call_id"]
        if call_id in sendable_ids:
            sendable[call_id] = {
                "mime": candidate["mime"],
                "data_b64": candidate["data_b64"],
                "bytes": candidate["bytes"],
            }
            rebuilt = {**message,
                       "content": _summary_text(candidate)}
        elif candidate["bytes"] > VISION_MAX_IMAGE_BYTES:
            rebuilt = {**message, "content": _summary_text(
                candidate,
                note=f"image omitted: {candidate['bytes']} bytes over "
                     f"the {VISION_MAX_IMAGE_BYTES}-byte send cap")}
        else:
            rebuilt = {**message, "content": _summary_text(
                candidate,
                note=f"image omitted: over the {VISION_MAX_IMAGES}-image "
                     "per-request cap (newest kept)")}
        new_messages.append(rebuilt)
    return new_messages, sendable


def attach_images(messages: list, sendable: dict,
                  *, capable: bool) -> list:
    """Re-attach held images as ``image_url`` parts (send-time only).

    Vision-flagged providers get each held image appended to its tool
    message as a content list ``[text summary, image part]`` — same
    message, same position, so tool pairing is untouched. Anything
    else (incapable provider, unknown id) leaves messages as-is: the
    text summaries from :func:`extract_images` are the fallback.
    Messages without held images pass through as the SAME objects.
    """
    if not capable or not sendable:
        return messages
    attached: list = []
    for message in messages:
        call_id = message.get("tool_call_id") if isinstance(
            message, dict) else None
        image = sendable.get(call_id) if isinstance(
            sendable, dict) else None
        if not isinstance(image, dict):
            attached.append(message)
            continue
        try:
            url = (f"data:{image['mime']};base64,{image['data_b64']}")
        except (KeyError, TypeError):
            attached.append(message)
            continue
        text = message.get("content")
        if not isinstance(text, str):
            text = ""
        attached.append({**message, "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": url}},
        ]})
    return attached


def provider_vision(provider: dict) -> bool:
    """Whether one attempt may receive image parts (fail-closed).

    Only an explicit ``vision: true`` on the provider entry (static
    YAML or BYOK candidate) enables images — anything else, including
    truthy non-bools, stays text-only.
    """
    if not isinstance(provider, dict):
        return False
    return provider.get("vision") is True
