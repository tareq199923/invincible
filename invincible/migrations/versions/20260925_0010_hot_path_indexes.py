"""Hot-path indexes: runs.request_id and the OAuth expiry/client columns.

Four additive indexes, no shape change. Three of them replace a sequential
scan of a table that only grows; the fourth removes the write-lock window
in the retention path (that part is a code fix, in session_store.py - this
revision is only the schema half).

- ``runs.request_id`` - ``RunStore.attach_output`` runs
  ``WHERE request_id = :rid AND outcome = 'ok'`` on EVERY streamed request
  to stamp the token estimate onto the winning attempt row. With no index
  that is a full scan of ``runs``, which grows with total traffic, so each
  streamed reply gets slower than the last.
- ``oauth_codes.expires_at`` / ``oauth_tokens.expires_at`` - the sweep in
  ``OAuthStore`` deletes ``WHERE expires_at <= now``; both were full scans.
- ``oauth_tokens.client_id`` - ``revoke_client_tokens`` updates by it and
  ``list_active_tokens`` filters by it.

All four are plain ``CREATE INDEX``. That takes a SHARE lock, which blocks
WRITES on the table for the duration but not reads. On a table this size
that is milliseconds; if ``runs`` ever reaches millions of rows, replace
the first statement with ``CREATE INDEX CONCURRENTLY`` inside
``op.get_context().autocommit_block()`` - it cannot run in a transaction,
which is why it is not the default here.

Guarded on information_schema/pg_indexes like earlier revisions so fresh
databases whose lifespan ``create_all`` already built these indexes skip
cleanly, and so re-running is harmless.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-25

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, index name, column list) - the same names and columns core.db
# declares, so a create_all-built database and a migrated one converge.
_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("runs", "idx_runs_request_id", "request_id"),
    ("oauth_codes", "idx_oauth_codes_expires", "expires_at"),
    ("oauth_tokens", "idx_oauth_tokens_expires", "expires_at"),
    ("oauth_tokens", "idx_oauth_tokens_client", "client_id"),
)


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
    for table, name, columns in _INDEXES:
        if not _has_table(bind, table):
            continue
        if _has_index(bind, table, name):
            continue
        bind.execute(sa.text(
            f"CREATE INDEX {name} ON {table} ({columns})"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    for table, name, _columns in _INDEXES:
        if _has_index(bind, table, name):
            bind.execute(sa.text(f"DROP INDEX {name}"))
