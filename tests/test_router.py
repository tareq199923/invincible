import json
import logging

import httpx
import pytest

from invincible.core.router import (
    DEFAULT_TIMEOUT_CONFIG,
    Router,
    UpstreamClientError,
)
from tests.conftest import default_providers, provider_body, sse_body, stream_chunk

MESSAGES = [{"role": "user", "content": "hi"}]

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "execute_bash",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }
]


class _TrackingResponse(httpx.Response):
    """httpx.Response subclass that records whether aclose() was awaited."""

    def __init__(self, *args, **kwargs):
        self.aclosed = False
        super().__init__(*args, **kwargs)

    async def aclose(self):
        self.aclosed = True
        await super().aclose()


async def test_success_returns_lowest_tier_provider(make_router):
    alpha_body = provider_body("alpha")
    router = make_router(
        handlers={
            "alpha.example.com": httpx.Response(200, json=alpha_body),
            "beta.example.com": httpx.Response(200, json=provider_body("beta")),
            "gamma.example.com": httpx.Response(200, json=provider_body("gamma")),
        }
    )
    result = await router.route_request(MESSAGES)
    assert result == alpha_body
    assert router.health_tracker.get("alpha").consecutive_failures == 0


async def test_tools_and_tool_choice_forwarded(make_router):
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha"))

    router = make_router(handlers={"alpha.example.com": alpha_handler})
    await router.route_request(MESSAGES, tools=TOOLS, tool_choice="auto")
    payload = captured[0]
    assert payload["tools"] == TOOLS
    assert payload["tool_choice"] == "auto"


async def test_tools_omitted_when_not_requested(make_router):
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha"))

    router = make_router(handlers={"alpha.example.com": alpha_handler})
    await router.route_request(MESSAGES)
    payload = captured[0]
    assert "tools" not in payload
    assert "tool_choice" not in payload


async def test_stream_open_forwards_tools(make_router):
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(
            200,
            content=sse_body(stream_chunk("alpha", {"content": "hi"})),
        )

    router = make_router(handlers={"alpha.example.com": alpha_handler})
    first, tail = await router.stream_open(MESSAGES, tools=TOOLS, tool_choice="auto")
    assert first is not None
    payload = captured[0]
    assert payload["tools"] == TOOLS
    assert payload["tool_choice"] == "auto"
    assert payload["stream"] is True


async def test_providers_sorted_by_tier(make_router):
    providers = list(reversed(default_providers()))
    alpha_body = provider_body("alpha")
    router = make_router(
        providers=providers,
        handlers={"alpha.example.com": httpx.Response(200, json=alpha_body)},
    )
    assert [p["name"] for p in router.providers] == ["alpha", "beta", "gamma"]
    result = await router.route_request(MESSAGES)
    assert result == alpha_body


async def test_failover_on_429(make_router):
    calls = []

    def alpha_handler(request):
        calls.append("alpha")
        return httpx.Response(429)

    def beta_handler(request):
        calls.append("beta")
        return httpx.Response(200, json=provider_body("beta"))

    router = make_router(
        handlers={"alpha.example.com": alpha_handler, "beta.example.com": beta_handler}
    )
    result = await router.route_request(MESSAGES)
    assert calls == ["alpha", "beta"]
    assert result == provider_body("beta")
    assert not router.health_tracker.is_available("alpha")


async def test_failover_on_5xx(make_router):
    router = make_router(
        handlers={
            "alpha.example.com": httpx.Response(503),
            "beta.example.com": httpx.Response(200, json=provider_body("beta")),
        }
    )
    result = await router.route_request(MESSAGES)
    assert result == provider_body("beta")


async def test_non_json_200_fails_over_to_next_provider(make_router):
    """A 200 with a non-JSON body must not crash routing: it counts as an
    upstream failure and fails over to the next provider like any other."""
    responses = []

    def alpha_handler(request):
        resp = _TrackingResponse(200, content="<html>not json</html>")
        responses.append(resp)
        return resp

    def beta_handler(request):
        return httpx.Response(200, json=provider_body("beta"))

    router = make_router(
        handlers={"alpha.example.com": alpha_handler, "beta.example.com": beta_handler}
    )
    result = await router.route_request(MESSAGES)
    assert result == provider_body("beta")
    assert not router.health_tracker.is_available("alpha")
    assert responses[0].aclosed


