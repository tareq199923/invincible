# tests/test_provider_catalog.py
"""BYOK catalog constants: only OpenAI-compatible upstreams, no placeholders.

Hermetic (no Postgres): the catalog is pure operator constants behind the
API's ``catalog_key`` prefill. Guards the audit findings: no native
Anthropic entry (the router only speaks POST {base}/chat/completions with
Bearer), no api.example.com placeholder (storable verbatim via the
create-time exemption), every entry an https URL with a real model id.
"""

from invincible.core.provider_catalog import CATALOG, catalog_entry


def test_no_native_anthropic_entry():
    assert "anthropic" not in CATALOG


def test_no_example_placeholder_entry():
    assert "openai_compatible" not in CATALOG
    for key, entry in CATALOG.items():
        assert "api.example.com" not in entry["base_url"], key
        assert "example.com" not in entry["base_url"], key


def test_remaining_entries_are_openai_compatible_https():
    assert set(CATALOG) == {"openai", "nvidia_nim", "groq", "openrouter", "gemini"}
    for key, entry in CATALOG.items():
        assert entry["base_url"].startswith("https://"), key
        assert isinstance(entry["model_id"], str)
        assert entry["model_id"].strip(), key
        assert isinstance(entry["max_context"], int)
        assert entry["max_context"] >= 1, key


def test_unknown_key_returns_none():
    assert catalog_entry(None) is None
    assert catalog_entry("") is None
    assert catalog_entry("anthropic") is None
    assert catalog_entry("openai_compatible") is None
    assert catalog_entry("tokenrouter") is None
    assert catalog_entry("bogus") is None


def test_known_entry_returns_copy():
    first = catalog_entry("groq")
    assert first is not None
    assert first["base_url"] == "https://api.groq.com/openai/v1"
    first["base_url"] = "mutated"
    assert catalog_entry("groq")["base_url"] == "https://api.groq.com/openai/v1"
