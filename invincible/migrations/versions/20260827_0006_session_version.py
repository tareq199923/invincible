"""Platform Phase 5 hardening: session invalidation on password change.

``users.session_version`` bumps atomically inside the same UPDATE that
sets ``password_hash`` (both UserService.set_password and
.change_password); signed session cookies embed the version they were
minted against and Principal resolution rejects any mismatch. Result: a
cookie issued before a password change stops verifying immediately after
the change instead of surviving up to the 30-day TTL.

Guarded on information_schema like earlier revisions so fresh databases
whose lifespan ``create_all`` already built the new shape skip cleanly;
``server_default 0`` backfills every existing row without a rewrite step.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-27

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
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
    if not _has_column(bind, "users", "session_version"):
        bind.execute(sa.text(
            "ALTER TABLE users ADD COLUMN session_version INTEGER "
            "NOT NULL DEFAULT 0"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "users", "session_version"):
        bind.execute(sa.text(
            "ALTER TABLE users DROP COLUMN session_version"
        ))