@pytest.mark.parametrize("status", [402, 404, 408, 413])
async def test_failover_on_limit_and_transient_statuses(make_router, status):
    calls = []

    def alpha_handler(request):
        calls.append("alpha")
        return httpx.Response(status)

    def beta_handler(request):
        calls.append("beta")
        return httpx.Response(200, json=provider_body("beta"))

    router = make_router(
        handlers={"alpha.example.com": alpha_handler, "beta.example.com": beta_handler}
    )
    result = await router.route_request(MESSAGES)
    assert calls == ["alpha", "beta"]
    assert result == provider_body("beta")
    assert not router.health_tracker.is_available("alpha")


async def test_failover_closes_unread_response_on_429(make_router):
    responses = []

    def alpha_handler(request):
        resp = _TrackingResponse(429)
        responses.append(resp)
        return resp

    def beta_handler(request):
        return httpx.Response(200, json=provider_body("beta"))

    router = make_router(
        handlers={"alpha.example.com": alpha_handler, "beta.example.com": beta_handler}
    )
    result = await router.route_request(MESSAGES)
    assert result == provider_body("beta")
    assert responses[0].aclosed


async def test_auth_failure_closes_unread_response(make_router):
    responses = []

    def alpha_handler(request):
        resp = _TrackingResponse(401)
        responses.append(resp)
        return resp

    def beta_handler(request):
        return httpx.Response(200, json=provider_body("beta"))

    router = make_router(
        handlers={"alpha.example.com": alpha_handler, "beta.example.com": beta_handler}
    )
    result = await router.route_request(MESSAGES)
    assert result == provider_body("beta")
    assert not router.health_tracker.is_available("alpha")
    assert responses[0].aclosed


async def test_failover_on_network_error(make_router):
    def alpha_handler(request):
        raise httpx.ConnectError("connection refused")

    router = make_router(
        handlers={
            "alpha.example.com": alpha_handler,
            "beta.example.com": httpx.Response(200, json=provider_body("beta")),
        }
    )
    result = await router.route_request(MESSAGES)
    assert result == provider_body("beta")
    assert not router.health_tracker.is_available("alpha")


async def test_skips_provider_in_cooldown(make_router):
    calls = []

    def alpha_handler(request):
        calls.append("alpha")
        return httpx.Response(200, json=provider_body("alpha"))

    router = make_router(
        handlers={
            "alpha.example.com": alpha_handler,
            "beta.example.com": httpx.Response(200, json=provider_body("beta")),
        }
    )
    router.health_tracker.record_failure("alpha")
    result = await router.route_request(MESSAGES)
    assert calls == []
    assert result == provider_body("beta")


async def test_skips_provider_with_missing_api_key(make_router):
    calls = []

    def alpha_handler(request):
        calls.append("alpha")
        return httpx.Response(200, json=provider_body("alpha"))

    router = make_router(
        handlers={
            "alpha.example.com": alpha_handler,
            "beta.example.com": httpx.Response(200, json=provider_body("beta")),
        },
        missing_keys=["ALPHA_API_KEY"],
    )
    result = await router.route_request(MESSAGES)
    assert calls == []
    assert result == provider_body("beta")


async def test_auth_failure_disables_provider(make_router):
    calls = []

    def alpha_handler(request):
        calls.append("alpha")
        return httpx.Response(401, json={"error": "unauthorized"})

    router = make_router(
        handlers={
            "alpha.example.com": alpha_handler,
            "beta.example.com": httpx.Response(200, json=provider_body("beta")),
        }
    )
    result = await router.route_request(MESSAGES)
    assert calls == ["alpha"]
    assert result == provider_body("beta")
    assert not router.health_tracker.is_available("alpha")

    result = await router.route_request(MESSAGES)
    assert calls == ["alpha"]
    assert result == provider_body("beta")


def _attempt_records(caplog, provider="alpha"):
    return [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith(f"provider={provider} ")
    ]


