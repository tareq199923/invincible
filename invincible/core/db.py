# invincible/core/db.py
"""PostgreSQL foundation: engine factory + the single source of schema
truth (Phase 16).

Every persistent store builds its queries against the ``metadata`` tables
declared here, so Alembic migrations (PG) and runtime ``create_all`` (test
databases) can never drift apart.

Type decisions (recorded in the Phase 16 plan):
- Timestamps stay epoch ``Float`` - TIMESTAMPTZ conversion is deferred.
- JSON-shaped columns use PostgreSQL ``JSONB`` (messages payload, runs meta,
  task_states payload, oauth redirect_uris, pending args).
- Surrogate PKs are ``BigInteger`` + ``Identity``; natural keys
  (token_hash, code, client_id, token) stay ``Text`` primary keys. Since
  Platform Phase 1, ``sessions`` also carries a surrogate PK with an
  ownership triple (user/project/client_session_id).
"""
import asyncio
import concurrent.futures
import importlib
import logging
import time

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine

logger = logging.getLogger("invincible.db")

metadata = MetaData()


def make_engine(url: str, **kwargs):
    """Async engine over asyncpg. ``pool_pre_ping`` so a restarted dev
    database heals instead of poisoning pooled connections."""
    kwargs.setdefault("pool_pre_ping", True)
    return create_async_engine(url, **kwargs)


def run_coro_sync(coro):
    """Run ``coro`` to completion from synchronous code.

    Plain ``asyncio.run`` breaks when an event loop is already running on
    this thread (pytest-asyncio tests driving the CLI, embedded runners),
    so in that case the coroutine executes on a helper thread with its own
    fresh loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


async def create_all_from_metadata(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)


async def drop_all_from_metadata(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(metadata.drop_all)


async def ensure_local_owner(engine) -> tuple[int, int]:
    """Idempotently ensure the system *local* user + its default project
    exist; returns ``(user_id, project_id)``.

    Runtime bootstrap for the same rows Alembic revision 0002 seeds: fresh
    ``create_all`` databases get the local owner on first use instead of
    requiring a migration round-trip. Unique constraints make concurrent
    callers safe; callers memoize (stores keep one resolved context).
    """
    async with engine.begin() as conn:
        return await seed_local_owner_conn(conn)


async def seed_local_owner_conn(conn) -> tuple[int, int]:
    """Connection-scoped variant of :func:`ensure_local_owner` - runs
    inside the caller's transaction (importer, batch bootstrap)."""
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    now = time.time()
    await conn.execute(
        pg_insert(users)
        .values(email=LOCAL_OWNER_EMAIL, is_system=True, created_at=now)
        .on_conflict_do_nothing(index_elements=["email"])
    )
    user_id = (await conn.execute(
        select(users.c.id).where(users.c.email == LOCAL_OWNER_EMAIL)
    )).scalar_one()
    await conn.execute(
        pg_insert(projects)
        .values(user_id=user_id, name=LOCAL_PROJECT_NAME,
                is_default=True, created_at=now)
        .on_conflict_do_nothing(index_elements=["user_id", "name"])
    )
    project_id = (await conn.execute(
        select(projects.c.id)
        .where(projects.c.user_id == user_id,
               projects.c.name == LOCAL_PROJECT_NAME)
    )).scalar_one()
    return int(user_id), int(project_id)


# ---------------------------------------------------------------------------
# Schema-truth handshake (Phase 16, decided during implementation):
# `invincible db upgrade` writes alembic_version; doctor FAILs loudly on a
# mismatch or an unmanaged-but-populated schema; startup only WARNS (migrations
# run explicitly - never auto-run against production).


def migrations_config(db_url: str | None = None):
    """Alembic Config pointing at the packaged ``invincible/migrations``
    directory, so ``db upgrade`` works from any cwd and any install mode."""
    from alembic.config import Config

    try:
        ref = importlib.resources.files("invincible").joinpath("migrations")
        location = str(ref)
    except (ModuleNotFoundError, TypeError, AttributeError) as exc:
        raise RuntimeError(
            "Packaged migrations directory not found"
        ) from exc
    cfg = Config()
    cfg.set_main_option("script_location", location)
    if db_url:
        cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def migration_heads() -> tuple[str, ...] | None:
    """Head revision(s) of the packaged migration scripts; None when the
    scripts cannot be loaded (doctor reports that loudly)."""
    try:
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(migrations_config())
        return tuple(script.get_heads())
    except Exception as exc:  # noqa: BLE001 - any failure means "unknown"
        logger.warning("Could not load migration scripts: %s", exc)
        return None


