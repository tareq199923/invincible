import hmac
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response

from invincible import __version__
from invincible.core.memory import MemoryStore
from invincible.core.oauth_store import OAuthStore
from invincible.core.router import Router
from invincible.core.session_store import SessionStore
from invincible.core.tool_executor import PendingActionStore
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
    if not os.getenv("GATEWAY_API_KEY"):
        logger.warning(
            "GATEWAY_API_KEY is not set - the /v1/* chat endpoints are "
            "UNAUTHENTICATED. Anyone who can reach this server can use your "
            "providers. Set GATEWAY_API_KEY in your .env before exposing it "
            "through a tunnel or to anything untrusted."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_path = os.getenv("INVINCIBLE_DB_PATH")
    _warn_if_gateway_open()
    app.state.router = Router(config_path=os.getenv("INVINCIBLE_CONFIG_PATH"))
    app.state.sessions = SessionStore(db_path=db_path)
    if os.getenv("INVINCIBLE_PERSIST_PENDING_ACTIONS"):
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
    yield
    await app.state.router.close()
    await app.state.memory.close()
    await app.state.sessions.close()
    await app.state.oauth_store.close()

app = FastAPI(title="Invincible", lifespan=lifespan)

async def require_auth(request: Request):
    gateway_key = os.getenv("GATEWAY_API_KEY")
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