def tiered_providers():
    """The shipped tier order as mock providers: TokenRouter -> NIM -> Groq
    -> OpenRouter -> Gemini (last)."""
    return [
        {
            "name": "nim",
            "tier": 1,
            "base_url": "https://nim.example.com/v1",
            "api_key_env": "NIM_API_KEY",
            "model_id": "z-ai/glm-5.2",
        },
        {
            "name": "groq",
            "tier": 2,
            "base_url": "https://groq.example.com/v1",
            "api_key_env": "GROQ_API_KEY",
            "model_id": "gpt-oss-120b",
        },
        {
            "name": "openrouter",
            "tier": 3,
            "base_url": "https://openrouter.example.com/v1",
            "api_key_env": "OPENROUTER_API_KEY",
            "model_id": "nemotron-free",
        },
        {
            "name": "gemini",
            "tier": 4,
            "base_url": "https://gemini.example.com/v1",
            "api_key_env": "GEMINI_API_KEY",
            "model_id": "gemini-2.5-flash",
        },
    ]


async def test_provider_priority_is_nim_groq_openrouter_gemini(make_router):
    """With every provider healthy, the lowest tier (nim) wins and the rest
    are never contacted."""
    calls = []

    def handler(name):
        def _handler(request):
            calls.append(name)
            return httpx.Response(200, json=provider_body(name))

        return _handler

    router = make_router(
        providers=tiered_providers(),
        handlers={
            "nim.example.com": handler("nim"),
            "groq.example.com": handler("groq"),
            "openrouter.example.com": handler("openrouter"),
            "gemini.example.com": handler("gemini"),
        },
    )
    assert [p["name"] for p in router.providers] == [
        "nim", "groq", "openrouter", "gemini",
    ]
    result = await router.route_request(MESSAGES)
    assert result == provider_body("nim")
    assert calls == ["nim"]


async def test_gemini_reached_only_after_earlier_providers_fail(make_router):
    """Gemini is the last resort: it is tried only after nim, groq, and
    openrouter all fail, and a second request skips the failed ones due to
    their cooldowns."""
    calls = []

    def fail_with(status):
        def _handler(request):
            calls.append("called")
            return httpx.Response(status)

        return _handler

    router = make_router(
        providers=tiered_providers(),
        handlers={
            "nim.example.com": fail_with(429),
            "groq.example.com": fail_with(503),
            "openrouter.example.com": fail_with(429),
            "gemini.example.com": httpx.Response(
                200, json=provider_body("gemini")
            ),
        },
    )
    result = await router.route_request(MESSAGES)
    assert result == provider_body("gemini")
    assert calls.count("called") == 3
    assert not router.health_tracker.is_available("nim")
    assert not router.health_tracker.is_available("groq")
    assert not router.health_tracker.is_available("openrouter")
    assert router.health_tracker.is_available("gemini")

    calls.clear()
    result = await router.route_request(MESSAGES)
    assert result == provider_body("gemini")
    assert "called" not in calls
    assert router.health_tracker.get("gemini").consecutive_failures == 0


async def test_gemini_failure_still_failovers_but_raises_when_all_fail(
    make_router,
):
    """Failover chain reaches gemini; when even gemini is down, the request
    fails like today."""
    router = make_router(
        providers=tiered_providers(),
        handlers={
            "nim.example.com": httpx.Response(429),
            "groq.example.com": httpx.Response(429),
            "openrouter.example.com": httpx.Response(429),
            "gemini.example.com": httpx.Response(429),
        },
    )
    with pytest.raises(Exception, match="All providers failed"):
        await router.route_request(MESSAGES)
    assert not router.health_tracker.is_available("gemini")


async def test_attempt_logged_on_success(make_router, caplog):
    router = make_router(
        handlers={"alpha.example.com": httpx.Response(200, json=provider_body("alpha"))}
    )
    with caplog.at_level(logging.INFO, logger="invincible.router"):
        result = await router.route_request(MESSAGES)
    assert result == provider_body("alpha")
    lines = _attempt_records(caplog)
    assert len(lines) == 1
    msg = lines[0]
    assert "provider=alpha model=alpha-model" in msg
    assert "payload_bytes=" in msg
    assert "estimated_tokens=" in msg
    assert "status=200" in msg
    assert "failover=false" in msg


