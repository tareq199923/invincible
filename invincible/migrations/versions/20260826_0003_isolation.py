"""Platform Phase 2: isolation and security columns.

Adds ownership to every string-keyed store so predicates can be enforced
per principal:

- ``runs`` / ``task_states`` / ``checkpoints`` gain ``session_pk``
  (FK → ``sessions.id``): the owning surrogate session, resolved under
  the writing principal. The legacy UNIQUE(session_id, task_key,
  version) on ``task_states`` is REPLACED by a partial unique index on
  ``(session_pk, task_key, version) WHERE session_pk IS NOT NULL`` -
  the client string is no longer globally unique, so the old constraint
  would collide two principals sharing one string.
- OAuth user subjects: ``oauth_clients.owner_user_id``,
  ``oauth_codes.subject_user_id``, ``oauth_tokens.subject_user_id``.
- New ``login_attempts`` table (persistent owner-login rate limiting).

Backfill (idempotent, count-preserving - no rows move or vanish):
existing oauth rows are stamped with the system *local* owner; distinct
session strings referenced by task/checkpoint/run rows get local-owned
``sessions`` rows created if missing, then their ``session_pk`` is set.

Like earlier revisions, every step guards on information_schema so it is
a clean pass over fresh databases whose lifespan ``create_all`` already
built the new shape.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-26

"""
import time
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from invincible.core.db import LOCAL_OWNER_EMAIL, LOCAL_PROJECT_NAME

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ADD_COLUMNS = (
    # (table, column, DDL fragment)
    ("runs", "session_pk",
     "BIGINT REFERENCES sessions(id)"),
    ("task_states", "session_pk",
     "BIGINT REFERENCES sessions(id)"),
    ("checkpoints", "session_pk",
     "BIGINT REFERENCES sessions(id)"),
    ("oauth_clients", "owner_user_id",
     "BIGINT REFERENCES users(id)"),
    ("oauth_codes", "subject_user_id",
     "BIGINT REFERENCES users(id)"),
    ("oauth_tokens", "subject_user_id",
     "BIGINT REFERENCES users(id)"),
)


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


def _has_object(bind, kind: str, name: str) -> bool:
    if kind == "index":
        row = bind.execute(
            sa.text("SELECT 1 FROM pg_indexes WHERE indexname = :n"),
            {"n": name},
        ).first()
    else:
        row = bind.execute(
            sa.text(
                "SELECT 1 FROM information_schema.table_constraints "
                "WHERE constraint_schema = 'public' AND constraint_name = :n"
            ),
            {"n": name},
        ).first()
    return row is not None


def _seed_local_owner(bind) -> int:
    """Ensure the system local owner exists; return its user id."""
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
    return int(user_id)


def _local_project_id(bind, user_id: int) -> int:
    return int(bind.execute(
        sa.text(
            "SELECT p.id FROM projects p"
            " JOIN users u ON u.id = p.user_id"
            " WHERE u.email = :email AND p.name = :name"
        ),
        {"email": LOCAL_OWNER_EMAIL, "name": LOCAL_PROJECT_NAME},
    ).scalar_one())


def upgrade() -> None:
    bind = op.get_bind()

    op.create_table(
        "login_attempts",
        sa.Column("ip", sa.Text(), nullable=False),
        sa.Column("window_start", sa.Float(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("ip"),
        if_not_exists=True,
    )

    for table, column, ddl in _ADD_COLUMNS:
        if not _has_column(bind, table, column):
            bind.execute(sa.text(
                f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
            ))

    user_id = _seed_local_owner(bind)
    project_id = _local_project_id(bind, user_id)

    # --- backfill 1: OAuth subjects ---------------------------------
    for table, column in (
        ("oauth_clients", "owner_user_id"),
        ("oauth_codes", "subject_user_id"),
        ("oauth_tokens", "subject_user_id"),
    ):
        bind.execute(sa.text(
            f"UPDATE {table} SET {column} = :u WHERE {column} IS NULL"
        ), {"u": user_id})

    # --- backfill 2: session_pk mapping ------------------------------
    # Every distinct client string still referenced without an owning
    # session gets a local-owned session row (resolve-or-create), then
    # all referencing rows point at its surrogate id.
    bind.execute(sa.text(
        "INSERT INTO sessions (user_id, project_id, client_session_id,"
        " created_at, updated_at)"
        " SELECT :u, :p, refs.session_id, :now, :now FROM ("
        "  SELECT DISTINCT session_id FROM task_states"
        "  WHERE session_pk IS NULL AND session_id IS NOT NULL"
        "  UNION SELECT DISTINCT session_id FROM checkpoints"
        "  WHERE session_pk IS NULL AND session_id IS NOT NULL"
        "  UNION SELECT DISTINCT session_id FROM runs"
        "  WHERE session_pk IS NULL AND session_id IS NOT NULL"
        " ) refs"
        " ON CONFLICT (user_id, project_id, client_session_id) DO NOTHING"
    ), {"u": user_id, "p": project_id, "now": time.time()})

    for table in ("task_states", "checkpoints", "runs"):
        bind.execute(sa.text(
            f"UPDATE {table} t SET session_pk = s.id"
            " FROM sessions s"
            " WHERE s.client_session_id = t.session_id"
            "   AND s.user_id = :u AND s.project_id = :p"
            "   AND t.session_pk IS NULL"
        ), {"u": user_id, "p": project_id})

    # --- constraint swap ---------------------------------------------
    # Drop the string-keyed version uniqueness (collides across owners
    # sharing a client string); enforce per-owner uniqueness via the
    # partial index instead.
    if _has_object(bind, "constraint", "uq_task_states_version"):
        bind.execute(sa.text(
            "ALTER TABLE task_states"
            " DROP CONSTRAINT uq_task_states_version"
        ))
    if not _has_object(bind, "index", "uq_task_states_owner_version"):
        bind.execute(sa.text(
            "CREATE UNIQUE INDEX uq_task_states_owner_version"
            " ON task_states (session_pk, task_key, version DESC)"
            " WHERE session_pk IS NOT NULL"
        ))


def downgrade() -> None:
    bind = op.get_bind()

    if _has_object(bind, "index", "uq_task_states_owner_version"):
        bind.execute(sa.text(
            "DROP INDEX uq_task_states_owner_version"
        ))
    if not _has_object(bind, "constraint", "uq_task_states_version"):
        # Restoring the string-keyed constraint requires that no two rows
        # share (session_id, task_key, version) - true whenever only the
        # local owner ever wrote. If hosted-era data violates it, this
        # fails loudly instead of silently mangling history.
        bind.execute(sa.text(
            "ALTER TABLE task_states"
            " ADD CONSTRAINT uq_task_states_version"
            " UNIQUE (session_id, task_key, version)"
        ))

    for table, column, _ddl in reversed(_ADD_COLUMNS):
        if _has_column(bind, table, column):
            bind.execute(sa.text(
                f"ALTER TABLE {table} DROP COLUMN {column}"
            ))

    op.drop_table("login_attempts", if_exists=True)
