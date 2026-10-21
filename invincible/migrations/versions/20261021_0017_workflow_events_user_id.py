"""Harness H6c: per-account ownership on workflow_events.

Adds a nullable ``user_id`` column and a ``(user_id, created_at)`` index
so the dashboard reads one account's timelines without scanning every
row. Existing rows are backfilled from ``sessions.client_session_id``
when the workflow id maps to a session; rows that cannot be mapped stay
NULL and are never shown.

Guarded on information_schema like earlier revisions so a fresh database
whose lifespan ``create_all`` already built the column skips cleanly.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-21

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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
    if not _has_column(bind, "workflow_events", "user_id"):
        bind.execute(sa.text(
            "ALTER TABLE workflow_events "
            "ADD COLUMN user_id BIGINT REFERENCES users(id)"
        ))
    # Backfill before the index so the one-time write pays for itself
    # once; every later read uses the (user_id, created_at) index.
    bind.execute(sa.text(
        "UPDATE workflow_events AS we "
        "SET user_id = s.user_id "
        "FROM sessions AS s "
        "WHERE we.user_id IS NULL "
        "AND we.workflow_id = s.client_session_id"
    ))
    if not _has_index(bind, "workflow_events", "idx_workflow_events_user"):
        bind.execute(sa.text(
            "CREATE INDEX idx_workflow_events_user "
            "ON workflow_events (user_id, created_at)"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_index(bind, "workflow_events", "idx_workflow_events_user"):
        bind.execute(sa.text(
            "DROP INDEX idx_workflow_events_user"
        ))
    if _has_column(bind, "workflow_events", "user_id"):
        bind.execute(sa.text(
            "ALTER TABLE workflow_events DROP COLUMN user_id"
        ))
