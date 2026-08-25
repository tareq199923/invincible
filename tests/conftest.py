import json
import os
import secrets
import socket
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.engine import make_url

from invincible.core.continuity import ContinuityEngine
from invincible.core.db import (
    create_all_from_metadata,
    make_engine,
)
from invincible.core.db import (
    metadata as db_metadata,
)
from invincible.core.identity import ApiKeyStore
from invincible.core.memory import MemoryStore
from invincible.core.oauth_store import OAuthStore, _s256_challenge
from invincible.core.router import Router
from invincible.core.run_store import RunStore
from invincible.core.session_store import SessionStore
from invincible.core.tool_executor import PendingActionStore
from invincible.main import app

TEST_OWNER_SECRET = "test-owner-secret"
TEST_REDIRECT_URI = "http://localhost:9999/callback"


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


TEST_DB_URL = os.getenv(
    "INVINCIBLE_TEST_DATABASE_URL",
    "postgresql+asyncpg://invincible@127.0.0.1:5433/invincible_test",
)

_TRUNCATE_SQL = "TRUNCATE {} RESTART IDENTITY CASCADE".format(
    ", ".join(f'"{t}"' for t in sorted(db_metadata.tables))
)


@pytest.fixture(scope="session")
def pg_live():
    """Opt-in marker fixture for tests that need the *real* local Postgres
    beyond what ``pg_engine`` already requires (scratch databases, CLI
    subprocess-style flows). Auto-skips so the suite stays runnable - and
    informative - on machines without a server at TEST_DB_URL."""
    host = urlparse(TEST_DB_URL).hostname or "127.0.0.1"
    port = urlparse(TEST_DB_URL).port or 5432
    try:
        with socket.create_connection((host, port), timeout=0.5):
            pass
    except OSError:
        pytest.skip(
            f"local Postgres not reachable at {host}:{port} "
            "(INVINCIBLE_TEST_DATABASE_URL)"
        )


@pytest.fixture
async def pg_engine():
    """Function-scoped engine on invincible_test with a clean schema:
    create_all up front, TRUNCATE after each test so storage tests never
    see each other's rows."""
    eng = make_engine(TEST_DB_URL)
    await create_all_from_metadata(eng)
    try:
        yield eng
    finally:
        async with eng.begin() as conn:
            await conn.execute(text(_TRUNCATE_SQL))
        await eng.dispose()


@pytest.fixture
async def stamp_revision(pg_engine):
    """Async callable: force the schema's recorded alembic revision (None
    drops the table). Clears the stamp afterwards so revision tests never
    leak. Async on purpose - it shares pg_engine's event loop."""

    async def _stamp(revision: str | None) -> None:
        async with pg_engine.begin() as conn:
            if revision is None:
                await conn.execute(
                    text("DROP TABLE IF EXISTS alembic_version"))
                return
            await conn.execute(text(
                "CREATE TABLE IF NOT EXISTS alembic_version ("
                " version_num VARCHAR(32) NOT NULL)"
            ))
            await conn.execute(text("DELETE FROM alembic_version"))
            await conn.execute(text(
                "INSERT INTO alembic_version (version_num) VALUES (:r)"
            ), {"r": revision})

    yield _stamp
    await _stamp(None)


@pytest.fixture
async def admin_pg(pg_live):
    """Async callable running SQL against the maintenance database - used
    to create/drop scratch databases for CLI-tier integration tests."""
    import asyncpg

    admin_url = make_url(TEST_DB_URL).set(database="postgres")
    dsn = admin_url.render_as_string().replace("+asyncpg", "")

    async def _run_sql(statement: str) -> None:
        conn = await asyncpg.connect(dsn, timeout=5)
        try:
            await conn.execute(statement)
        finally:
            await conn.close()

    return _run_sql


@pytest.fixture
async def client(pg_engine, router_setter, monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "test-gateway-key")
    monkeypatch.setenv("INVINCIBLE_OWNER_SECRET", TEST_OWNER_SECRET)
    router_setter({})
    store = SessionStore(engine=pg_engine)
    app.state.engine = pg_engine
    app.state.sessions = store
    memory = MemoryStore(engine=pg_engine)
    await memory.init()
    app.state.memory = memory
    pending = PendingActionStore()
    if os.getenv("INVINCIBLE_PERSIST_PENDING_ACTIONS"):
        pending.attach_engine(pg_engine)
        await pending.load_persisted()
    app.state.pending_actions = pending
    oauth_store = OAuthStore(engine=pg_engine)
    await oauth_store.init()
    app.state.oauth_store = oauth_store
    app.state.api_keys = ApiKeyStore(engine=pg_engine)
    runs_store = RunStore(engine=pg_engine)
    await runs_store.init()
    app.state.runs = runs_store
    continuity = ContinuityEngine(engine=pg_engine, runs=runs_store)
    await continuity.init()
    app.state.continuity = continuity
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as async_client:
        yield async_client
    for router in router_setter.routers:
        await router.close()
    await continuity.close()
    await runs_store.close()
    await memory.close()
    await oauth_store.close()
    app.state.oauth_store = None


def pkce_pair():
    """Generate a (code_verifier, code_challenge) pair."""
    verifier = secrets.token_urlsafe(32)
    return verifier, _s256_challenge(verifier)


def authorize_params(client_id, challenge, redirect_uri=TEST_REDIRECT_URI):
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "xyz-state",
    }


async def oauth_register(client, redirect_uri=TEST_REDIRECT_URI, name="test-client"):
    """Register an OAuth client; returns (client_id, redirect_uri)."""
    response = await client.post(
        "/oauth/register",
        json={"redirect_uris": [redirect_uri], "client_name": name},
    )
    assert response.status_code == 201, response.text
    return response.json()["client_id"], redirect_uri


async def oauth_login(client, params, owner_secret=TEST_OWNER_SECRET):
    """Submit the owner-login form (sets the session cookie on success)."""
    return await client.post(
        "/oauth/authorize", data={**params, "owner_secret": owner_secret}
    )


async def oauth_approve(client, params, deny=False):
    """Submit the Approve/Deny consent form; returns the redirect Location
    with code/error. Consent is POST-only (GET links were CSRF-able)."""
    action = "deny" if deny else "approve"
    response = await client.post(
        "/oauth/authorize",
        data={**params, "action": action},
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text[:300]
    return response.headers["location"]


async def oauth_exchange(
    client, code, client_id, redirect_uri=TEST_REDIRECT_URI, verifier=None
):
    """Exchange an authorization code for tokens."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
    }
    if verifier is not None:
        data["code_verifier"] = verifier
    return await client.post("/oauth/token", data=data)


async def obtain_access_token(client):
    """Run the complete register -> login -> approve -> exchange flow and
    return the raw access token (and client_id, refresh_token, verifier)."""
    verifier, challenge = pkce_pair()
    client_id, redirect_uri = await oauth_register(client)
    params = authorize_params(client_id, challenge, redirect_uri)
    login = await oauth_login(client, params)
    assert login.status_code == 302, login.text[:300]
    location = await oauth_approve(client, params)
    code = parse_qs(urlparse(location).query)["code"][0]
    exchange = await oauth_exchange(client, code, client_id, redirect_uri, verifier)
    assert exchange.status_code == 200, exchange.text
    tokens = exchange.json()
    return {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "verifier": verifier,
        "code": code,
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
    }


@pytest.fixture
async def bearer_headers(client):
    """Authorization header for API tests that need a valid MCP token."""
    tokens = await obtain_access_token(client)
    return {"Authorization": f"Bearer {tokens['access_token']}"}


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
