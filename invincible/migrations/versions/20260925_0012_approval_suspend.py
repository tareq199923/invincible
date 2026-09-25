"""Harness H5: durable-approval columns on pending_actions.

Two additive nullable columns for the slow-path ApprovalStore
(Hendrixer approvals.ts parity):

- ``suspended_workflow_id`` - the workflow parked awaiting this human
  decision (NULL = fast-path confirm_action token, unchanged behavior).
- ``deadline`` - absolute epoch seconds; past-deadline rows resolve as
  unknown/expired, indistinguishably from a wrong guess.

Fast-path rows keep both NULL, and the slow path refuses NULL-workflow
rows, so the two approval paths can never resolve each other's tokens.

Guarded on information_schema like earlier revisions so fresh databases
whose lifespan ``create_all`` already built the columns skip cleanly.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-25

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | None = "0011"
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


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "pending_actions", "suspended_workflow_id"):
        bind.execute(sa.text(
            "ALTER TABLE pending_actions "
            "ADD COLUMN suspended_workflow_id TEXT"
        ))
    if not _has_column(bind, "pending_actions", "deadline"):
        bind.execute(sa.text(
            "ALTER TABLE pending_actions ADD COLUMN deadline DOUBLE PRECISION"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "pending_actions", "deadline"):
        bind.execute(sa.text(
            "ALTER TABLE pending_actions DROP COLUMN deadline"
        ))
    if _has_column(bind, "pending_actions", "suspended_workflow_id"):
        bind.execute(sa.text(
            "ALTER TABLE pending_actions DROP COLUMN suspended_workflow_id"
        ))
