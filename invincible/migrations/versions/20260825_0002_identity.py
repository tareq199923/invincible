"""Platform Phase 1: identity and ownership foundation.

Creates ``users`` / ``projects`` / ``api_keys`` / ``audit_log`` /
``memories``, seeds the system *local* owner (shared constants live in
``invincible.core.db``), and rebuilds ``sessions`` on surrogate identity:
an ``Identity`` PK plus the ownership triple ``(user_id, project_id,
client_session_id)``, with the ``turns`` FK chain repointed to
``sessions.id`` (the legacy session string survives as
``client_session_id``).

The backfill is COUNT-PRESERVING and asserted: after copying, every
old/new row-count pair must match exactly or the revision raises and the
whole migration rolls back (Alembic runs each revision inside a
transaction on PostgreSQL).

Two start states are handled:

- Databases stamped at ``0001`` (real upgrades): the full rebuild path,
  including populated ``sessions``.
- Fresh databases whose lifespan ``create_all`` already built the new
  shape (``sessions.user_id`` exists): seed-only path - the rebuild is
  skipped entirely.

Like the baseline revision, every CREATE carries ``IF NOT EXISTS`` so
running over a bootstrapped database succeeds cleanly.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-25

"""
import time
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from invincible.core.db import LOCAL_OWNER_EMAIL, LOCAL_PROJECT_NAME

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_identity_tables() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column(
            "is_system", sa.Boolean(), nullable=False,
            server_default="false",
        ),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "user_id", sa.BigInteger(),
            sa.ForeignKey("users.id"), nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "is_default", sa.Boolean(), nullable=False,
            server_default="false",
        ),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_projects_user_name"),
        if_not_exists=True,
    )
    op.create_index(
        "idx_projects_user", "projects", ["user_id"],
        unique=False, if_not_exists=True,
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "user_id", sa.BigInteger(),
            sa.ForeignKey("users.id"), nullable=False,
        ),
        sa.Column("label", sa.Text(), nullable=False, server_default=""),
        sa.Column("key_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("prefix", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("last_used_at", sa.Float(), nullable=True),
        sa.Column("revoked_at", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )
    op.create_index(
        "idx_api_keys_user", "api_keys", ["user_id"],
        unique=False, if_not_exists=True,
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("at", sa.Float(), nullable=False),
        sa.Column(
            "actor_user_id", sa.BigInteger(),
            sa.ForeignKey("users.id"), nullable=True,
        ),
        sa.Column("actor_kind", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.Text(), nullable=True),
        sa.Column("resource_id", sa.Text(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("meta", postgresql.JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )
    op.create_index(
        "idx_audit_log_at", "audit_log", [sa.text("at DESC")],
        unique=False, if_not_exists=True,
    )
    op.create_index(
        "idx_audit_log_actor", "audit_log",
        ["actor_user_id", sa.text("at DESC")],
        unique=False, if_not_exists=True,
    )
    op.create_table(
        "memories",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "user_id", sa.BigInteger(),
            sa.ForeignKey("users.id"), nullable=False,
        ),
        sa.Column(
            "project_id", sa.BigInteger(),
            sa.ForeignKey("projects.id"), nullable=True,
        ),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("layer", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False, server_default="fact"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "confidence", sa.Float(), nullable=False, server_default="1.0"
        ),
        sa.Column("provenance", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )
    op.create_index(
        "idx_memories_owner", "memories",
        ["user_id", "project_id", sa.text("created_at DESC")],
        unique=False, if_not_exists=True,
    )


def _drop_identity_tables() -> None:
    op.drop_index(
        "idx_memories_owner", table_name="memories", if_exists=True)
    op.drop_table("memories", if_exists=True)
    op.drop_index(
        "idx_audit_log_actor", table_name="audit_log", if_exists=True)
    op.drop_index(
        "idx_audit_log_at", table_name="audit_log", if_exists=True)
    op.drop_table("audit_log", if_exists=True)
    op.drop_index(
        "idx_api_keys_user", table_name="api_keys", if_exists=True)
    op.drop_table("api_keys", if_exists=True)
    op.drop_index(
        "idx_projects_user", table_name="projects", if_exists=True)
    op.drop_table("projects", if_exists=True)
    op.drop_table("users", if_exists=True)


def _has_column(bind, table: str, column: str) -> bool:
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t "
            "AND column_name = :c"
        ),
        {"t": table, "c": column},
    ).first()
    return row is not None


def _count(bind, table: str) -> int:
    return bind.execute(
        sa.text(f'SELECT COUNT(*) FROM "{table}"')
    ).scalar_one()


def _seed_local_owner(bind) -> tuple[int, int]:
    """Idempotently ensure the system local user + default project exist;
    returns ``(user_id, project_id)``."""
    now = time.time()
    bind.execute(
        sa.text(
            "INSERT INTO users (email, password_hash, is_system, created_at)"
            " VALUES (:email, NULL, TRUE, :now)"
            " ON CONFLICT (email) DO NOTHING"
        ),
        {"email": LOCAL_OWNER_EMAIL, "now": now},
    )
    user_id = bind.execute(
        sa.text("SELECT id FROM users WHERE email = :email"),
        {"email": LOCAL_OWNER_EMAIL},
    ).scalar_one()
    bind.execute(
        sa.text(
            "INSERT INTO projects (user_id, name, is_default, created_at)"
            " VALUES (:uid, :name, TRUE, :now)"
            " ON CONFLICT (user_id, name) DO NOTHING"
        ),
        {"uid": user_id, "name": LOCAL_PROJECT_NAME, "now": now},
    )
    project_id = bind.execute(
        sa.text(
            "SELECT p.id FROM projects p JOIN users u ON u.id = p.user_id"
            " WHERE u.email = :email AND p.name = :name"
        ),
        {"email": LOCAL_OWNER_EMAIL, "name": LOCAL_PROJECT_NAME},
    ).scalar_one()
    return user_id, project_id


# Old-shape artifacts whose schema-global names collide with the rebuilt
# tables; dropped right after the renames so the canonical names free up.
_REBUILD_DROPS = (
    # (object kind, name, table)
    ("index", "idx_turns_session", "turns_old"),
    ("index", "idx_messages_turn", "messages_old"),
    ("constraint", "uq_turns_session_seq", "turns_old"),
    ("constraint", "uq_messages_turn_seq", "messages_old"),
)


def _drop_rebuild_name_conflicts() -> None:
    for kind, name, table in _REBUILD_DROPS:
        if kind == "index":
            op.drop_index(name, table_name=table, if_exists=True)
        else:
            op.drop_constraint(
                name, table_name=table, type_="unique", if_exists=True)


def _rebuild_sessions(bind, user_id: int, project_id: int) -> None:
    """Rebuild sessions/turns/messages under surrogate ownership.

    PostgreSQL index and constraint names are schema-global, so the old
    tables are renamed out of the way (their FKs follow automatically),
    same-named indexes/constraints on them are dropped, the new shapes
    take the canonical names, rows are copied through the ownership
    triple, counts are asserted, and only then are the old tables
    dropped (children first - FK dependency order).
    """
    op.rename_table("sessions", "sessions_old")
    op.rename_table("turns", "turns_old")
    op.rename_table("messages", "messages_old")
    _drop_rebuild_name_conflicts()

    op.create_table(
        "sessions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "user_id", sa.BigInteger(),
            sa.ForeignKey("users.id"), nullable=False,
        ),
        sa.Column(
            "project_id", sa.BigInteger(),
            sa.ForeignKey("projects.id"), nullable=False,
        ),
        sa.Column("client_session_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "project_id", "client_session_id",
            name="uq_sessions_owner_client",
        ),
    )
    op.create_index(
        "idx_sessions_owner", "sessions", ["user_id", "project_id"],
        unique=False,
    )
    op.create_table(
        "turns",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "session_id", sa.BigInteger(),
            sa.ForeignKey("sessions.id"), nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "seq", name="uq_turns_session_seq"),
    )
    op.create_index(
        "idx_turns_session", "turns", ["session_id", "seq"], unique=False,
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "turn_id", sa.BigInteger(),
            sa.ForeignKey("turns.id"), nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("turn_id", "seq", name="uq_messages_turn_seq"),
    )
    op.create_index(
        "idx_messages_turn", "messages", ["turn_id", "seq"], unique=False,
    )

    params = {"u": user_id, "p": project_id}

    # 1) Sessions: the legacy string becomes client_session_id.
    bind.execute(
        sa.text(
            "INSERT INTO sessions (user_id, project_id, client_session_id,"
            " created_at, updated_at)"
            " SELECT :u, :p, session_id, created_at, updated_at"
            " FROM sessions_old ORDER BY session_id"
        ),
        params,
    )

    # 2) Turns: remapped through the now-unique ownership triple
    #    (UNIQUE(user_id, project_id, client_session_id) guarantees a
    #    single match per legacy string).
    bind.execute(
        sa.text(
            "INSERT INTO turns (session_id, seq)"
            " SELECT s.id, t.seq FROM turns_old t"
            " JOIN sessions_old o ON t.session_id = o.session_id"
            " JOIN sessions s ON s.client_session_id = o.session_id"
            "  AND s.user_id = :u AND s.project_id = :p"
        ),
        params,
    )

    # 3) Messages: turn identity maps via (legacy session string, seq),
    #    which is unique on both sides of the move.
    bind.execute(
        sa.text(
            "INSERT INTO messages (turn_id, seq, role, payload)"
            " SELECT nt.id, m.seq, m.role, m.payload FROM messages_old m"
            " JOIN turns_old t ON m.turn_id = t.id"
            " JOIN sessions_old o ON t.session_id = o.session_id"
            " JOIN sessions s ON s.client_session_id = o.session_id"
            "  AND s.user_id = :u AND s.project_id = :p"
            " JOIN turns nt ON nt.session_id = s.id AND nt.seq = t.seq"
        ),
        params,
    )

    # Count-preservation gate: abort (and roll the whole revision back)
    # rather than ever lose a row silently.
    for new_table, old_table in (
        ("sessions", "sessions_old"),
        ("turns", "turns_old"),
        ("messages", "messages_old"),
    ):
        new_n, old_n = _count(bind, new_table), _count(bind, old_table)
        if new_n != old_n:
            raise RuntimeError(
                f"Phase 1 backfill aborted: '{new_table}' received "
                f"{new_n} row(s) but '{old_table}' held {old_n}. The "
                f"migration transaction rolled back; nothing changed."
            )

    op.drop_table("messages_old")
    op.drop_table("turns_old")
    op.drop_table("sessions_old")


