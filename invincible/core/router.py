# invincible/core/router.py
import importlib.resources
import json
import logging
import os
import time
from collections.abc import AsyncIterator

import httpx
import yaml

from invincible.core.compression import compress_messages, compression_enabled
from invincible.core.provider_health import HealthTracker

logger = logging.getLogger("invincible.router")

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


def load_providers_config(config_path: str = None) -> dict:
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


class _Failover(Exception):
    """Internal control-flow signal raised by a transport attempt after the
    failover decision was made, logged, and recorded in the health tracker.
    ``_iter_attempts`` catches it and moves on to the next provider; it
    never escapes the Router."""


def _status_wants_failover(provider: dict, status_code: int) -> bool:
    """The shared failover classification (Phase 13): rate limits,
    billing/not-found/timeout/payload-size classes, server errors - and 400
    only when the provider opted in via ``failover_on_400``. One copy for
    both transports."""
    return (
        status_code == 429
        or status_code in (402, 404, 408, 413)
        or status_code >= 500
        or (
            status_code == 400
            and provider.get("failover_on_400", False)
        )
    )


def _parse_json_or_raw(body: bytes) -> dict:
    """Parse an upstream error body, degrading to a ``{"raw": ...}`` mapping
    for non-JSON payloads so UpstreamClientError always carries a dict."""
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw": body.decode(errors="replace")}


def _all_providers_failed() -> AllProvidersFailedError:
    """Build the terminal exhaustion error with its one canonical message."""
    return AllProvidersFailedError("All providers failed or are in cooldown.")


