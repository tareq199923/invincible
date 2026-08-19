from invincible.core.router import DEFAULT_TIMEOUT_CONFIG, resolve_timeout


def test_provider_without_timeout_block_uses_defaults():
    provider = {"name": "no-override"}
    timeout = resolve_timeout(provider)
    assert timeout.connect == DEFAULT_TIMEOUT_CONFIG["connect"]
    assert timeout.read == DEFAULT_TIMEOUT_CONFIG["read"]
    assert timeout.write == DEFAULT_TIMEOUT_CONFIG["write"]
    assert timeout.pool == DEFAULT_TIMEOUT_CONFIG["pool"]


def test_provider_with_partial_timeout_override_merges_with_defaults():
    provider = {"name": "gemini-like", "timeout": {"read": 90.0}}
    timeout = resolve_timeout(provider)
    assert timeout.read == 90.0
    assert timeout.connect == DEFAULT_TIMEOUT_CONFIG["connect"]
    assert timeout.write == DEFAULT_TIMEOUT_CONFIG["write"]
    assert timeout.pool == DEFAULT_TIMEOUT_CONFIG["pool"]


def test_provider_with_full_timeout_override():
    provider = {
        "name": "custom",
        "timeout": {"connect": 1.0, "read": 20.0, "write": 1.0, "pool": 1.0},
    }
    timeout = resolve_timeout(provider)
    assert timeout.connect == 1.0
    assert timeout.read == 20.0
    assert timeout.write == 1.0
    assert timeout.pool == 1.0


def test_real_providers_yaml_parses_with_timeout_overrides():
    """Guards against a YAML typo breaking the actual providers.yaml
    shipped in this repo."""
    from invincible.core.router import Router

    router = Router()
    by_name = {p["name"]: p for p in router.providers}

    assert resolve_timeout(by_name["nim-glm"]).read == 90.0
    assert resolve_timeout(by_name["groq-llama"]).read == 45.0
    assert resolve_timeout(by_name["openrouter-fallback"]).read == 90.0
    assert resolve_timeout(by_name["gemini-flash"]).read == 90.0
    assert resolve_timeout(by_name["tokenrouter-deepseek"]).read == 90.0