def upgrade() -> None:
    _create_identity_tables()
    bind = op.get_bind()
    user_id, project_id = _seed_local_owner(bind)

    if _has_column(bind, "sessions", "user_id"):
        # Fresh-database path: lifespan create_all already built the new
        # shape; seeding the local owner above completes this revision.
        return
    _rebuild_sessions(bind, user_id, project_id)


def _restore_legacy_sessions(bind) -> None:
    """Inverse of :func:`_rebuild_sessions` (downgrade path).

    Turn/message ids are preserved exactly; sessions collapse back to the
    legacy string PK. If hosted-era data ever produced the same
    ``client_session_id`` under different owners, one arbitrary row wins -
    acceptable for a developer-only downgrade path.
    """
    op.rename_table("sessions", "sessions_p1")
    op.rename_table("turns", "turns_p1")
    op.rename_table("messages", "messages_p1")

    # Free the schema-global names the legacy shapes need back.
    op.drop_index(
        "idx_turns_session", table_name="turns_p1", if_exists=True)
    op.drop_index(
        "idx_messages_turn", table_name="messages_p1", if_exists=True)
    op.drop_constraint(
        "uq_turns_session_seq", table_name="turns_p1", type_="unique",
        if_exists=True,
    )
    op.drop_constraint(
        "uq_messages_turn_seq", table_name="messages_p1", type_="unique",
        if_exists=True,
    )
    op.drop_index(
        "idx_sessions_owner", table_name="sessions_p1", if_exists=True)

    op.create_table(
        "sessions",
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_table(
        "turns",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "session_id", sa.Text(),
            sa.ForeignKey("sessions.session_id"), nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "seq", name="uq_turns_session_seq"),
    )
    op.create_index(
        "idx_turns_session", "turns", ["session_id", "seq"], unique=False,
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "turn_id", sa.BigInteger(),
            sa.ForeignKey("turns.id"), nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("turn_id", "seq", name="uq_messages_turn_seq"),
    )
    op.create_index(
        "idx_messages_turn", "messages", ["turn_id", "seq"], unique=False,
    )

    bind.execute(
        sa.text(
            "INSERT INTO sessions (session_id, created_at, updated_at)"
            " SELECT DISTINCT ON (client_session_id)"
            "  client_session_id, created_at, updated_at"
            " FROM sessions_p1 ORDER BY client_session_id, id"
        )
    )
    bind.execute(
        sa.text(
            "INSERT INTO turns (id, session_id, seq)"
            " SELECT t.id, s.client_session_id, t.seq"
            " FROM turns_p1 t JOIN sessions_p1 s ON t.session_id = s.id"
        )
    )
    bind.execute(
        sa.text(
            "INSERT INTO messages (id, turn_id, seq, role, payload)"
            " SELECT id, turn_id, seq, role, payload FROM messages_p1"
        )
    )

    # turns/messages must round-trip exactly; sessions may legitimately
    # dedupe (see docstring).
    for kept, backup in (("turns", "turns_p1"), ("messages", "messages_p1")):
        if _count(bind, kept) != _count(bind, backup):
            raise RuntimeError(
                f"Phase 1 downgrade aborted: '{kept}' lost rows vs "
                f"'{backup}'. Rolled back."
            )

    op.drop_table("messages_p1")
    op.drop_table("turns_p1")
    op.drop_table("sessions_p1")


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "sessions", "user_id"):
        _restore_legacy_sessions(bind)
    _drop_identity_tables()