class Router:
    def __init__(self, config_path=None, transport=None):
        config = load_providers_config(config_path)
        self.providers = config.get("providers", [])
        # Shape is validated in load_providers_config (Phase 6); sort by
        # tier here so failover order never depends on YAML order.
        self.providers.sort(key=lambda p: p["tier"])
        for provider in self.providers:
            if not os.getenv(provider["api_key_env"]):
                logger.warning(
                    f"Provider '{provider['name']}' has no API key set via "
                    f"{provider['api_key_env']}. It will be unavailable."
                )
        self.health_tracker = HealthTracker()
        self.client = httpx.AsyncClient(transport=transport)

    def _ordered_providers(self, model: str | None = None) -> list:
        """Tier-ordered providers, optionally with a soft alias preference.

        When ``model`` matches a provider's ``aliases`` or its exact
        ``model_id``, those providers move to the front of the attempt
        order (still tier-sorted among themselves). Everything else keeps
        its position, so an alias is a routing hint - never a hard
        constraint - and the failover guarantees stay intact.
        """
        if not model:
            return self.providers
        preferred = [
            p for p in self.providers
            if model == p.get("model_id") or model in (p.get("aliases") or [])
        ]
        if not preferred:
            return self.providers
        rest = [p for p in self.providers if p not in preferred]
        return preferred + rest

    def _request_url(self, provider: dict) -> str:
        """Chat-completions URL for a provider (``chat_path`` override)."""
        return f"{provider['base_url']}{provider.get('chat_path', '/chat/completions')}"

    def _request_headers(self, provider: dict, api_key: str) -> dict:
        """Auth + content headers for a provider attempt.

        Default is ``Authorization: Bearer``. ``auth_type: query`` puts the
        key in a query parameter instead (``auth_param``, default ``key``) -
        for providers that do not accept a header. Note: a key in the URL is
        visible to any proxy on the request path; never logged by this
        router (``_log_attempt`` emits sizes/status only).
        """
        if provider.get("auth_type") == "query":
            return {"Content-Type": "application/json"}
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def _request_params(self, provider: dict, api_key: str) -> dict | None:
        if provider.get("auth_type") == "query":
            return {provider.get("auth_param", "key"): api_key}
        return None

    async def route_request(
        self,
        messages: list,
        tools: list | None = None,
        tool_choice=None,
        model: str | None = None,
    ) -> dict:
        """Non-streaming chat completion through the provider tiers.

        Shares the single failover policy loop (:meth:`_iter_attempts`) with
        :meth:`stream_open`; this wrapper differs only in how a successful
        attempt is consumed. Returns the first successful provider's parsed
        JSON body. Raises :class:`UpstreamClientError` for non-failover
        client errors and :class:`AllProvidersFailedError` when every
        provider failed, was disabled, or is in cooldown.
        """
        async for parsed in self._iter_attempts(
            messages, tools, tool_choice, model, stream=False
        ):
            return parsed
        # Unreachable: _iter_attempts terminates by raising instead.
        raise _all_providers_failed()

    async def stream_open(
        self,
        messages: list,
        tools: list | None = None,
        tool_choice=None,
        model: str | None = None,
    ) -> tuple[dict | None, AsyncIterator[dict]]:
        """Open a streaming chat-completions response through the providers.

        Consumes the same single failover policy loop as
        :meth:`route_request` (:meth:`_iter_attempts`: tier order, soft
        alias preference, cooldowns, missing keys, 429/402/404/408/413/5xx
        and opt-in 400 failover, 401/403 disabling) and differs only in
        transport. Returns once a provider's stream is live:
        ``(first_chunk, tail)``. ``first_chunk`` is ``None`` for a clean but
        empty stream. Connection-stage failures - including a failure while
        fetching the first chunk - fail over to the next provider; a
        mid-stream error after the first chunk propagates to the caller so
        it can terminate the response cleanly.
        """
        async for outcome in self._iter_attempts(
            messages, tools, tool_choice, model, stream=True
        ):
            first, tail = outcome
            return first, tail
        # Unreachable: _iter_attempts terminates by raising instead.
        raise _all_providers_failed()

    async def _iter_attempts(
        self,
        messages: list,
        tools: list | None,
        tool_choice,
        model: str | None,
        stream: bool,
    ):
        """The single failover policy loop behind both public entry points
        (Phase 13): provider ordering, cooldown and missing-key skips,
        compress-then-trim payload preparation, the shared status
        classification, health tracking, and logging exist exactly once
        here. Transport mechanics live in :meth:`_attempt_nonstreaming` and
        :meth:`_attempt_streaming`.

        Yields one successful attempt per transport mode: the parsed JSON
        body when ``stream=False``, ``(first_chunk, tail)`` when
        ``stream=True``. Exhaustion raises
        :class:`AllProvidersFailedError`.
        """
        for provider in self._ordered_providers(model):
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

            headers = self._request_headers(provider, api_key)
            # Compress before trimming so the budget check runs on
            # post-compression sizes (otherwise trimming over-drops).
            # Send-time only: the caller's `messages` stay verbatim for
            # session persistence.
            send_messages = (
                compress_messages(messages) if compression_enabled() else messages
            )
            trimmed_messages = trim_messages(
                send_messages, provider.get("max_context", DEFAULT_MAX_CONTEXT)
            )
            payload = {
                "model": provider["model_id"],
                "messages": trimmed_messages,
            }
            if stream:
                payload["stream"] = True
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
                if stream:
                    yield await self._attempt_streaming(
                        provider,
                        api_key,
                        headers,
                        payload,
                        payload_bytes,
                        estimated_tokens,
                        attempt_started,
                    )
                else:
                    yield await self._attempt_nonstreaming(
                        provider,
                        api_key,
                        headers,
                        payload,
                        payload_bytes,
                        estimated_tokens,
                        attempt_started,
                    )
            except _Failover:
                continue

        raise _all_providers_failed()

    async def _attempt_nonstreaming(
        self,
        provider: dict,
        api_key: str,
        headers: dict,
        payload: dict,
        payload_bytes: int,
        estimated_tokens: int,
        attempt_started: float,
    ) -> dict:
        """Non-streaming transport: POST, classify, parse.

        Owns the non-streaming side of the 401/403 asymmetry: those statuses
        surface as ``httpx.HTTPStatusError`` from ``raise_for_status`` and
        are handled there; any other hard client error is re-raised as
        :class:`UpstreamClientError`.
        """
        name = provider["name"]
        try:
            resp = await self.client.post(
                self._request_url(provider),
                headers=headers,
                params=self._request_params(provider, api_key),
                json=payload,
                timeout=resolve_timeout(provider),
            )

            if _status_wants_failover(provider, resp.status_code):
                await self._handle_failover_status(
                    provider, name, resp, payload_bytes, estimated_tokens
                )
                raise _Failover

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
                raise _Failover from None
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
                raise _Failover from None
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
            raise UpstreamClientError(
                status_code=status, body=_parse_json_or_raw(body)
            ) from e

        except httpx.RequestError as e:
            self._handle_network_error(
                provider, name, payload_bytes, estimated_tokens, attempt_started, e
            )
            raise _Failover from None

    async def _attempt_streaming(
        self,
        provider: dict,
        api_key: str,
        headers: dict,
        payload: dict,
        payload_bytes: int,
        estimated_tokens: int,
        attempt_started: float,
    ) -> tuple[dict | None, AsyncIterator[dict]]:
        """Streaming transport: build/send, classify pre-read, open the SSE
        iterator, consume the first chunk.

        Owns the streaming side of the 401/403 asymmetry (checked directly
        against ``resp.status_code`` before anything is consumed). A failure
        while fetching the first chunk still fails over; errors after that
        propagate - no mid-stream provider switching.
        """
        name = provider["name"]
        try:
            request = self.client.build_request(
                "POST",
                self._request_url(provider),
                headers=headers,
                params=self._request_params(provider, api_key),
                json=payload,
                timeout=resolve_timeout(provider),
            )
            resp = await self.client.send(request, stream=True)

            if _status_wants_failover(provider, resp.status_code):
                await self._handle_failover_status(
                    provider, name, resp, payload_bytes, estimated_tokens
                )
                raise _Failover

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
                raise _Failover

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
                raise UpstreamClientError(
                    status_code=resp.status_code, body=_parse_json_or_raw(body)
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
            raise _Failover from None

        except httpx.RequestError as e:
            self._handle_network_error(
                provider, name, payload_bytes, estimated_tokens, attempt_started, e
            )
            raise _Failover from None

    async def _handle_failover_status(
        self,
        provider: dict,
        name: str,
        resp: httpx.Response,
        payload_bytes: int,
        estimated_tokens: int,
    ) -> None:
        """Shared failover-status action: warn-log the attempt, record the
        failure, release the response. The caller then skips providers."""
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

    def _handle_network_error(
        self,
        provider: dict,
        name: str,
        payload_bytes: int,
        estimated_tokens: int,
        attempt_started: float,
        exc: httpx.RequestError,
    ) -> None:
        """Shared network-error action: structured error log plus the
        ERROR-level attempt line (elapsed and read-timeout fields), then
        record the failure. The caller then skips providers."""
        details = _network_error_details(exc)
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

    async def close(self):
        await self.client.aclose()