@pytest.mark.parametrize("status", [413, 429])
async def test_attempt_logged_on_failover(make_router, caplog, status):
    router = make_router(
        handlers={
            "alpha.example.com": httpx.Response(status),
            "beta.example.com": httpx.Response(200, json=provider_body("beta")),
        }
    )
    with caplog.at_level(logging.INFO, logger="invincible.router"):
        result = await router.route_request(MESSAGES)
    assert result == provider_body("beta")
    lines = _attempt_records(caplog)
    assert any(
        f"status={status}" in m and "failover=true" in m for m in lines
    )


async def test_attempt_logged_on_network_error(make_router, caplog):
    def alpha_handler(request):
        raise httpx.ConnectError("connection refused")

    router = make_router(handlers={"alpha.example.com": alpha_handler})
    with caplog.at_level(logging.INFO, logger="invincible.router"), pytest.raises(
        Exception, match="All providers failed"
    ):
        await router.route_request(MESSAGES)
    lines = _attempt_records(caplog)
    assert any("status=network_error" in m and "failover=true" in m for m in lines)


async def test_network_error_logs_exception_details(make_router, caplog):
    def alpha_handler(request):
        raise httpx.ReadTimeout("read timed out")

    router = make_router(
        handlers={
            "alpha.example.com": alpha_handler,
            "beta.example.com": httpx.Response(200, json=provider_body("beta")),
        }
    )
    with caplog.at_level(logging.INFO, logger="invincible.router"):
        result = await router.route_request(MESSAGES)
    assert result == provider_body("beta")
    line = next(
        m for m in _attempt_records(caplog)
        if "status=network_error" in m and "failover=true" in m
    )
    assert "error_type=ReadTimeout" in line
    assert "error_kind=read_timeout" in line
    assert "error_msg=read timed out" in line
    assert "elapsed_s=" in line
    assert "read_timeout_s=60.0" in line
    assert "payload_bytes=" in line
    assert "estimated_tokens=" in line


async def test_network_error_logs_empty_message_gracefully(make_router, caplog):
    """httpx streaming timeouts surface with an empty message; the log must
    say so instead of logging a blank field."""

    def alpha_handler(request):
        raise httpx.ReadTimeout("")

    router = make_router(
        handlers={
            "alpha.example.com": alpha_handler,
            "beta.example.com": httpx.Response(200, json=provider_body("beta")),
        }
    )
    with caplog.at_level(logging.INFO, logger="invincible.router"):
        result = await router.route_request(MESSAGES)
    assert result == provider_body("beta")
    line = next(
        m for m in _attempt_records(caplog)
        if "status=network_error" in m and "failover=true" in m
    )
    assert "error_type=ReadTimeout" in line
    assert "error_msg=no_message" in line


async def test_stream_open_network_error_logs_exception_details(
    make_router, caplog
):
    def alpha_handler(request):
        raise httpx.ReadTimeout("")

    router = make_router(
        handlers={
            "alpha.example.com": alpha_handler,
            "beta.example.com": httpx.Response(
                200,
                content=sse_body(stream_chunk("beta", {"role": "assistant"})),
            ),
        }
    )
    with caplog.at_level(logging.INFO, logger="invincible.router"):
        first, tail = await router.stream_open(MESSAGES)
    assert first["model"] == "beta-model"
    line = next(
        m for m in _attempt_records(caplog)
        if "status=network_error" in m and "failover=true" in m
    )
    assert "error_type=ReadTimeout" in line
    assert "error_kind=read_timeout" in line
    assert "error_msg=no_message" in line
    assert "elapsed_s=" in line
    assert "read_timeout_s=60.0" in line
    await router.close()


async def test_stream_attempt_logged_on_failover(make_router, caplog):
    router = make_router(
        handlers={
            "alpha.example.com": httpx.Response(413),
            "beta.example.com": httpx.Response(
                200,
                content=sse_body(stream_chunk("beta", {"role": "assistant"})),
            ),
        }
    )
    with caplog.at_level(logging.INFO, logger="invincible.router"):
        first, tail = await router.stream_open(MESSAGES)
    assert first["model"] == "beta-model"
    lines = _attempt_records(caplog)
    assert any("status=413" in m and "failover=true" in m for m in lines)
    await router.close()


