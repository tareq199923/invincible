"""Drop the legacy per-session ``facts`` triple store.

Superseded by ``memories`` in Phase 4, writerless since the legacy SQLite
importer was removed 2026-09-24, audited EMPTY on production 2026-09-25
(backed up to invincible-facts-backup-20260925.csv before this revision
was written). Nothing in request-serving code reads or writes it; the
``extract_facts`` memory extractor feeds ``memories``, not this table.

The DROP is guarded on information_schema like earlier revisions so
fresh databases whose lifespan ``create_all`` never built the table skip
cleanly, and so re-running is harmless. The downgrade recreates the
exact baseline shape (columns + ``uq_facts_triple``) for rollback only -
it restores structure, not rows.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-25

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(bind, table: str) -> bool:
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = :t"
        ),
        {"t": table},
    ).first()
    return row is not None


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "facts"):
        bind.execute(sa.text("DROP TABLE facts"))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "facts"):
        return
    op.create_table(
        "facts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "user_id", sa.Text(), nullable=False, server_default="default"
        ),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("relation", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "session_id", "entity", "relation", "target",
            name="uq_facts_triple",
        ),
    )
