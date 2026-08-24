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
- Surrogate PKs are ``BigInteger`` + ``Identity``; natural keys (session_id,
  token_hash, code, client_id, token) stay ``Text`` primary keys.
"""
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

metadata = MetaData()


def make_engine(url: str, **kwargs):
    """Async engine over asyncpg. ``pool_pre_ping`` so a restarted dev
    database heals instead of poisoning pooled connections."""
    kwargs.setdefault("pool_pre_ping", True)
    return create_async_engine(url, **kwargs)


async def create_all_from_metadata(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)


async def drop_all_from_metadata(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(metadata.drop_all)


# ---------------------------------------------------------------------------
# Sessions / turns / messages  (Phase 15a shapes, PG-typed)

sessions = Table(
    "sessions",
    metadata,
    # Renamed from sessions_v2 during Phase 16: PG is the only backend now,
    # so the version suffix lost its meaning.
    Column("session_id", String, primary_key=True),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)

turns = Table(
    "turns",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column(
        "session_id",
        Text,
        ForeignKey("sessions.session_id"),
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
