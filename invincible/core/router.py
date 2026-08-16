# invincible/core/router.py
import importlib.resources
import json
import logging
import os
import time
from collections.abc import AsyncIterator

import httpx
import yaml

from invincible.core.provider_health import HealthTracker

logger = logging.getLogger("invincible.router")

DEFAULT_TIMEOUT_CONFIG = {"connect": 5.0, "read": 60.0, "write": 5.0, "pool": 2.0}


def load_providers_config(config_path: str = None) -> dict:
    """Load and validate the provider configuration as a YAML mapping.

    An explicit ``config_path`` is authoritative. Otherwise the canonical
    packaged configuration (``invincible/providers.yaml``) is loaded through
    ``importlib.resources`` so it works identically from a Git checkout, an
    editable install, or a wheel - never from the current working directory.

    Backward compatibility: if the packaged resource cannot be read, fall
    back to the deprecated repository-root ``providers.yaml``. The packaged
    copy always has priority; the root copy can be removed in a future
    release.

    Raises FileNotFoundError for a missing explicit path and ValueError for
    malformed or non-mapping YAML.
    """
    if config_path is None:
        try:
            ref = importlib.resources.files("invincible").joinpath("providers.yaml")
            with ref.open("r", encoding="utf-8") as f:
                raw = f.read()
            source = str(ref)
        except (
            FileNotFoundError,
            ModuleNotFoundError,
            TypeError,
            AttributeError,
        ) as exc:
            legacy = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "providers.yaml",
            )
            logger.warning(
                "Packaged providers.yaml unavailable (%s); using deprecated "
                "repository-root copy at %s", exc, legacy
            )
            with open(legacy, encoding="utf-8") as f:
                raw = f.read()
            source = legacy
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
    return config


def resolve_timeout(provider: dict) -> httpx.Timeout:
    """Build an httpx.Timeout for a provider, using its own `timeout:` block
    from providers.yaml where present, falling back to DEFAULT_TIMEOUT_CONFIG
    field-by-field for anything the provider doesn't override."""
    cfg = {**DEFAULT_TIMEOUT_CONFIG, **(provider.get("timeout") or {})}
    return httpx.Timeout(
        connect=cfg["connect"], read=cfg["read"], write=cfg["write"], pool=cfg["pool"]
    )

DEFAULT_MAX_CONTEXT = 32000
RESERVE_TOKENS = 1000  # headroom left for the provider's own response


def estimate_tokens(message: dict) -> int:
    """Rough token estimate: ~4 chars per token. This is a heuristic, not an
    exact tokenizer match - it will over/under-count on code-heavy content,
    but it's cheap and good enough to decide what to drop, not to bill by."""
    return max(1, len(json.dumps(message)) // 4)


def group_into_turns(messages: list) -> list:
    """Group non-system messages into turns, where a new turn starts at each
    user message. This keeps an assistant's tool_calls together with the
    tool result message(s) that answer them and the eventual assistant
    follow-up, since all of those belong to the same user turn and must
    never be split apart when trimming."""
    turns = []
    current = []
    for m in messages:
        if m.get("role") == "user" and current:
            turns.append(current)
            current = []
        current.append(m)
    if current:
        turns.append(current)
    return turns


def trim_messages(
    messages: list,
    max_context: int,
    reserve_tokens: int = RESERVE_TOKENS,
) -> list:
    """Keep all system messages, then keep as many of the most recent turns
    as fit inside max_context (minus reserve_tokens for the response).
    Always keeps at least the single most recent turn, even if it alone
    exceeds budget - there's nothing better to send in that case.
    Turns are dropped as atomic units so a tool_call is never separated
    from its tool result."""
    system_msgs = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]

    def turn_tokens(turn):
        return sum(estimate_tokens(m) for m in turn)

    system_tokens = sum(estimate_tokens(m) for m in system_msgs)
    budget = max(max_context - reserve_tokens - system_tokens, 0)

    turns = group_into_turns(rest)
    if not turns:
        return system_msgs

    kept = [turns[-1]]
    used = turn_tokens(turns[-1])

    for turn in reversed(turns[:-1]):
        t = turn_tokens(turn)
        if used + t > budget:
            break
        kept.insert(0, turn)
        used += t

    return system_msgs + [m for turn in kept for m in turn]

