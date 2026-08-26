"""Platform Phase 4: usage tokens on runs + memory lexical retrieval.

- ``runs.input_tokens`` / ``runs.output_tokens``: nullable token counts per
  upstream attempt. Populated with real provider usage where available;
  streaming estimates are flagged via ``meta["usage_estimated"]``.
- ``memories.search_vector``: stored generated ``tsvector`` over
  ``content``, plus a GIN index - the match path for RetrievalService.
  The generation expression uses the same regconfig constant as the
  metadata and query layer (``core.db.MEMORY_FTS_CONFIG``); a mismatch
  would silently bypass the index.

Like earlier revisions, every step guards on information_schema so it is
a clean pass over fresh databases whose lifespan ``create_all`` already
built the new shape.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-26

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from invincible.core.db import MEMORY_FTS_CONFIG

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
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


def _has_index(bind, name: str) -> bool:
    row = bind.execute(
        sa.text("SELECT 1 FROM pg_indexes WHERE indexname = :n"),
        {"n": name},
    ).first()
    return row is not None


def upgrade() -> None:
    bind = op.get_bind()

    # --- runs token columns -------------------------------------------
    if not _has_column(bind, "runs", "input_tokens"):
        bind.execute(sa.text("ALTER TABLE runs ADD COLUMN input_tokens INTEGER"))
    if not _has_column(bind, "runs", "output_tokens"):
        bind.execute(sa.text("ALTER TABLE runs ADD COLUMN output_tokens INTEGER"))

    # --- memories FTS ---------------------------------------------------
    if not _has_column(bind, "memories", "search_vector"):
        bind.execute(sa.text(
            "ALTER TABLE memories ADD COLUMN search_vector tsvector"
            " GENERATED ALWAYS AS"
            f" (to_tsvector('{MEMORY_FTS_CONFIG}', content)) STORED"
        ))
    if not _has_index(bind, "idx_memories_search"):
        bind.execute(sa.text(
            "CREATE INDEX idx_memories_search"
            " ON memories USING gin (search_vector)"
        ))


def downgrade() -> None:
    bind = op.get_bind()

    if _has_index(bind, "idx_memories_search"):
        bind.execute(sa.text("DROP INDEX idx_memories_search"))
    if _has_column(bind, "memories", "search_vector"):
        bind.execute(sa.text(
            "ALTER TABLE memories DROP COLUMN search_vector"
        ))

    if _has_column(bind, "runs", "output_tokens"):
        bind.execute(sa.text("ALTER TABLE runs DROP COLUMN output_tokens"))
    if _has_column(bind, "runs", "input_tokens"):
        bind.execute(sa.text("ALTER TABLE runs DROP COLUMN input_tokens"))