async def test_non_failover_upstream_error_raises_client_error(make_router):
    error_body = {"error": {"message": "bad request"}}
    calls = []

    def alpha_handler(request):
        calls.append("alpha")
        return httpx.Response(400, json=error_body)

    def beta_handler(request):
        calls.append("beta")
        return httpx.Response(200, json=provider_body("beta"))

    router = make_router(
        handlers={"alpha.example.com": alpha_handler, "beta.example.com": beta_handler}
    )
    with pytest.raises(UpstreamClientError) as excinfo:
        await router.route_request(MESSAGES)
    assert excinfo.value.status_code == 400
    assert excinfo.value.body == error_body
    assert calls == ["alpha"]


async def test_failover_on_400_flag_retries_next_tier(make_router):
    """failover_on_400: true makes a 400 from this provider cascade to the
    next tier (the different-model case) instead of forwarding."""
    calls = []

    def alpha_handler(request):
        calls.append("alpha")
        return httpx.Response(
            400, json={"error": {"message": "model not found"}}
        )

    providers = default_providers()
    providers[0]["failover_on_400"] = True

    def beta_handler(request):
        calls.append("beta")
        return httpx.Response(200, json=provider_body("beta"))

    router = make_router(
        providers=providers,
        handlers={
            "alpha.example.com": alpha_handler,
            "beta.example.com": beta_handler,
        },
    )
    result = await router.route_request(MESSAGES)
    assert calls == ["alpha", "beta"]
    assert result["choices"][0]["message"]["content"] == "hello"


async def test_failover_on_400_streaming(make_router):
    """The streaming loop honors the flag identically (pre-first-chunk)."""
    calls = []

    def alpha_handler(request):
        calls.append("alpha")
        return httpx.Response(
            400, json={"error": {"message": "model not found"}}
        )

    providers = default_providers()
    providers[0]["failover_on_400"] = True

    def beta_handler(request):
        calls.append("beta")
        return httpx.Response(
            200,
            content=sse_body(stream_chunk("beta", {"content": "hi"})),
        )

    router = make_router(
        providers=providers,
        handlers={
            "alpha.example.com": alpha_handler,
            "beta.example.com": beta_handler,
        },
    )
    first, _tail = await router.stream_open(MESSAGES)
    assert calls == ["alpha", "beta"]
    assert first["choices"][0]["delta"].get("content") == "hi"
    await router.close()


async def test_failover_on_400_false_forwards_even_when_set_on_other_provider(
    make_router,
):
    """A 400 from a provider without the flag still forwards verbatim, even
    when a different tier has the flag set."""
    calls = []

    def alpha_handler(request):
        calls.append("alpha")
        return httpx.Response(400, json={"error": {"message": "bad"}})

    providers = default_providers()
    providers[1]["failover_on_400"] = True
    router = make_router(
        providers=providers,
        handlers={
            "alpha.example.com": alpha_handler,
            "beta.example.com": httpx.Response(200, json=provider_body("beta")),
        },
    )
    with pytest.raises(UpstreamClientError) as excinfo:
        await router.route_request(MESSAGES)
    assert excinfo.value.status_code == 400
    assert calls == ["alpha"]


async def test_all_providers_fail_raises(make_router):
    router = make_router(
        handlers={
            "alpha.example.com": httpx.Response(429),
            "beta.example.com": httpx.Response(503),
            "gamma.example.com": httpx.Response(500),
        }
    )
    with pytest.raises(Exception, match="All providers failed"):
        await router.route_request(MESSAGES)


def test_missing_required_field_raises(make_router):
    providers = [
        {"name": "bad", "tier": 1, "base_url": "https://bad.example.com/v1"}
    ]
    with pytest.raises(ValueError, match="api_key_env"):
        make_router(providers=providers, handlers={})


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        Router(config_path=str(tmp_path / "does-not-exist.yaml"))


def test_malformed_config_file_raises(tmp_path):
    path = tmp_path / "providers.yaml"
    path.write_text("providers: [\n  - name: unclosed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Malformed provider configuration"):
        Router(config_path=str(path))