class UpstreamClientError(Exception):
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self.body = body
        super().__init__(str(body))


def _log_attempt(
    name: str,
    model_id: str,
    payload_bytes: int,
    estimated_tokens: int,
    status,
    failover: bool,
    level: int = logging.INFO,
    **extra,
):
    """Emit one concise structured line per provider attempt.

    Only sizes and outcome are logged; payload content, keys, and headers
    are never included.
    """
    suffix = "".join(f" {k}={v}" for k, v in extra.items())
    logger.log(
        level,
        "provider=%s model=%s payload_bytes=%d estimated_tokens=%d "
        "status=%s failover=%s%s",
        name,
        model_id,
        payload_bytes,
        estimated_tokens,
        status,
        "true" if failover else "false",
        suffix,
    )

class AllProvidersFailedError(Exception):
    """Every provider failed, was disabled, or is in cooldown; nothing left to try."""

_TIMEOUT_KIND_BY_CLASS = {
    "ConnectTimeout": "connect_timeout",
    "ReadTimeout": "read_timeout",
    "WriteTimeout": "write_timeout",
    "PoolTimeout": "pool_timeout",
    "TimeoutException": "timeout",
}


def _network_error_details(e: Exception) -> dict:
    """Structured, safe detail fields for a network-error log line.

    Never includes payload content, headers, or keys - only the exception
    class, a coarse kind, and a truncated message. httpx streaming timeouts
    surface with an empty message, hence the explicit fallback.
    """
    cls = type(e).__name__
    kind = _TIMEOUT_KIND_BY_CLASS.get(
        cls,
        "timeout" if isinstance(e, httpx.TimeoutException) else "network_error",
    )
    message = str(e) or getattr(e, "message", "") or "no_message"
    return {"error_type": cls, "error_kind": kind, "error_msg": message.strip()[:200]}

async def _iter_stream(resp: httpx.Response) -> AsyncIterator[dict]:
    """Yield parsed OpenAI SSE events from a streaming httpx response.

    Skips non-``data:`` lines (SSE keep-alives / comments), stops at the
    ``[DONE]`` sentinel, and always closes the upstream response so the
    connection is released even on client disconnect or mid-stream error.
    """
    try:
        async for line in resp.aiter_lines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if not data or data == "[DONE]":
                break
            yield json.loads(data)
    finally:
        await resp.aclose()

