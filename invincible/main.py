import hmac
import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response

from invincible import __version__
from invincible.core.config import load_providers_config
from invincible.core.continuity import ContinuityEngine
from invincible.core.memory import MemoryStore
from invincible.core.oauth_store import OAuthStore
from invincible.core.provider_registry import ProviderRegistry
from invincible.core.router import Router
from invincible.core.run_store import RunStore
from invincible.core.session_store import SessionStore
from invincible.core.settings import settings
from invincible.core.tool_executor import PendingActionStore
from invincible.endpoints.admin_api import router as admin_router
from invincible.endpoints.anthropic_compat import router as anthropic_router
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_path = settings.db_path()
    _warn_if_gateway_open()
    # Provider state (Phase 13.5): the packaged/override YAML seeds a
    # file-backed registry when INVINCIBLE_PROVIDERS_FILE is set; without
    # it the registry runs read-only and management mutations refuse.
    seed_config = load_providers_config(settings.config_path())
    app.state.registry = ProviderRegistry(
        file_path=settings.providers_file(), seed_config=seed_config
    )
    app.state.router = Router(registry=app.state.registry)
    app.state.sessions = SessionStore(db_path=db_path)
    if settings.persist_pending_actions():
        app.state.pending_actions = PendingActionStore(db_path=db_path)
    else:
        app.state.pending_actions = PendingActionStore()
    app.state.oauth_store = OAuthStore(db_path=db_path)
    await app.state.sessions.init()
    await app.state.oauth_store.init()
    # Phase 10 fact memory: shares the session DB connection so a
    # `:memory:` database stays one database (tests, ephemeral runs).
    app.state.memory = MemoryStore(shared=app.state.sessions)
    await app.state.memory.init()
    # Phase 13.5 provider-run records share the same connection; the
    # recorder is bound only after the store is live.
    app.state.runs = RunStore(shared=app.state.sessions)
    await app.state.runs.init()
    app.state.router.run_recorder = app.state.runs.record
    # Phase 15b continuity engine: canonical task state shared by LLM
    # requests and MCP tools; reads runs for interruption awareness.
    app.state.continuity = ContinuityEngine(
        shared=app.state.sessions, runs=app.state.runs
    )
    await app.state.continuity.init()
    yield
    await app.state.router.close()
    await app.state.continuity.close()
    await app.state.runs.close()
    await app.state.memory.close()
    await app.state.sessions.close()
    await app.state.oauth_store.close()

app = FastAPI(title="Invincible", lifespan=lifespan)

async def require_auth(request: Request):
    gateway_key = settings.gateway_api_key()
    if not gateway_key:
        return
    auth = request.headers.get("Authorization")
    if auth and auth.startswith("Bearer "):
        token = auth.removeprefix("Bearer ")
    else:
        token = request.headers.get("x-api-key")
    if not token:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": "Missing authentication token",
                    "type": "auth_error",
                }
            },
        )
    if not hmac.compare_digest(
        token.encode("utf-8"), gateway_key.encode("utf-8")
    ):
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": "Invalid authentication token",
                    "type": "auth_error",
                }
            },
        )

app.include_router(openai_router, dependencies=[Depends(require_auth)])
app.include_router(anthropic_router, dependencies=[Depends(require_auth)])
app.include_router(oauth_router)
app.include_router(mcp_router, dependencies=[Depends(require_mcp_auth)])
# Management surface carries its own fail-closed authz (INVINCIBLE_ADMIN_KEY).
app.include_router(admin_router)

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
