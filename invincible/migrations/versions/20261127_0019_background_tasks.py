"""Background tasks Step 5: restart-safe task records.

Additive table:

- ``background_tasks`` - one metadata-only row per background task:
  owner (``user_id``), best-effort session reference (``session_pk``),
  mode, status, and epoch timestamps. The task text, commands, paths,
  tool args, answers, and free-form error text are never stored here;
  the text lives in the chat session whose id IS the workflow id.

``session_pk`` is nullable because the row is inserted before the runner
resolves/creates the session, and ``ON DELETE SET NULL`` keeps the task
record when a session is hard-deleted. Statuses are validated in
``core/task_records.py`` (the schema uses no CHECK constraints, matching
``core/db.py``).

Guarded on information_schema like earlier revisions so a fresh database
whose lifespan ``create_all`` already built the table skips cleanly.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-27

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "background_tasks"
USER_INDEX = "idx_bg_tasks_user_created"
STATUS_INDEX = "idx_bg_tasks_status"


def _has_table(bind, table: str) -> bool:
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = :t"
        ),
        {"t": table},
    ).first()
    return row is not None


def _has_index(bind, table: str, name: str) -> bool:
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM pg_indexes "
            "WHERE schemaname = 'public' AND tablename = :t "
            "AND indexname = :n"
        ),
        {"t": table, "n": name},
    ).first()
    return row is not None


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, TABLE):
        bind.execute(sa.text(
            "CREATE TABLE background_tasks ("
            " workflow_id TEXT NOT NULL,"
            " user_id BIGINT NOT NULL REFERENCES users(id),"
            " session_pk BIGINT NULL REFERENCES sessions(id)"
            "  ON DELETE SET NULL,"
            " mode TEXT NOT NULL,"
            " status TEXT NOT NULL,"
            " created_at DOUBLE PRECISION NOT NULL,"
            " started_at DOUBLE PRECISION NOT NULL,"
            " finished_at DOUBLE PRECISION,"
            " CONSTRAINT background_tasks_pkey PRIMARY KEY (workflow_id)"
            ")"
        ))
    if _has_table(bind, TABLE) and not _has_index(
        bind, TABLE, USER_INDEX
    ):
        bind.execute(sa.text(
            f"CREATE INDEX {USER_INDEX}"
            " ON background_tasks (user_id, created_at DESC)"
        ))
    if _has_table(bind, TABLE) and not _has_index(
        bind, TABLE, STATUS_INDEX
    ):
        bind.execute(sa.text(
            f"CREATE INDEX {STATUS_INDEX} ON background_tasks (status)"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_index(bind, TABLE, USER_INDEX):
        bind.execute(sa.text(f"DROP INDEX {USER_INDEX}"))
    if _has_index(bind, TABLE, STATUS_INDEX):
        bind.execute(sa.text(f"DROP INDEX {STATUS_INDEX}"))
    if _has_table(bind, TABLE):
        bind.execute(sa.text(f"DROP TABLE {TABLE}"))
