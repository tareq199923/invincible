"""Typo-tolerant memory retrieval: pg_trgm + a content trigram index.

The lexical fallback can miss a query term that is a near-miss spelling
(``postgress`` vs. stored ``PostgreSQL``). Revision 0016 installs the
trusted ``pg_trgm`` extension and a GIN trigram index over
``memories.content``; RetrievalService uses ``word_similarity`` / ``<%``
only in its no-exact-match OR stage.

The extension is database-scoped and deliberately left in place on
downgrade: other objects may depend on it, and dropping it would require
``CASCADE``. The index is the only thing this revision owns, so the
downgrade drops just that.

Both directions are idempotent (``IF EXISTS`` / ``IF NOT EXISTS``), and
the metadata index in ``core.db`` has the same shape, so a
``create_all``-built database converges with a migrated one. ``ANALYZE
memories`` refreshes planner statistics after the index build so a
freshly upgraded large table does not choose a cold full scan.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-21

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from invincible.core.db import ensure_pg_trgm_extension

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "idx_memories_content_trgm"


def upgrade() -> None:
    bind = op.get_bind()
    # Fails with an actionable message when pg_trgm is unavailable or the
    # migration role cannot install it (see core.db).
    ensure_pg_trgm_extension(bind)
    bind.execute(sa.text(
        f"CREATE INDEX IF NOT EXISTS {_INDEX_NAME}"
        " ON memories USING gin (content gin_trgm_ops)"
    ))
    # Refresh planner statistics so the new trigram index is chosen
    # immediately on a freshly upgraded database (the first cold plan can
    # otherwise fall back to a full scan on a large table).
    bind.execute(sa.text("ANALYZE memories"))


def downgrade() -> None:
    # Leave the extension installed; only the index belongs to this revision.
    op.execute(f"DROP INDEX IF EXISTS {_INDEX_NAME}")
