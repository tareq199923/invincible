"""Add ``sessions.title`` and ``sessions.pinned`` (dashboard sidebar).

The chat-first shell lets an account rename a conversation (a custom label
that outranks the derived first-user-message title) and pin it to the top
of the sidebar. Both are per-session presentation state, so they live on
the ``sessions`` row rather than in a side table:

* ``title``  TEXT NULL - NULL means "keep deriving the label from the
  first user message"; clearing a custom name writes NULL back.
* ``pinned`` BOOLEAN NOT NULL DEFAULT false - PostgreSQL orders ``DESC``
  as true-first, which is exactly the sidebar's sort.

Both directions are idempotent (``IF EXISTS`` / ``IF NOT EXISTS``) so a
partial run or a re-run is harmless, and the shapes match ``core.db``
metadata exactly so a ``create_all``-built database converges with a
migrated one.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-30

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS title TEXT")
    op.execute(
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS pinned BOOLEAN "
        "NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS pinned")
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS title")
