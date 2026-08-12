import json

import httpx
import pytest
import yaml

from invincible.core.router import Router
from invincible.core.session_store import SessionStore
from invincible.core.tool_executor import PendingActionStore
from invincible.main import app


def default_providers():
    return [
        {
            "name": "alpha",
            "tier": 1,
            "base_url": "https://alpha.example.com/v1",
            "api_key_env": "ALPHA_API_KEY",
            "model_id": "alpha-model",
        },
        {
            "name": "beta",
            "tier": 2,
            "base_url": "https://beta.example.com/v1",
            "api_key_env": "BETA_API_KEY",
            "model_id": "beta-model",
        },
        {
            "name": "gamma",
            "tier": 3,
            "base_url": "https://gamma.example.com/v1",
            "api_key_env": "GAMMA_API_KEY",
            "model_id": "gamma-model",
        },
    ]


def make_transport(handlers):
    def handler(request: httpx.Request) -> httpx.Response:
        callback = handlers.get(request.url.host)
        if callback is None:
            return httpx.Response(500, json={"error": "unexpected host"})
        return callback(request) if callable(callback) else callback

    return httpx.MockTransport(handler)


@pytest.fixture
def provider_config(tmp_path):
    def _write(providers):
        path = tmp_path / "providers.yaml"
        path.write_text(yaml.safe_dump({"providers": providers}), encoding="utf-8")
        return str(path)

    return _write


@pytest.fixture
def make_router(provider_config, monkeypatch):
    def _make(providers=None, handlers=None, missing_keys=None):
        providers = providers if providers is not None else default_providers()
        for provider in providers:
            key = provider.get("api_key_env")
            if not key or (missing_keys and key in missing_keys):
                continue
            monkeypatch.setenv(key, f"test-key-{provider['name']}")
        config_path = provider_config(providers)
        return Router(config_path=config_path, transport=make_transport(handlers or {}))

    return _make


@pytest.fixture
def router_setter(make_router):
    routers = []

    def _set(handlers=None, providers=None):
        routers.append(make_router(handlers=handlers, providers=providers))
        app.state.router = routers[-1]
        return routers[-1]

    _set.routers = routers
    return _set


@pytest.fixture
async def client(router_setter, monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "test-gateway-key")
    router_setter({})
    store = SessionStore(db_path=":memory:")
    await store.init()
    app.state.sessions = store
    app.state.pending_actions = PendingActionStore()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as async_client:
        yield async_client
    for router in router_setter.routers:
        await router.close()
    await store.close()


def provider_body(provider_name, content="hello"):
    return {
        "id": f"cmpl-{provider_name}",
        "model": f"{provider_name}-model",
        "choices": [
            {"message": {"role": "assistant", "content": content}}
        ],
    }


def stream_chunk(provider, delta, finish_reason=None):
    return {
        "id": f"cmpl-{provider}",
        "object": "chat.completion.chunk",
        "created": 1234567890,
        "model": f"{provider}-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def sse_body(*chunks, done=True):
    events = [f"data: {json.dumps(c)}\n\n" for c in chunks]
    if done:
        events.append("data: [DONE]\n\n")
    return "".join(events)
