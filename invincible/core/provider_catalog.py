# invincible/core/provider_catalog.py
"""Starter BYOK provider catalog (Platform Phase 9).

Operator-supplied constants mirroring the packaged ``providers.yaml``:
the dashboard's connect cards pre-fill ``base_url``/``model_id`` from
here and the user only pastes an API key. A stored credential whose
``base_url`` EQUALS the catalog constant skips the SSRF check at create
time (operator-supplied, not user input); the moment a user edits the
URL it is treated as fully custom and validated. Test and chat-time use
always re-validates regardless.
"""
import copy

CATALOG: dict[str, dict] = {
    "tokenrouter": {
        "label": "TokenRouter",
        "base_url": "https://api.tokenrouter.com/v1",
        "model_id": "qwen/qwen3.8-max-free",
        "max_context": 1_000_000,
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
