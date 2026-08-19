# tests/test_provider_schema.py
"""Phase 6: providers.yaml schema validation.

Every invalid shape must fail with a ValueError naming the offending
provider and field - the named, fixable error the ROADMAP Phase 6
acceptance criteria require. The shipped providers.yaml must keep
validating, and an empty providers list stays legal (the gateway then
serves an empty /v1/models and 503s requests).
"""
import pytest

from invincible.core.router import Router, validate_providers_config


def valid_provider(**overrides):
    provider = {
        "name": "alpha",
        "tier": 1,
        "base_url": "https://alpha.example.com/v1",
        "api_key_env": "ALPHA_API_KEY",
        "model_id": "alpha-model",
    }
    provider.update(overrides)
    return provider


def test_valid_config_passes():
    validate_providers_config(
        {"providers": [valid_provider(), valid_provider(name="beta", tier=2)]}
    )


def test_empty_providers_list_is_allowed():
    validate_providers_config({"providers": []})


def test_missing_providers_key_raises():
    with pytest.raises(ValueError, match="missing the 'providers' key"):
        validate_providers_config({})


def test_providers_not_a_list_raises():
    with pytest.raises(ValueError, match="must be a YAML list"):
        validate_providers_config({"providers": "alpha"})


def test_provider_not_a_mapping_raises():
    with pytest.raises(ValueError, match="must be a YAML mapping"):
        validate_providers_config({"providers": ["alpha"]})


def test_missing_required_field_names_the_field():
    provider = valid_provider()
    del provider["api_key_env"]
    with pytest.raises(ValueError, match="api_key_env"):
        validate_providers_config({"providers": [provider]})


def test_tier_must_be_positive_int():
    for bad in ("1", 1.5, True, 0):
        with pytest.raises(ValueError, match="'tier' must be an integer >= 1"):
            validate_providers_config({"providers": [valid_provider(tier=bad)]})


def test_duplicate_provider_names_raise():
    with pytest.raises(ValueError, match="Duplicate provider name 'alpha'"):
        validate_providers_config(
            {"providers": [valid_provider(), valid_provider(tier=2)]}
        )


def test_base_url_must_be_http():
    for bad in ("alpha.example.com", "ftp://x"):
        with pytest.raises(ValueError, match="'base_url' must start with http:// or https://"):
            validate_providers_config({"providers": [valid_provider(base_url=bad)]})


def test_required_string_fields_must_be_non_empty():
    for field in ("name", "api_key_env", "model_id"):
        with pytest.raises(ValueError, match=f"'{field}' must be a non-empty string"):
            validate_providers_config({"providers": [valid_provider(**{field: ""})]})


def test_max_context_must_be_positive_int():
    for bad in ("1000", 0, True):
        with pytest.raises(ValueError, match="'max_context' must be an integer >= 1"):
            validate_providers_config({"providers": [valid_provider(max_context=bad)]})


def test_timeout_must_be_mapping_of_positive_numbers():
    with pytest.raises(ValueError, match="'timeout' must be a mapping"):
        validate_providers_config({"providers": [valid_provider(timeout=30)]})
    with pytest.raises(ValueError, match="unknown timeout field"):
        validate_providers_config(
            {"providers": [valid_provider(timeout={"reads": 30})]}
        )
    with pytest.raises(ValueError, match="'timeout.read' must be a positive number"):
        validate_providers_config({"providers": [valid_provider(timeout={"read": -1})]})


def test_aliases_must_be_list_of_non_empty_strings():
    for bad in ("fast", ["fast", ""], [1]):
        with pytest.raises(
            ValueError, match="'aliases' must be a list of non-empty strings"
        ):
            validate_providers_config({"providers": [valid_provider(aliases=bad)]})


def test_duplicate_alias_across_providers_raises():
    with pytest.raises(ValueError, match="Duplicate alias 'fast'.*'alpha' and 'beta'"):
        validate_providers_config(
            {
                "providers": [
                    valid_provider(aliases=["fast"]),
                    valid_provider(name="beta", tier=2, aliases=["fast"]),
                ]
            }
        )


def test_auth_type_restricted_to_known_values():
    with pytest.raises(ValueError, match="'auth_type' must be one of: bearer, query"):
        validate_providers_config({"providers": [valid_provider(auth_type="header")]})


def test_auth_param_and_chat_path_validation():
    with pytest.raises(ValueError, match="'auth_param' must be a non-empty string"):
        validate_providers_config(
            {"providers": [valid_provider(auth_type="query", auth_param="")]}
        )
    with pytest.raises(ValueError, match="'chat_path' must start with '/'"):
        validate_providers_config(
            {"providers": [valid_provider(chat_path="chat/completions")]}
        )


def test_unknown_fields_are_rejected():
    with pytest.raises(ValueError, match="unknown field\\(s\\): base_urll"):
        validate_providers_config(
            {"providers": [valid_provider(base_urll="https://x")]}
        )


def test_shipped_providers_yaml_validates():
    router = Router()
    by_name = {p["name"]: p for p in router.providers}
    assert by_name["nim-glm"]["aliases"] == ["strong"]
    assert by_name["groq-llama"]["aliases"] == ["fast"]
    assert by_name["openrouter-fallback"]["aliases"] == ["free"]
    assert by_name["gemini-flash"]["aliases"] == ["backup"]
    tokenrouter = by_name["tokenrouter-deepseek"]
    assert tokenrouter["tier"] == 1
    assert tokenrouter["base_url"] == "https://api.tokenrouter.com/v1"
    assert tokenrouter["api_key_env"] == "TOKENROUTER_API_KEY"
    assert tokenrouter["model_id"] == "deepseek/deepseek-v4-pro-0813-free"
    assert "aliases" not in tokenrouter
    assert [p["tier"] for p in router.providers] == [1, 2, 3, 4, 5]