async def test_stream_open_returns_first_chunk_and_tail(make_router):
    router = make_router(
        handlers={
            "alpha.example.com": httpx.Response(
                200,
                content=sse_body(
                    stream_chunk("alpha", {"role": "assistant"}),
                    stream_chunk("alpha", {"content": "hi"}),
                    stream_chunk("alpha", {}, finish_reason="stop"),
                ),
            )
        }
    )
    first, tail = await router.stream_open(MESSAGES)
    assert first["choices"][0]["delta"] == {"role": "assistant"}
    rest = []
    async for chunk in tail:
        rest.append(chunk)
    assert [c["choices"][0]["delta"] for c in rest] == [
        {"content": "hi"},
        {},
    ]
    await router.close()


async def test_stream_open_failover_before_first_chunk(make_router):
    router = make_router(
        handlers={
            "alpha.example.com": httpx.Response(429),
            "beta.example.com": httpx.Response(
                200,
                content=sse_body(stream_chunk("beta", {"role": "assistant"})),
            ),
        }
    )
    first, tail = await router.stream_open(MESSAGES)
    assert first["model"] == "beta-model"
    assert not router.health_tracker.is_available("alpha")
    await router.close()


@pytest.mark.parametrize("status", [402, 404, 408, 413])
async def test_stream_open_failover_on_limit_and_transient_statuses(
    make_router, status
):
    calls = []

    def alpha_handler(request):
        calls.append("alpha")
        return httpx.Response(status)

    def beta_handler(request):
        calls.append("beta")
        return httpx.Response(
            200,
            content=sse_body(stream_chunk("beta", {"role": "assistant"})),
        )

    router = make_router(
        handlers={"alpha.example.com": alpha_handler, "beta.example.com": beta_handler}
    )
    first, tail = await router.stream_open(MESSAGES)
    assert calls == ["alpha", "beta"]
    assert first["model"] == "beta-model"
    assert not router.health_tracker.is_available("alpha")
    await router.close()


async def test_stream_open_all_providers_fail_raises(make_router):
    router = make_router(
        handlers={"alpha.example.com": httpx.Response(429)}
    )
    with pytest.raises(Exception, match="All providers failed"):
        await router.stream_open(MESSAGES)
    await router.close()


async def test_stream_open_timeout_extension_is_a_dict(make_router):
    captured = []

    def alpha_handler(request):
        captured.append(request)
        return httpx.Response(
            200,
            content=sse_body(stream_chunk("alpha", {"role": "assistant"})),
        )

    router = make_router(handlers={"alpha.example.com": alpha_handler})
    first, _ = await router.stream_open(MESSAGES)
    timeout = captured[0].extensions["timeout"]
    assert isinstance(timeout, dict)
    assert timeout == {
        "connect": DEFAULT_TIMEOUT_CONFIG["connect"],
        "read": DEFAULT_TIMEOUT_CONFIG["read"],
        "write": DEFAULT_TIMEOUT_CONFIG["write"],
        "pool": DEFAULT_TIMEOUT_CONFIG["pool"],
    }
    assert first["model"] == "alpha-model"
    await router.close()

# --- Phase 6: model aliasing --------------------------------------------------


def alias_providers():
    providers = tiered_providers()
    providers[1]["aliases"] = ["fast"]
    providers[3]["aliases"] = ["backup"]
    return providers


async def test_model_alias_prefers_aliased_provider(make_router):
    calls = []

    def handler(name):
        def _h(request):
            calls.append(name)
            return httpx.Response(200, json=provider_body(name))

        return _h

    router = make_router(
        providers=alias_providers(),
        handlers={
            "nim.example.com": handler("nim"),
            "groq.example.com": handler("groq"),
            "openrouter.example.com": handler("openrouter"),
            "gemini.example.com": handler("gemini"),
        },
    )
    result = await router.route_request(MESSAGES, model="fast")
    assert result == provider_body("groq")
    assert calls == ["groq"]
    await router.close()


async def test_model_alias_falls_back_when_preferred_provider_fails(make_router):
    """Soft semantics: a failing aliased provider still fails over to the
    rest of the tier order - an alias is a hint, never a hard constraint."""
    calls = []

    def handler(name, status=200):
        def _h(request):
            calls.append(name)
            return httpx.Response(status, json=provider_body(name))

        return _h

    router = make_router(
        providers=alias_providers(),
        handlers={
            "nim.example.com": handler("nim", 503),
            "groq.example.com": handler("groq", 429),
            "openrouter.example.com": handler("openrouter"),
            "gemini.example.com": handler("gemini"),
        },
    )
    result = await router.route_request(MESSAGES, model="fast")
    assert result == provider_body("openrouter")
    assert calls == ["groq", "nim", "openrouter"]
    await router.close()


