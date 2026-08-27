# invincible/core/router.py
import json
import logging
import time
import uuid
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
from invincible.core.run_store import new_run_entry
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


NO_CREDENTIALS_MESSAGE = (
    "No AI provider is connected for this account. Connect one at "
    "/dashboard/providers - chat requests route only through providers "
    "you connect yourself, never the operator's shared pool."
)


class NoCredentialsConfiguredError(AllProvidersFailedError):
    """A BYOK principal made a chat request with ZERO connected
    credentials. Expected/normal for a user who has not connected a
    provider yet - endpoints map this to a clear 400-class response, not
    a 5xx alarm. Subclassing AllProvidersFailedError keeps call sites
    that do not know about BYOK on the safe exhaustion path."""

    def __init__(self):
        super().__init__(NO_CREDENTIALS_MESSAGE)

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


def _dump_debug_payload(name: str, status: int, payload: dict) -> None:
    """Opt-in debug aid: dump the exact outgoing payload for a 400 so it can
    be replayed directly against the provider outside the gateway.

    Hardened after the first iteration clobbered its own evidence (and got
    committed) because every 400 - including mocked ones from the test
    suite - wrote the same fixed filename:

    - fires ONLY when INVINCIBLE_DEBUG_400 is set (1/true/on)
    - one file PER EVENT: debug_400_<provider>_<epoch>.json, so repeated
      failures build an evidence trail instead of overwriting
    - payloads contain conversation content; the glob is gitignored and
      files are local-machine only. Best-effort: never raises.
    """
    import pathlib as _pathlib

    if not settings.debug_dump_400():
        return
    try:
        out = _pathlib.Path(
            f"debug_400_{name}_{int(time.time())}.json"
        )
        out.write_text(
            json.dumps(
                {"provider": name, "status": status, "payload": payload},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.warning("Dumped failing payload to %s", out.resolve())
    except Exception as e:
        logger.warning("Failed to dump debug payload: %s", e)

def _log_upstream_error_body(name: str, status: int, parsed_body: dict) -> None:
    """Log the upstream provider's own error body (error visibility fix).

    ``UpstreamClientError.body`` was previously only ever handed to the
    caller (which discards it behind a generic client-facing message) and
    never written to the log, so a 400 from a provider was indistinguishable
    from any other 400 in the console. This is the single place that logs
    it: truncated, and only the parsed/raw error body - never the outgoing
    payload, headers, or keys.
    """
    try:
        rendered = json.dumps(parsed_body, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = str(parsed_body)
    logger.warning(
        "Upstream client error from %s (status=%s): %s",
        name,
        status,
        rendered[:500],
    )

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
    never escapes the Router. Carries the failing attempt's identity so
    post-failover actions (reactive checkpoints) can describe what broke."""

    def __init__(self, provider_name: str | None = None,
                 error_class: str | None = None):
        self.provider_name = provider_name
        self.error_class = error_class
        super().__init__(f"failover from {provider_name} ({error_class})")


def _extract_usage(body: dict) -> tuple[int | None, int | None]:
    """Pull ``(input_tokens, output_tokens)`` from an upstream success body
    when it carries real usage counts. Accepts both OpenAI
    (prompt_tokens/completion_tokens) and Anthropic-style
    (input_tokens/output_tokens) key names; anything non-integer is None.
    """
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return None, None

    def _int(*keys: str) -> int | None:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        return None

    return _int("prompt_tokens", "input_tokens"), _int(
        "completion_tokens", "output_tokens"
    )


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


def _health_key(provider: dict) -> str:
    """Cooldown/isolation key for the health tracker. BYOK candidates
    carry a per-credential ``health_id`` so same-labeled providers of
    different users never share cooldown state (and can never poison the
    operator pool's); operator providers keep their name."""
    return provider.get("health_id") or provider["name"]


class Router:
    def __init__(
        self,
        config_path: str | None = None,
        transport=None,
        registry: "ProviderRegistry | None" = None,
        run_recorder=None,
    ):
        # Registry mode (Phase 13.5): the registry owns provider state and
        # the routing mode; a snapshot is taken per request. Legacy mode
        # (tests, direct construction) keeps loading static YAML exactly as
        # before and runs in auto mode.
        self.registry = registry
        # Optional async callable receiving one run-entry dict per upstream
        # attempt (success, failover, or error). None = no recording.
        self.run_recorder = run_recorder
        # Optional async callable invoked ONCE per request when a provider
        # fails over (Phase 4 reactive checkpoints). Receives keyword args:
        # request_id, session_id, session_pk, failed_provider, error_class.
        # Wired by the lifespan to the ContinuityEngine; never imported here.
        self.failover_hook = None
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

    async def _resolve_attempt_key(
        self, provider: dict, byok_key_resolver
    ) -> str | None:
        """One attempt's API key, resolved lazily. Registry-mode providers
        resolve their env var by name; BYOK providers go through the
        injected resolver (decrypt on demand) - a None result skips the
        attempt exactly like a missing env key."""
        if byok_key_resolver is None:
            return settings.provider_api_key(provider["api_key_env"])
        return await byok_key_resolver(provider)

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
        *,
        session_id: str | None = None,
        session_pk: int | None = None,
        byok_candidates: list[dict] | None = None,
        byok_key_resolver=None,
    ) -> dict:
        """Non-streaming chat completion through the provider tiers.

        Shares the single failover policy loop (:meth:`_iter_attempts`) with
        :meth:`stream_open`; this wrapper differs only in how a successful
        attempt is consumed. Returns the first successful provider's parsed
        JSON body. Raises :class:`UpstreamClientError` for non-failover
        client errors and :class:`AllProvidersFailedError` when every
        provider failed, was disabled, or is in cooldown.

        BYOK (Platform Phase 9): when ``byok_candidates`` is not None, the
        attempt list comes ENTIRELY from that list (the caller's per-user
        credential rows) and ``byok_key_resolver(provider)`` resolves each
        attempt's key lazily - never the operator registry.
        """
        result, _info = await self.route_request_detailed(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            model=model,
            session_id=session_id,
            session_pk=session_pk,
            byok_candidates=byok_candidates,
            byok_key_resolver=byok_key_resolver,
        )
        return result

    async def route_request_detailed(
        self,
        messages: list,
        tools: list | None = None,
        tool_choice=None,
        model: str | None = None,
        *,
        session_id: str | None = None,
        session_pk: int | None = None,
        byok_candidates: list[dict] | None = None,
        byok_key_resolver=None,
    ) -> tuple[dict, dict]:
        """Like :meth:`route_request` but also returns route metadata:
        ``(parsed_body, {request_id, provider_name, model_id, attempts})``.
        """
        async for result, info in self._iter_attempts(
            messages, tools, tool_choice, model, stream=False,
            session_id=session_id, session_pk=session_pk,
            byok_candidates=byok_candidates,
            byok_key_resolver=byok_key_resolver,
        ):
            return result, info
        # Unreachable: _iter_attempts terminates by raising instead.
        raise _all_providers_failed()

    async def stream_open(
        self,
        messages: list,
        tools: list | None = None,
        tool_choice=None,
        model: str | None = None,
        *,
        session_id: str | None = None,
        session_pk: int | None = None,
        byok_candidates: list[dict] | None = None,
        byok_key_resolver=None,
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
        result, _info = await self.stream_open_detailed(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            model=model,
            session_id=session_id,
            session_pk=session_pk,
            byok_candidates=byok_candidates,
            byok_key_resolver=byok_key_resolver,
        )
        return result

    async def stream_open_detailed(
        self,
        messages: list,
        tools: list | None = None,
        tool_choice=None,
        model: str | None = None,
        *,
        session_id: str | None = None,
        session_pk: int | None = None,
        byok_candidates: list[dict] | None = None,
        byok_key_resolver=None,
    ) -> tuple[tuple[dict | None, AsyncIterator[dict]], dict]:
        """Like :meth:`stream_open` but also returns route metadata:
        ``((first_chunk, tail), {request_id, provider_name, model_id,
        attempts})``.
        """
        async for result, info in self._iter_attempts(
            messages, tools, tool_choice, model, stream=True,
            session_id=session_id, session_pk=session_pk,
            byok_candidates=byok_candidates,
            byok_key_resolver=byok_key_resolver,
        ):
            return result, info
        # Unreachable: _iter_attempts terminates by raising instead.
        raise _all_providers_failed()

    async def _iter_attempts(
        self,
        messages: list,
        tools: list | None,
        tool_choice,
        model: str | None,
        stream: bool,
        session_id: str | None = None,
        session_pk: int | None = None,
        byok_candidates: list[dict] | None = None,
        byok_key_resolver=None,
    ) -> AsyncIterator[tuple[dict | tuple[dict | None, AsyncIterator[dict]], dict]]:
        """The single failover policy loop behind both public entry points
        (Phase 13): provider ordering, cooldown and missing-key skips,
        compress-then-trim payload preparation, the shared status
        classification, health tracking, and logging exist exactly once
        here. Transport mechanics live in :meth:`_attempt_nonstreaming` and
        :meth:`_attempt_streaming`.

        BYOK (Platform Phase 9): ``byok_candidates is not None`` switches
        the candidate source to that list - the requesting user's own
        connected credentials, never the operator registry, with no
        fallback in either direction. ``byok_key_resolver(provider)`` must
        return the attempt's API key or None (skip); it is awaited once per
        attempt so decryption is lazy and per-credential. An empty BYOK
        list raises :class:`NoCredentialsConfiguredError` - connecting a
        provider is expected of the user, not an operator emergency.

        Yields ``(result, route_info)`` where ``result`` is the parsed JSON
        body (``stream=False``) or ``(first_chunk, tail)`` (``stream=True``)
        and ``route_info`` describes the winning attempt (request_id,
        provider_name, model_id, attempts). Exhaustion raises
        :class:`AllProvidersFailedError`.
        """
        request_id = uuid.uuid4().hex
        attempt_index = 0
        checkpoint_fired = False
        if byok_candidates is not None:
            if not byok_candidates:
                raise NoCredentialsConfiguredError()
            try:
                candidates = attempt_order(
                    byok_candidates, self.health_tracker, AUTO_ROUTING, model)
            except PinnedUnavailableError as e:
                raise AllProvidersFailedError(str(e)) from None
        else:
            candidates = self._candidates(model)
        for provider in candidates:
            name = provider["name"]

            if not self.health_tracker.is_available(_health_key(provider)):
                logger.info(f"Provider {name} in cooldown. Skipping.")
                continue

            api_key = await self._resolve_attempt_key(
                provider, byok_key_resolver)
            if not api_key:
                if byok_key_resolver is not None:
                    logger.warning(
                        f"Unusable credential for {name}. Skipping.")
                else:
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

            attempt_index += 1
            attempt_started = time.monotonic()
            try:
                if stream:
                    first, tail = await self._attempt_streaming(
                        provider,
                        api_key,
                        headers,
                        payload,
                        payload_bytes,
                        estimated_tokens,
                        attempt_started,
                        request_id=request_id,
                        session_pk=session_pk,
                        session_id=session_id,
                        attempt_index=attempt_index,
                    )
                    yield (first, tail), {
                        "request_id": request_id,
                        "provider_name": name,
                        "model_id": provider["model_id"],
                        "attempts": attempt_index,
                    }
                else:
                    parsed = await self._attempt_nonstreaming(
                        provider,
                        api_key,
                        headers,
                        payload,
                        payload_bytes,
                        estimated_tokens,
                        attempt_started,
                        request_id=request_id,
                        session_pk=session_pk,
                        session_id=session_id,
                        attempt_index=attempt_index,
                    )
                    yield parsed, {
                        "request_id": request_id,
                        "provider_name": name,
                        "model_id": provider["model_id"],
                        "attempts": attempt_index,
                    }
            except _Failover as f:
                # Reactive failover checkpoint (Phase 4): snapshot task
                # state BEFORE the next provider attempt, once per request
                # - the "why did work move" story stays recoverable. The
                # hook is injected (never imported) and best-effort.
                if (
                    self.failover_hook is not None
                    and not checkpoint_fired
                    and (session_pk is not None or session_id is not None)
                ):
                    checkpoint_fired = True
                    try:
                        await self.failover_hook(
                            request_id=request_id,
                            session_id=session_id,
                            session_pk=session_pk,
                            failed_provider=f.provider_name,
                            error_class=f.error_class,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Reactive failover checkpoint failed: %s", exc
                        )
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
        request_id: str = "",
        session_id: str | None = None,
        session_pk: int | None = None,
        attempt_index: int = 0,
    ) -> dict:
        """Non-streaming transport: POST, classify, parse.

        Owns the non-streaming side of the 401/403 asymmetry: those statuses
        surface as ``httpx.HTTPStatusError`` from ``raise_for_status`` and
        are handled there; any other hard client error is re-raised as
        :class:`UpstreamClientError`. Every terminal outcome records a run
        row via the injected recorder (best-effort).
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
                await self._record_run(
                    provider, attempt_index, time.time(), "failover",
                    error_class=str(resp.status_code),
                    request_id=request_id, session_id=session_id,
                    session_pk=session_pk,
                    started_at=attempt_started,
                )
                raise _Failover(
                    provider_name=name,
                    error_class=str(resp.status_code),
                )

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
                self.health_tracker.record_failure(_health_key(provider))
                await resp.aclose()
                await self._record_run(
                    provider, attempt_index, time.time(), "failover",
                    error_class="malformed_json",
                    request_id=request_id, session_id=session_id,
                    session_pk=session_pk,
                    started_at=attempt_started,
                )
                raise _Failover(
                    provider_name=name, error_class="malformed_json"
                ) from None
            self.health_tracker.record_success(_health_key(provider))
            _log_attempt(
                name,
                provider["model_id"],
                payload_bytes,
                estimated_tokens,
                resp.status_code,
                False,
            )
            # Phase 4 usage accounting: real upstream counts when present,
            # otherwise a flagged chars/4 estimate of the response body.
            input_tokens, output_tokens = _extract_usage(parsed)
            estimated = output_tokens is None
            if estimated:
                output_tokens = len(body) // 4
            await self._record_run(
                provider, attempt_index, time.time(), "ok",
                request_id=request_id, session_id=session_id,
                    session_pk=session_pk,
                started_at=attempt_started,
                input_tokens=input_tokens, output_tokens=output_tokens,
                usage_estimated=estimated,
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
                self.health_tracker.disable(_health_key(provider))
                await e.response.aclose()
                await self._record_run(
                    provider, attempt_index, time.time(), "disabled",
                    error_class=str(status),
                    request_id=request_id, session_id=session_id,
                    session_pk=session_pk,
                    started_at=attempt_started,
                )
                raise _Failover(
                    provider_name=name, error_class=str(status)
                ) from None
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
            parsed_body = _parse_json_or_raw(body)
            _log_upstream_error_body(name, status, parsed_body)
            if status == 400:
                _dump_debug_payload(name, status, payload)
            await self._record_run(
                provider, attempt_index, time.time(), "error",
                error_class=str(status),
                request_id=request_id, session_id=session_id,
                    session_pk=session_pk,
                started_at=attempt_started,
            )
            raise UpstreamClientError(
                status_code=status, body=parsed_body
            ) from e

        except httpx.RequestError as e:
            self._handle_network_error(
                provider, name, payload_bytes, estimated_tokens, attempt_started, e
            )
            details = _network_error_details(e)
            await self._record_run(
                provider, attempt_index, time.time(), "failover",
                error_class=details["error_kind"],
                request_id=request_id, session_id=session_id,
                    session_pk=session_pk,
                started_at=attempt_started,
            )
            raise _Failover(
                provider_name=name, error_class=details["error_kind"]
            ) from None

    async def _attempt_streaming(
        self,
        provider: dict,
        api_key: str,
        headers: dict,
        payload: dict,
        payload_bytes: int,
        estimated_tokens: int,
        attempt_started: float,
        request_id: str = "",
        session_id: str | None = None,
        session_pk: int | None = None,
        attempt_index: int = 0,
    ) -> tuple[dict | None, AsyncIterator[dict]]:
        """Streaming transport: build/send, classify pre-read, open the SSE
        iterator, consume the first chunk.

        Owns the streaming side of the 401/403 asymmetry (checked directly
        against ``resp.status_code`` before anything is consumed). A failure
        while fetching the first chunk still fails over; errors after that
        propagate - no mid-stream provider switching. Every terminal outcome
        records a run row via the injected recorder (best-effort).
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
                await self._record_run(
                    provider, attempt_index, time.time(), "failover",
                    error_class=str(resp.status_code),
                    request_id=request_id, session_id=session_id,
                    session_pk=session_pk,
                    started_at=attempt_started,
                )
                raise _Failover(
                    provider_name=name,
                    error_class=str(resp.status_code),
                )

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
                self.health_tracker.disable(_health_key(provider))
                await resp.aclose()
                await self._record_run(
                    provider, attempt_index, time.time(), "disabled",
                    error_class=str(resp.status_code),
                    request_id=request_id, session_id=session_id,
                    session_pk=session_pk,
                    started_at=attempt_started,
                )
                raise _Failover(
                    provider_name=name, error_class=str(resp.status_code)
                )

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
                parsed_body = _parse_json_or_raw(body)
                _log_upstream_error_body(name, resp.status_code, parsed_body)
                if resp.status_code == 400:
                    _dump_debug_payload(name, resp.status_code, payload)
                await self._record_run(
                    provider, attempt_index, time.time(), "error",
                    error_class=str(resp.status_code),
                    request_id=request_id, session_id=session_id,
                    session_pk=session_pk,
                    started_at=attempt_started,
                )
                raise UpstreamClientError(
                    status_code=resp.status_code, body=parsed_body
                )

            tail = _iter_stream(resp)
            try:
                first = await anext(tail)
            except StopAsyncIteration:
                first = None
            self.health_tracker.record_success(_health_key(provider))
            _log_attempt(
                name,
                provider["model_id"],
                payload_bytes,
                estimated_tokens,
                resp.status_code,
                False,
            )
            await self._record_run(
                provider, attempt_index, time.time(), "ok",
                request_id=request_id, session_id=session_id,
                    session_pk=session_pk,
                started_at=attempt_started,
                # Streaming never sees real upstream usage without a wire
                # change (stream_options.include_usage): record the input
                # estimate now; the endpoint attaches the output estimate
                # via RunStore.attach_output once accumulation finishes.
                input_tokens=estimated_tokens,
                usage_estimated=True,
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
            self.health_tracker.record_failure(_health_key(provider))
            await self._record_run(
                provider, attempt_index, time.time(), "failover",
                error_class="malformed_sse",
                request_id=request_id, session_id=session_id,
                    session_pk=session_pk,
                started_at=attempt_started,
            )
            raise _Failover(
                provider_name=name, error_class="malformed_sse"
            ) from None

        except httpx.RequestError as e:
            self._handle_network_error(
                provider, name, payload_bytes, estimated_tokens, attempt_started, e
            )
            details = _network_error_details(e)
            await self._record_run(
                provider, attempt_index, time.time(), "failover",
                error_class=details["error_kind"],
                request_id=request_id, session_id=session_id,
                    session_pk=session_pk,
                started_at=attempt_started,
            )
            raise _Failover(
                provider_name=name, error_class=details["error_kind"]
            ) from None

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
        self.health_tracker.record_failure(_health_key(provider))
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
        self.health_tracker.record_failure(_health_key(provider))

    async def _record_run(
        self,
        provider: dict,
        attempt_index: int,
        finished_at: float,
        outcome: str,
        error_class: str | None = None,
        request_id: str = "",
        session_id: str | None = None,
        session_pk: int | None = None,
        started_at: float | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        usage_estimated: bool = False,
    ) -> None:
        """Best-effort provider-run recording via the injected recorder.

        Never raises into the attempt path: persistence problems are logged
        and swallowed so a run-record write can never fail a completion.
        ``session_pk`` (Phase 2) scopes the row to the owning surrogate
        session so graph/brief reads can predicate on it. Token counts
        (Phase 4) land on success rows; ``usage_estimated`` flags counts
        derived from payload/response sizes instead of upstream reporting.
        """
        if self.run_recorder is None:
            return
        wall_started = (
            time.time() - (time.monotonic() - started_at)
            if started_at is not None
            else finished_at
        )
        meta = (
            {"usage_estimated": True} if usage_estimated else None
        )
        try:
            await self.run_recorder(
                new_run_entry(
                    request_id=request_id,
                    session_id=session_id,
                    session_pk=session_pk,
                    provider_name=provider["name"],
                    model_id=provider["model_id"],
                    attempt_index=attempt_index,
                    started_at=wall_started,
                    outcome=outcome,
                    error_class=error_class,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    meta=meta,
                )
            )
        except Exception as e:
            logger.warning("Failed to persist provider run record: %s", e)

    async def close(self) -> None:
        await self.client.aclose()
