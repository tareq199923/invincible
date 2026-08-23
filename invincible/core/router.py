# invincible/core/router.py
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import httpx

from invincible.core.compression import compress_messages, compression_enabled

# Re-exported for backward compatibility: tests and older call sites import
# the trimming helpers and provider-config helpers from the Router module.
from invincible.core.config import (  # noqa: F401 - re-exports
    DEFAULT_TIMEOUT_CONFIG,
    auth_headers,
    auth_params,
    load_providers_config,
    resolve_timeout,
    validate_providers_config,
)
from invincible.core.provider_health import HealthTracker
from invincible.core.selection import (
    AUTO_ROUTING,
    PinnedUnavailableError,
    attempt_order,
    routing_from_config,
)
from invincible.core.settings import settings
from invincible.core.trimming import (  # noqa: F401 - re-exports
    DEFAULT_MAX_CONTEXT,
    RESERVE_TOKENS,
    estimate_tokens,
    group_into_turns,
    trim_messages,
)

logger = logging.getLogger("invincible.router")

if TYPE_CHECKING:
    from invincible.core.provider_registry import ProviderRegistry

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
    def __init__(
        self,
        config_path: str | None = None,
        transport=None,
        registry: "ProviderRegistry | None" = None,
    ):
        # Registry mode (Phase 13.5): the registry owns provider state and
        # the routing mode; a snapshot is taken per request. Legacy mode
        # (tests, direct construction) keeps loading static YAML exactly as
        # before and runs in auto mode.
        self.registry = registry
        if registry is not None:
            self.providers = []
        else:
            config = load_providers_config(config_path)
            self.providers = config.get("providers", [])
            # Shape is validated in load_providers_config (Phase 6); sort by
            # tier here so failover order never depends on YAML order.
            self.providers.sort(key=lambda p: p["tier"])
            for provider in self.providers:
                if not settings.provider_api_key(provider["api_key_env"]):
                    logger.warning(
                        f"Provider '{provider['name']}' has no API key set via "
                        f"{provider['api_key_env']}. It will be unavailable."
                    )
        self.health_tracker = HealthTracker()
        self.client = httpx.AsyncClient(transport=transport)

    def _candidates(self, model: str | None = None) -> list[dict]:
        """Per-request ordered candidate providers.

        Delegates to the selection layer with a fresh snapshot, so routing
        mode changes and registry mutations apply to the next request but
        never to an in-flight one. Pinned misconfiguration surfaces as the
        normal gateway exhaustion error.
        """
        if self.registry is not None:
            snapshot = self.registry.list()
            routing = routing_from_config(self.registry.routing())
        else:
            snapshot = self.providers
            routing = AUTO_ROUTING
        try:
            return attempt_order(snapshot, self.health_tracker, routing, model)
        except PinnedUnavailableError as e:
            raise AllProvidersFailedError(str(e)) from None

    def _request_url(self, provider: dict) -> str:
        """Chat-completions URL for a provider (``chat_path`` override)."""
        return f"{provider['base_url']}{provider.get('chat_path', '/chat/completions')}"

    def _request_headers(self, provider: dict, api_key: str) -> dict:
        """Auth + content headers for a provider attempt (see
        ``core.config.auth_headers``; a key in the URL is visible to any
        proxy on the request path and never logged)."""
        return auth_headers(provider, api_key)

    def _request_params(self, provider: dict, api_key: str) -> dict | None:
        return auth_params(provider, api_key)

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
    ) -> AsyncIterator[dict | tuple[dict | None, AsyncIterator[dict]]]:
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
        for provider in self._candidates(model):
            name = provider["name"]

            if not self.health_tracker.is_available(name):
                logger.info(f"Provider {name} in cooldown. Skipping.")
                continue

            api_key = settings.provider_api_key(provider["api_key_env"])
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

    async def close(self) -> None:
        await self.client.aclose()