class Router:
    def __init__(self, config_path=None, transport=None):
        config = load_providers_config(config_path)
        self.providers = config.get("providers", [])
        required_fields = {"name", "tier", "base_url", "api_key_env", "model_id"}
        for provider in self.providers:
            missing = required_fields - set(provider.keys())
            if missing:
                raise ValueError(
                    f"Provider '{provider.get('name', 'unnamed')}' is missing required "
                    f"field(s): {', '.join(sorted(missing))}"
                )
        self.providers.sort(key=lambda p: p["tier"])
        for provider in self.providers:
            if not os.getenv(provider["api_key_env"]):
                logger.warning(
                    f"Provider '{provider['name']}' has no API key set via "
                    f"{provider['api_key_env']}. It will be unavailable."
                )
        self.health_tracker = HealthTracker()
        self.client = httpx.AsyncClient(transport=transport)

    async def route_request(
        self, messages: list, tools: list | None = None, tool_choice=None
    ) -> dict:
        for provider in self.providers:
            name = provider["name"]

            if not self.health_tracker.is_available(name):
                logger.info(f"Provider {name} in cooldown. Skipping.")
                continue

            api_key = os.getenv(provider["api_key_env"])
            if not api_key:
                logger.warning(
                    f"No API key found for {name} "
                    f"({provider['api_key_env']}). Skipping."
                )
                continue

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
            trimmed_messages = trim_messages(
                messages, provider.get("max_context", DEFAULT_MAX_CONTEXT)
            )
            payload = {
                "model": provider["model_id"],
                "messages": trimmed_messages
            }
            if tools:
                payload["tools"] = tools
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice

            payload_bytes = len(json.dumps(payload, ensure_ascii=False))
            estimated_tokens = sum(
                estimate_tokens(m)
                for m in trimmed_messages + (payload.get("tools") or [])
            )

            attempt_started = time.monotonic()
            try:
                resp = await self.client.post(
                    f"{provider['base_url']}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=resolve_timeout(provider)
                )

                if (
                    resp.status_code == 429
                    or resp.status_code in (402, 404, 408, 413)
                    or resp.status_code >= 500
                ):
                    _log_attempt(
                        name,
                        provider["model_id"],
                        payload_bytes,
                        estimated_tokens,
                        resp.status_code,
                        True,
                        level=logging.WARNING,
                    )
                    self.health_tracker.record_failure(name)
                    await resp.aclose()
                    continue

                resp.raise_for_status()
                body = await resp.aread()
                try:
                    parsed = json.loads(body)
                except json.JSONDecodeError:
                    logger.warning(
                        f"Malformed JSON (non-JSON 200) from {name}: "
                        f"{body[:200]!r}. Triggering failover."
                    )
                    _log_attempt(
                        name,
                        provider["model_id"],
                        payload_bytes,
                        estimated_tokens,
                        "malformed_json",
                        True,
                        level=logging.WARNING,
                    )
                    self.health_tracker.record_failure(name)
                    await resp.aclose()
                    continue
                self.health_tracker.record_success(name)
                _log_attempt(
                    name,
                    provider["model_id"],
                    payload_bytes,
                    estimated_tokens,
                    resp.status_code,
                    False,
                )
                return parsed

            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in (401, 403):
                    _log_attempt(
                        name,
                        provider["model_id"],
                        payload_bytes,
                        estimated_tokens,
                        status,
                        True,
                        level=logging.WARNING,
                        disabled=True,
                    )
                    self.health_tracker.disable(name)
                    await e.response.aclose()
                    continue
                _log_attempt(
                    name,
                    provider["model_id"],
                    payload_bytes,
                    estimated_tokens,
                    status,
                    False,
                    level=logging.WARNING,
                )
                body = await e.response.aread()
                try:
                    parsed = json.loads(body)
                except json.JSONDecodeError:
                    parsed = {"raw": body.decode(errors="replace")}
                raise UpstreamClientError(status_code=status, body=parsed) from e

            except httpx.RequestError as e:
                details = _network_error_details(e)
                logger.error(
                    "Network error with %s (%s): %s. Triggering failover.",
                    name,
                    details["error_type"],
                    details["error_msg"],
                )
                _log_attempt(
                    name,
                    provider["model_id"],
                    payload_bytes,
                    estimated_tokens,
                    "network_error",
                    True,
                    level=logging.ERROR,
                    elapsed_s=round(time.monotonic() - attempt_started, 2),
                    read_timeout_s=resolve_timeout(provider).read,
                    **details,
                )
                self.health_tracker.record_failure(name)
                continue

        raise AllProvidersFailedError("All providers failed or are in cooldown.")

    async def stream_open(
        self, messages: list, tools: list | None = None, tool_choice=None
    ) -> tuple[dict | None, AsyncIterator[dict]]:
        """Open a streaming chat-completions response through the providers.

        Mirrors ``route_request``'s failover decisions exactly (tier order,
        cooldowns, missing keys, 429/402/404/408/413/5xx and 401/403 handling)
        so streaming reuses the same routing behavior. Returns once a
        provider's stream is live: ``(first_chunk, tail)``. ``first_chunk`` is
        ``None`` for a clean but empty stream. Connection-stage failures fail
        over to the next provider; a mid-stream error after the first chunk
        propagates to the caller so it can terminate the response cleanly.
        """
        for provider in self.providers:
            name = provider["name"]

            if not self.health_tracker.is_available(name):
                logger.info(f"Provider {name} in cooldown. Skipping.")
                continue

            api_key = os.getenv(provider["api_key_env"])
            if not api_key:
                logger.warning(
                    f"No API key found for {name} "
                    f"({provider['api_key_env']}). Skipping."
                )
                continue

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            trimmed_messages = trim_messages(
                messages, provider.get("max_context", DEFAULT_MAX_CONTEXT)
            )
            payload = {
                "model": provider["model_id"],
                "messages": trimmed_messages,
                "stream": True,
            }
            if tools:
                payload["tools"] = tools
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice

            payload_bytes = len(json.dumps(payload, ensure_ascii=False))
            estimated_tokens = sum(
                estimate_tokens(m)
                for m in trimmed_messages + (payload.get("tools") or [])
            )

            attempt_started = time.monotonic()
            try:
                request = self.client.build_request(
                    "POST",
                    f"{provider['base_url']}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=resolve_timeout(provider),
                )
                resp = await self.client.send(request, stream=True)

                if (
                    resp.status_code == 429
                    or resp.status_code in (402, 404, 408, 413)
                    or resp.status_code >= 500
                ):
                    _log_attempt(
                        name,
                        provider["model_id"],
                        payload_bytes,
                        estimated_tokens,
                        resp.status_code,
                        True,
                        level=logging.WARNING,
                    )
                    self.health_tracker.record_failure(name)
                    await resp.aclose()
                    continue

                if resp.status_code in (401, 403):
                    _log_attempt(
                        name,
                        provider["model_id"],
                        payload_bytes,
                        estimated_tokens,
                        resp.status_code,
                        True,
                        level=logging.WARNING,
                        disabled=True,
                    )
                    self.health_tracker.disable(name)
                    await resp.aclose()
                    continue

                if resp.status_code >= 400:
                    _log_attempt(
                        name,
                        provider["model_id"],
                        payload_bytes,
                        estimated_tokens,
                        resp.status_code,
                        False,
                        level=logging.WARNING,
                    )
                    body = await resp.aread()
                    await resp.aclose()
                    try:
                        parsed = json.loads(body)
                    except json.JSONDecodeError:
                        parsed = {"raw": body.decode(errors="replace")}
                    raise UpstreamClientError(
                        status_code=resp.status_code, body=parsed
                    )

                tail = _iter_stream(resp)
                try:
                    first = await anext(tail)
                except StopAsyncIteration:
                    first = None
                self.health_tracker.record_success(name)
                _log_attempt(
                    name,
                    provider["model_id"],
                    payload_bytes,
                    estimated_tokens,
                    resp.status_code,
                    False,
                )
                return first, tail

            except json.JSONDecodeError as e:
                logger.warning(
                    f"Malformed SSE from {name}: {e}. Triggering failover."
                )
                _log_attempt(
                    name,
                    provider["model_id"],
                    payload_bytes,
                    estimated_tokens,
                    "malformed_sse",
                    True,
                    level=logging.WARNING,
                )
                self.health_tracker.record_failure(name)
                continue

            except httpx.RequestError as e:
                details = _network_error_details(e)
                logger.error(
                    "Network error with %s (%s): %s. Triggering failover.",
                    name,
                    details["error_type"],
                    details["error_msg"],
                )
                _log_attempt(
                    name,
                    provider["model_id"],
                    payload_bytes,
                    estimated_tokens,
                    "network_error",
                    True,
                    level=logging.ERROR,
                    elapsed_s=round(time.monotonic() - attempt_started, 2),
                    read_timeout_s=resolve_timeout(provider).read,
                    **details,
                )
                self.health_tracker.record_failure(name)
                continue

        raise AllProvidersFailedError("All providers failed or are in cooldown.")

    async def close(self):
        await self.client.aclose()