async def stored_schema_revision(engine) -> str | None:
    """The database's current alembic revision, or None when the schema is
    not under migration control (no alembic_version table)."""
    from sqlalchemy import text

    async with engine.connect() as conn:
        exists = (await conn.execute(text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'alembic_version'"
        ))).scalar()
        if not exists:
            return None
        return (await conn.execute(
            text("SELECT version_num FROM alembic_version")
        )).scalar()


async def warn_if_schema_stale(engine) -> None:
    """Loud startup warning when the schema is not at the migration head.

    Deliberately non-blocking: dev databases bootstrap via create_all below,
    but operators are always told to run `invincible db upgrade`."""
    heads = migration_heads()
    if heads is None:
        logger.warning(
            "Schema check skipped: packaged migration scripts unavailable."
        )
        return
    stored = await stored_schema_revision(engine)
    if stored is None:
        logger.warning(
            "Database schema is not managed by Alembic (no alembic_version). "
            "Run `invincible db upgrade` to bring it under migration control."
        )
    elif stored not in heads:
        logger.warning(
            "Database schema revision %s does not match migration head %s. "
            "Run `invincible db upgrade`.",
            stored, "/".join(heads),
        )


# ---------------------------------------------------------------------------
# Identity & ownership  (Platform Phase 1)

# System *local* owner: every legacy/local-mode row backfills to this user
# and its default project. Declared here (not in a store module) so the
# packaged Alembic revisions and the runtime bootstrap share one constant.
LOCAL_OWNER_EMAIL = "local@invincible.local"
LOCAL_PROJECT_NAME = "local"

users = Table(
    "users",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    # Natural key; unique constraint backs the idempotent local-owner seed.
    Column("email", Text, nullable=False, unique=True),
    # argon2id hash (core.identity); NULL = no password login (system user).
    Column("password_hash", Text),
    Column("is_system", Boolean, nullable=False, server_default="false"),
    Column("created_at", Float, nullable=False),
)

projects = Table(
    "projects",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("user_id", BigInteger, ForeignKey("users.id"), nullable=False),
    Column("name", Text, nullable=False),
    Column("is_default", Boolean, nullable=False, server_default="false"),
    Column("created_at", Float, nullable=False),
    UniqueConstraint("user_id", "name", name="uq_projects_user_name"),
)

Index("idx_projects_user", projects.c.user_id)