async def test_model_id_exact_match_prefers_that_provider(make_router):
    calls = []

    def handler(name):
        def _h(request):
            calls.append(name)
            return httpx.Response(200, json=provider_body(name))

        return _h

    router = make_router(
        providers=alias_providers(),
        handlers={
            "nim.example.com": handler("nim"),
            "groq.example.com": handler("groq"),
            "openrouter.example.com": handler("openrouter"),
            "gemini.example.com": handler("gemini"),
        },
    )
    result = await router.route_request(MESSAGES, model="gpt-oss-120b")
    assert result == provider_body("groq")
    assert calls == ["groq"]
    await router.close()


async def test_unknown_model_keeps_tier_order(make_router):
    calls = []

    def handler(name):
        def _h(request):
            calls.append(name)
            return httpx.Response(200, json=provider_body(name))

        return _h

    router = make_router(
        providers=alias_providers(),
        handlers={
            "nim.example.com": handler("nim"),
            "groq.example.com": handler("groq"),
            "openrouter.example.com": handler("openrouter"),
            "gemini.example.com": handler("gemini"),
        },
    )
    result = await router.route_request(MESSAGES, model="claude-sonnet-4")
    assert result == provider_body("nim")
    assert calls == ["nim"]
    await router.close()


async def test_stream_open_respects_model_alias(make_router):
    calls = []

    def handler(name):
        def _h(request):
            calls.append(name)
            return httpx.Response(
                200, content=sse_body(stream_chunk(name, {"role": "assistant"}))
            )

        return _h

    router = make_router(
        providers=alias_providers(),
        handlers={
            "nim.example.com": handler("nim"),
            "groq.example.com": handler("groq"),
            "openrouter.example.com": handler("openrouter"),
            "gemini.example.com": handler("gemini"),
        },
    )
    first, _ = await router.stream_open(MESSAGES, model="backup")
    assert calls == ["gemini"]
    assert first["model"] == "gemini-model"
    await router.close()


# --- Phase 6: extension point (query auth, chat_path) --------------------------


async def test_query_auth_sends_key_in_query_and_no_bearer(make_router):
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(200, json=provider_body("alpha"))

    provider = {
        "name": "alpha",
        "tier": 1,
        "base_url": "https://alpha.example.com/v1",
        "api_key_env": "ALPHA_API_KEY",
        "model_id": "alpha-model",
        "auth_type": "query",
    }
    router = make_router(
        providers=[provider], handlers={"alpha.example.com": handler}
    )
    await router.route_request(MESSAGES)
    assert "key=test-key-alpha" in captured["url"]
    assert captured["authorization"] is None
    await router.close()


async def test_query_auth_custom_param_and_chat_path(make_router):
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(200, json=provider_body("alpha"))

    provider = {
        "name": "alpha",
        "tier": 1,
        "base_url": "https://alpha.example.com",
        "api_key_env": "ALPHA_API_KEY",
        "model_id": "alpha-model",
        "auth_type": "query",
        "auth_param": "api_key",
        "chat_path": "/v1/chat/completions",
    }
    router = make_router(
        providers=[provider], handlers={"alpha.example.com": handler}
    )
    await router.route_request(MESSAGES)
    assert "/v1/chat/completions" in captured["url"]
    assert "api_key=test-key-alpha" in captured["url"]
    assert captured["authorization"] is None
    await router.close()


async def test_chat_path_override_with_bearer_auth(make_router):
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(200, json=provider_body("alpha"))

    provider = {
        "name": "alpha",
        "tier": 1,
        "base_url": "https://alpha.example.com/v1",
        "api_key_env": "ALPHA_API_KEY",
        "model_id": "alpha-model",
        "chat_path": "/responses",
    }
    router = make_router(
        providers=[provider], handlers={"alpha.example.com": handler}
    )
    await router.route_request(MESSAGES)
    assert "/v1/responses" in captured["url"]
    assert captured["authorization"] == "Bearer test-key-alpha"
    await router.close()
