# invincible/core/config.py
"""Provider configuration loading and validation (extracted from
``core/router.py``, Phase 13).

Owns the ``providers.yaml`` schema: shape validation with named, fixable
error messages, canonical packaged-config loading through
``importlib.resources``, and per-provider httpx timeout resolution.
Nothing here performs network I/O; the Router is the only consumer of the
loaded mapping at runtime.
"""
import importlib.resources
import os

import httpx
import yaml

DEFAULT_TIMEOUT_CONFIG = {"connect": 5.0, "read": 60.0, "write": 5.0, "pool": 2.0}

_REQUIRED_PROVIDER_FIELDS = {"name", "tier", "base_url", "api_key_env", "model_id"}
_TIMEOUT_FIELDS = {"connect", "read", "write", "pool"}
_AUTH_TYPES = ("bearer", "query")
# Extra keys a provider entry may carry (all optional).
_OPTIONAL_PROVIDER_FIELDS = {
    "max_context", "timeout", "aliases", "auth_type", "auth_param", "chat_path",
    "failover_on_400",
}


def validate_providers_config(config: dict) -> None:
    """Validate the ``providers`` mapping shape (Phase 6).

    Runs after YAML load, so ``start`` pre-flight, ``doctor``, and the
    lifespan all get the same named, fixable ``ValueError`` messages.
    Unknown keys are rejected so a typo (``base_urll``) fails loudly at
    startup instead of silently producing an unreachable provider.

    Raises ValueError naming the offending provider and field.
    """
    providers = config.get("providers")
    if providers is None:
        raise ValueError("Provider configuration is missing the 'providers' key")
    if not isinstance(providers, list):
        raise ValueError("Provider configuration: 'providers' must be a YAML list")

    names = set()
    aliases = {}
    for index, provider in enumerate(providers):
        if not isinstance(provider, dict):
            raise ValueError(f"Provider #{index} must be a YAML mapping")
        missing = _REQUIRED_PROVIDER_FIELDS - set(provider)
        if missing:
            raise ValueError(
                f"Provider '{provider.get('name', f'#{index}')}' is missing required "
                f"field(s): {', '.join(sorted(missing))}"
            )
        name = provider["name"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Provider #{index}: 'name' must be a non-empty string")
        if name in names:
            raise ValueError(f"Duplicate provider name '{name}'")
        names.add(name)

        tier = provider["tier"]
        if not isinstance(tier, int) or isinstance(tier, bool) or tier < 1:
            raise ValueError(f"Provider '{name}': 'tier' must be an integer >= 1")
        for field in ("base_url", "api_key_env", "model_id"):
            value = provider[field]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"Provider '{name}': '{field}' must be a non-empty string"
                )
        if not provider["base_url"].startswith(("http://", "https://")):
            raise ValueError(
                f"Provider '{name}': 'base_url' must start with http:// or https://"
            )

        max_context = provider.get("max_context")
        if max_context is not None and (
            not isinstance(max_context, int)
            or isinstance(max_context, bool)
            or max_context < 1
        ):
            raise ValueError(
                f"Provider '{name}': 'max_context' must be an integer >= 1"
            )

        timeout = provider.get("timeout")
        if timeout is not None:
            if not isinstance(timeout, dict):
                raise ValueError(f"Provider '{name}': 'timeout' must be a mapping")
            unknown = set(timeout) - _TIMEOUT_FIELDS
            if unknown:
                raise ValueError(
                    f"Provider '{name}': unknown timeout field(s): "
                    f"{', '.join(sorted(unknown))}"
                )
            for field, value in timeout.items():
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or value <= 0
                ):
                    raise ValueError(
                        f"Provider '{name}': 'timeout.{field}' "
                        "must be a positive number"
                    )

        alias_list = provider.get("aliases")
        if alias_list is not None:
            if not isinstance(alias_list, list) or not all(
                isinstance(a, str) and a.strip() for a in alias_list
            ):
                raise ValueError(
                    f"Provider '{name}': 'aliases' must be a list of non-empty strings"
                )
            for alias in alias_list:
                if alias in aliases:
                    raise ValueError(
                        f"Duplicate alias '{alias}' (providers "
                        f"'{aliases[alias]}' and '{name}')"
                    )
                aliases[alias] = name

        auth_type = provider.get("auth_type", "bearer")
        if auth_type not in _AUTH_TYPES:
            raise ValueError(
                f"Provider '{name}': 'auth_type' must be one of: "
                f"{', '.join(_AUTH_TYPES)}"
            )
        auth_param = provider.get("auth_param")
        if auth_param is not None and (
            not isinstance(auth_param, str) or not auth_param.strip()
        ):
            raise ValueError(
                f"Provider '{name}': 'auth_param' must be a non-empty string"
            )
        chat_path = provider.get("chat_path")
        if chat_path is not None and (
            not isinstance(chat_path, str) or not chat_path.startswith("/")
        ):
            raise ValueError(f"Provider '{name}': 'chat_path' must start with '/'")

        failover_on_400 = provider.get("failover_on_400")
        if failover_on_400 is not None and not isinstance(failover_on_400, bool):
            raise ValueError(
                f"Provider '{name}': 'failover_on_400' must be a boolean"
            )

        unknown_fields = set(provider) - (
            _REQUIRED_PROVIDER_FIELDS | _OPTIONAL_PROVIDER_FIELDS
        )
        if unknown_fields:
            raise ValueError(
                f"Provider '{name}': unknown field(s): "
                f"{', '.join(sorted(unknown_fields))}"
            )


def load_providers_config(config_path: str | None = None) -> dict:
    """Load and validate the provider configuration as a YAML mapping.

    An explicit ``config_path`` is authoritative. Otherwise the canonical
    packaged configuration (``invincible/providers.yaml``) is loaded through
    ``importlib.resources`` so it works identically from a Git checkout, an
    editable install, or a wheel - never from the current working directory.

    Raises FileNotFoundError for a missing explicit path and ValueError for
    malformed or non-mapping YAML.
    """
    if config_path is None:
        ref = importlib.resources.files("invincible").joinpath("providers.yaml")
        with ref.open("r", encoding="utf-8") as f:
            raw = f.read()
        source = str(ref)
    else:
        source = os.path.abspath(config_path)
        if not os.path.isfile(source):
            raise FileNotFoundError(f"Provider configuration not found: {source}")
        with open(source, encoding="utf-8") as f:
            raw = f.read()

    try:
        config = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(
            f"Malformed provider configuration in {source}: {exc}"
        ) from exc
    if not isinstance(config, dict):
        raise ValueError(f"Provider configuration in {source} must be a YAML mapping")
    validate_providers_config(config)
    return config


def resolve_timeout(provider: dict) -> httpx.Timeout:
    """Build an httpx.Timeout for a provider, using its own `timeout:` block
    from providers.yaml where present, falling back to DEFAULT_TIMEOUT_CONFIG
    field-by-field for anything the provider doesn't override."""
    cfg = {**DEFAULT_TIMEOUT_CONFIG, **(provider.get("timeout") or {})}
    return httpx.Timeout(
        connect=cfg["connect"], read=cfg["read"], write=cfg["write"], pool=cfg["pool"]
    )
