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
    shipped in this repo.

    Keyed by api_key_env (stable) rather than provider names/model ids,
    which change with the free-tier lineup."""
    from invincible.core.router import Router

    router = Router()
    by_env = {p["api_key_env"]: p for p in router.providers}

    expected_reads = {
        "TOKENROUTER_API_KEY": 90.0,
        "NVIDIA_API_KEY": 90.0,
        "GROQ_API_KEY": 45.0,
        "OPENROUTER_API_KEY": 90.0,
        "GEMINI_API_KEY": 90.0,
    }
    for env, read in expected_reads.items():
        assert env in by_env, f"shipped config lost provider key {env}"
        assert resolve_timeout(by_env[env]).read == read