api_keys = Table(
    "api_keys",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("user_id", BigInteger, ForeignKey("users.id"), nullable=False),
    Column("label", Text, nullable=False, server_default=""),
    # sha256 hex of the raw key - raw values are shown once at creation and
    # never stored (same discipline as oauth_tokens.token_hash).
    Column("key_hash", Text, nullable=False, unique=True),
    # Visible identifier (first chars of the raw key incl. the inv_ prefix)
    # so keys are recognizable in listings without revealing the secret.
    Column("prefix", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("last_used_at", Float),
    Column("revoked_at", Float),
)

Index("idx_api_keys_user", api_keys.c.user_id)

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("at", Float, nullable=False),
    Column("actor_user_id", BigInteger, ForeignKey("users.id")),
    Column("actor_kind", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("resource_type", Text),
    Column("resource_id", Text),
    Column("request_id", Text),
    Column("meta", JSONB),
)

Index("idx_audit_log_at", audit_log.c.at.desc())
Index("idx_audit_log_actor", audit_log.c.actor_user_id, audit_log.c.at.desc())

# Scoped memory rows (schema lands in Phase 1; retrieval engine is Phase 4).
memories = Table(
    "memories",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("user_id", BigInteger, ForeignKey("users.id"), nullable=False),
    Column("project_id", BigInteger, ForeignKey("projects.id")),
    # scope: 'user' | 'project' (project requires project_id); layer:
    # 'explicit' | 'auto'.
    Column("scope", Text, nullable=False),
    Column("layer", Text, nullable=False),
    Column("kind", Text, nullable=False, server_default="fact"),
    Column("content", Text, nullable=False),
    Column("confidence", Float, nullable=False, server_default="1.0"),
    Column("provenance", Text),
    Column("created_at", Float, nullable=False),
)

Index(
    "idx_memories_owner",
    memories.c.user_id,
    memories.c.project_id,
    memories.c.created_at.desc(),
)


# ---------------------------------------------------------------------------
# Sessions / turns / messages

# Phase 1 rebuild: ``sessions`` lost its client-supplied string PK and now
# carries surrogate identity plus an ownership triple. The legacy session
# string survives as ``client_session_id`` under UNIQUE(user_id, project_id,
# client_session_id); ``turns.session_id`` keeps its column name but is now
# a BigInteger FK to ``sessions.id``.

sessions = Table(
    "sessions",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("user_id", BigInteger, ForeignKey("users.id"), nullable=False),
    Column("project_id", BigInteger, ForeignKey("projects.id"), nullable=False),
    Column("client_session_id", String, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    UniqueConstraint(
        "user_id", "project_id", "client_session_id",
        name="uq_sessions_owner_client",
    ),
)

Index("idx_sessions_owner", sessions.c.user_id, sessions.c.project_id)

turns = Table(
    "turns",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column(
        "session_id",
        BigInteger,
        ForeignKey("sessions.id"),
        nullable=False,
    ),
    Column("seq", Integer, nullable=False),
    UniqueConstraint("session_id", "seq", name="uq_turns_session_seq"),
)

messages = Table(
    "messages",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column(
        "turn_id",
        BigInteger,
        ForeignKey("turns.id"),
        nullable=False,
    ),
    Column("seq", Integer, nullable=False),
    Column("role", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    UniqueConstraint("turn_id", "seq", name="uq_messages_turn_seq"),
)

Index("idx_turns_session", turns.c.session_id, turns.c.seq)
Index("idx_messages_turn", messages.c.turn_id, messages.c.seq)


# ---------------------------------------------------------------------------
# Continuity  (Phase 15b shapes)

facts = Table(
    "facts",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("user_id", Text, nullable=False, server_default="default"),
    Column("session_id", Text, nullable=False),
    Column("entity", Text, nullable=False),
    Column("relation", Text, nullable=False),
    Column("target", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    UniqueConstraint(
        "user_id", "session_id", "entity", "relation", "target",
        name="uq_facts_triple",
    ),
)

runs = Table(
    "runs",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("request_id", Text, nullable=False),
    Column("session_id", Text),
    Column("provider_name", Text, nullable=False),
    Column("model_id", Text, nullable=False),
    Column("attempt_index", Integer, nullable=False),
    Column("outcome", Text, nullable=False),
    Column("error_class", Text),
    Column("started_at", Float, nullable=False),
    Column("finished_at", Float),
    Column("meta", JSONB),
)

Index("idx_runs_session", runs.c.session_id, runs.c.started_at)
Index("idx_runs_outcome", runs.c.outcome)

task_states = Table(
    "task_states",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("session_id", Text, nullable=False),
    Column("task_key", Text, nullable=False, server_default="default"),
    Column("status", Text, nullable=False, server_default="active"),
    Column("payload", JSONB, nullable=False),
    Column("version", BigInteger, nullable=False),
    Column("updated_by", Text, nullable=False),
    Column("request_id", Text),
    Column("updated_at", Float, nullable=False),
    UniqueConstraint(
        "session_id", "task_key", "version", name="uq_task_states_version"
    ),
)

Index(
    "idx_task_states_session",
    task_states.c.session_id,
    task_states.c.task_key,
    task_states.c.version.desc(),
)

checkpoints = Table(
    "checkpoints",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("session_id", Text, nullable=False),
    Column("task_key", Text, nullable=False, server_default="default"),
    Column("state_version", Integer, nullable=False),
    Column("note", Text, nullable=False, server_default=""),
    Column("created_at", Float, nullable=False),
)

Index(
    "idx_checkpoints_session",
    checkpoints.c.session_id,
    checkpoints.c.task_key,
    checkpoints.c.created_at.desc(),
)


# ---------------------------------------------------------------------------
# OAuth 2.1 + PKCE  (shapes carried over verbatim; uris -> JSONB list)

oauth_clients = Table(
    "oauth_clients",
    metadata,
    Column("client_id", String, primary_key=True),
    Column("client_name", String, nullable=False, server_default=""),
    Column("redirect_uris", JSONB, nullable=False),
    Column("created_at", Float, nullable=False),
)

oauth_codes = Table(
    "oauth_codes",
    metadata,
    Column("code", String, primary_key=True),
    Column("client_id", String, nullable=False),
    Column("redirect_uri", String, nullable=False),
    Column("code_challenge", String, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("used", Boolean, nullable=False, server_default="false"),
)

oauth_tokens = Table(
    "oauth_tokens",
    metadata,
    Column("token_hash", String, primary_key=True),
    Column("token_type", String, nullable=False),
    Column("client_id", String, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("revoked", Boolean, nullable=False, server_default="false"),
    Column("created_at", Float, nullable=False),
)


# ---------------------------------------------------------------------------
# MCP staged approvals

pending_actions = Table(
    "pending_actions",
    metadata,
    Column("token", String, primary_key=True),
    Column("type", String, nullable=False),
    Column("args", JSONB, nullable=False),
    Column("created_at", Float, nullable=False),
)
