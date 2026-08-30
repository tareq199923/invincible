"""Operator role: gate OAuth consent approval on ``users.role``.

Phase 5 let a dashboard session cookie grant OAuth consent as that user,
but registration (``/auth/register``) is open - so any self-registered
account could approve its own client and mint MCP bearer tokens
(execute_bash / write_file on the host) with zero secrets. This revision
adds ``users.role`` ('user' | 'operator', default 'user') and elevates the
system *local* owner to 'operator'. The consent endpoints (Phase 6 hardening)
refuse non-operator sessions; the owner-secret cookie path is unchanged -
possession of the secret already proves operator authority.

Guarded on information_schema like earlier revisions so fresh databases
whose lifespan ``create_all`` already built the new shape skip cleanly;
``server_default 'user'`` backfills every existing row without a rewrite
step. The local-owner UPDATE is idempotent and matches the runtime
bootstrap in ``core.db.seed_local_owner_conn``.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-31

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same literals as core.db (ROLE_OPERATOR / LOCAL_OWNER_EMAIL); spelled
# out so the revision stays standalone against future renames.
LOCAL_OWNER_EMAIL = "local@invincible.local"


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
    if not _has_column(bind, "users", "role"):
        bind.execute(sa.text(
            "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'"
        ))
    # The system local owner is an operator by definition (the owner-secret
    # consent path resolves to it); elevate rows seeded before the column.
    bind.execute(sa.text(
        "UPDATE users SET role = 'operator' WHERE email = :email"
    ), {"email": LOCAL_OWNER_EMAIL})


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "users", "role"):
        bind.execute(sa.text("ALTER TABLE users DROP COLUMN role"))
