# invincible/core/provider_catalog.py
"""Starter BYOK provider catalog (Platform Phase 9).

Operator-supplied constants mirroring the packaged ``providers.yaml``:
the API's ``catalog_key`` prefill fills ``base_url``/``model_id`` from
here and the user only pastes an API key. A stored credential whose
``base_url`` EQUALS the catalog constant skips the SSRF check at create
time (operator-supplied, not user input); the moment a user edits the
URL it is treated as fully custom and validated. Test and chat-time use
always re-validates regardless.

Only OpenAI-compatible chat-completions providers belong here. The
gateway speaks the Anthropic Messages API to *clients* (Claude Code),
but upstream it only speaks ``POST {base_url}/chat/completions`` with
Bearer auth — a native Anthropic upstream (``POST /v1/messages`` with
``x-api-key`` + ``anthropic-version``) has no outbound client, so no
``anthropic`` entry is offered. Likewise no ``openai_compatible``
placeholder is offered: the dashboard connect form already covers
custom URLs, and an ``https://api.example.com`` constant would be
storable verbatim via the exemption.
"""
import copy

CATALOG: dict[str, dict] = {
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model_id": "gpt-4o-mini",
        "max_context": 128_000,
    },
    "nvidia_nim": {
        "label": "NVIDIA NIM",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "model_id": "deepseek-ai/deepseek-v4-flash-0731",
        "max_context": 1_000_000,
    },
    "groq": {
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "model_id": "openai/gpt-oss-120b",
        "max_context": 128_000,
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "model_id": "nvidia/nemotron-3-ultra-550b-a55b:free",
        "max_context": 1_000_000,
    },
    "gemini": {
        "label": "Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model_id": "gemini-2.5-flash",
        "max_context": 1_000_000,
    },
}


def catalog_entry(key: str | None) -> dict | None:
    """Deep copy of one catalog entry, or None for unknown/absent keys."""
    if not key or key not in CATALOG:
        return None
    return copy.deepcopy(CATALOG[key])
