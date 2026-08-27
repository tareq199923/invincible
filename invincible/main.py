import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Response
from fastapi.staticfiles import StaticFiles

from invincible import __version__
from invincible.core.config import load_providers_config
from invincible.core.continuity import ContinuityEngine
from invincible.core.db import (
    create_all_from_metadata,
    ensure_local_owner,
    make_engine,
    warn_if_schema_stale,
)
from invincible.core.identity import ApiKeyStore, AuditLog
from invincible.core.memory import MemoryStore
from invincible.core.oauth_store import OAuthStore
from invincible.core.provider_registry import ProviderRegistry
from invincible.core.retrieval import RetrievalService
from invincible.core.router import Router
from invincible.core.run_store import RunStore
from invincible.core.session_store import SessionStore
from invincible.core.settings import settings
from invincible.core.tool_executor import PendingActionStore
from invincible.endpoints.accounts import router as accounts_router
from invincible.endpoints.admin_api import router as admin_router
from invincible.endpoints.anthropic_compat import router as anthropic_router
from invincible.endpoints.auth import require_auth
from invincible.endpoints.dashboard import router as dashboard_router
from invincible.endpoints.graph import router as graph_router
from invincible.endpoints.mcp import require_mcp_auth
from invincible.endpoints.mcp import router as mcp_router
from invincible.endpoints.oauth import router as oauth_router
from invincible.endpoints.openai_compat import router as openai_router

load_dotenv()

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger("invincible")


def _warn_if_gateway_open() -> None:
    """The /v1/* chat endpoints fail open when GATEWAY_API_KEY is unset
    (documented behavior, convenient for local use). Make sure anyone
    exposing the server to something untrusted sees that loud and clear."""
    if not settings.gateway_api_key():
        logger.warning(
            "GATEWAY_API_KEY is not set - the /v1/* chat endpoints are "
            "UNAUTHENTICATED. Anyone who can reach this server can use your "
            "providers. Set GATEWAY_API_KEY in your .env before exposing it "
            "through a tunnel or to anything untrusted."
        )


def _warn_if_credential_key_unset() -> None:
    """BYOK provider connections refuse to run without INVINCIBLE_CREDENTIAL_KEY
    (fail closed, same posture as INVINCIBLE_ADMIN_KEY). Surface that at
    startup so operators notice before a user hits a 503 on /providers/mine."""
    if not settings.credential_key():
        logger.warning(
            "INVINCIBLE_CREDENTIAL_KEY is not set - BYOK provider connections "
            "are DISABLED. Generate one with `invincible secret credential-key` "
            "before users can connect their own AI providers."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _warn_if_gateway_open()
    _warn_if_credential_key_unset()
    url = settings.db_url()
    if not url:
        raise RuntimeError(
            "INVINCIBLE_DB_URL is required since Phase 16 "
            "(e.g. postgresql+asyncpg://invincible:pw@localhost:5433/invincible). "
            "For local development run `invincible dev-db` first."
        )
    engine = make_engine(url)
    # Dev bootstrap: metadata is the schema source of truth. The Alembic
    # revision handshake (decided during Phase 16 implementation) only WARNS
    # here - migrations run explicitly via `invincible db upgrade`, never
    # auto-run at startup; doctor fails loudly on mismatches instead.
    await create_all_from_metadata(engine)
    # Phase 1: the system *local* owner must exist before any store write
    # resolves a session (same rows Alembic revision 0002 seeds).
    await ensure_local_owner(engine)
    await warn_if_schema_stale(engine)
    app.state.engine = engine

    seed_config = load_providers_config(settings.config_path())
    app.state.registry = ProviderRegistry(
        file_path=settings.providers_file(), seed_config=seed_config
    )
    app.state.router = Router(registry=app.state.registry)

    oauth_store = OAuthStore(engine)
    await oauth_store.init()
    memory = MemoryStore(engine)
    await memory.init()
    retrieval = RetrievalService(engine)
    await retrieval.init()
    runs = RunStore(engine)
    await runs.init()
    continuity = ContinuityEngine(engine=engine, runs=runs)
    await continuity.init()
    # Phase 4: reactive failover checkpoints fire inside the router's
    # single failover loop via this injected hook (best-effort; the engine
    # itself no-ops when no task state exists).
    app.state.router.failover_hook = continuity.failover_hook()

    pending = PendingActionStore()
    if settings.persist_pending_actions():
        pending.attach_engine(engine)
    app.state.pending_actions = pending
    app.state.oauth_store = oauth_store
    app.state.memory = memory
    app.state.retrieval = retrieval
    app.state.runs = runs
    app.state.continuity = continuity

    app.state.sessions = SessionStore(engine)
    await app.state.sessions.init()
    app.state.api_keys = ApiKeyStore(engine)
    app.state.audit_log = AuditLog(engine)
    app.state.router.run_recorder = runs.record
    yield
    await app.state.router.close()
    await continuity.close()
    await runs.close()
    await retrieval.close()
    await memory.close()
    await oauth_store.close()
    # Drain fire-and-forget staged-action writes before the engine goes.
    await pending.flush_persisted()
    await engine.dispose()

app = FastAPI(title="Invincible", lifespan=lifespan)

# /v1/* resolves a Principal per request (dual-realm: legacy gateway key
# vs per-user API keys; fail-open anonymous when no gateway key is set -
# see endpoints/auth.py for the exact, tested resolution order).

app.include_router(openai_router, dependencies=[Depends(require_auth)])
app.include_router(anthropic_router, dependencies=[Depends(require_auth)])
app.include_router(oauth_router)
app.include_router(mcp_router, dependencies=[Depends(require_mcp_auth)])
# Account surface (browser sessions + per-user management) carries its own
# realm: session cookies and inv_ keys only - never /v1/* or /mcp auth.
app.include_router(accounts_router)
# Phase 5 dashboard pages: cookie-realm only (require_user_session inside).
app.include_router(dashboard_router)
# Vendored browser assets for the dashboard (templates/static ships in the
# wheel via package-data; the minified file is never edited in place).
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).resolve().parent
                / "templates" / "static"),
    name="static",
)
# Management surface carries its own fail-closed authz (INVINCIBLE_ADMIN_KEY).
app.include_router(admin_router)
app.include_router(graph_router)

@app.head("/mcp")
async def mcp_head():
    # MCP clients probe the resource with HEAD before OAuth; answer 200
    # with an empty body. This is a separate unauthenticated route: the
    # POST /mcp handler on mcp_router requires a Bearer token. Starlette
    # records a partial match for HEAD against the POST-only route and
    # continues; only if no later route fully matches would that become
    # 405. Registered after include_router(mcp_router) so the full HEAD
    # match wins the probe without auth.
    return Response(status_code=200)

@app.get("/")
def health_check():
    return {"status": "healthy"}

@app.head("/")
async def head_check():
    # Claude Code probes the base URL with HEAD before sending
    # POST /v1/messages; answer 200 with an empty body.
    return Response(status_code=200)

@app.get("/health")
def health_detail():
    return {
        "service": "Invincible",
        "status": "ok",
        "version": __version__,
    }
