"""Platform Phase 3: accounts schema.

- ``projects.archived_at``: nullable soft-archive timestamp (rows kept).
- ``login_attempts.scope``: separates owner-consent lockout counters from
  /auth/login counters; primary key widens from (ip) to (ip, scope).
- New ``user_identities``: external identity links (GitHub), unique per
  (provider, provider_account_id).
- New ``device_codes``: RFC 8628-style pairing state (hashed device_code
  PK, short human-typed user_code).

Like earlier revisions, every step guards on information_schema so it is
a clean pass over fresh databases whose lifespan ``create_all`` already
built the new shape.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-26

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
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


def _has_table(bind, table: str) -> bool:
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = :t"
        ),
        {"t": table},
    ).first()
    return row is not None


def _pk_columns(bind, table: str) -> list[str]:
    return [
        str(row[0])
        for row in bind.execute(sa.text(
            "SELECT kcu.column_name"
            " FROM information_schema.table_constraints tc"
            " JOIN information_schema.key_column_usage kcu"
            "   ON tc.constraint_name = kcu.constraint_name"
            "  AND tc.constraint_schema = kcu.constraint_schema"
            " WHERE tc.constraint_type = 'PRIMARY KEY'"
            "   AND tc.table_schema = 'public'"
            "   AND tc.table_name = :t"
            " ORDER BY kcu.ordinal_position"
        ), {"t": table}).fetchall()
    ]


def upgrade() -> None:
    bind = op.get_bind()

    # --- projects.archived_at ----------------------------------------
    if not _has_column(bind, "projects", "archived_at"):
        bind.execute(sa.text("ALTER TABLE projects ADD COLUMN archived_at FLOAT"))

    # --- login_attempts scope + widened PK ----------------------------
    if _has_table(bind, "login_attempts"):
        if not _has_column(bind, "login_attempts", "scope"):
            # Existing rows are all owner-consent counters.
            bind.execute(sa.text(
                "ALTER TABLE login_attempts ADD COLUMN scope TEXT"
                " NOT NULL DEFAULT 'owner'"
            ))
        if set(_pk_columns(bind, "login_attempts")) != {"ip", "scope"}:
            pk_name = bind.execute(sa.text(
                "SELECT tc.constraint_name FROM"
                " information_schema.table_constraints tc"
                " WHERE tc.constraint_type = 'PRIMARY KEY'"
                "   AND tc.table_schema = 'public'"
                "   AND tc.table_name = 'login_attempts'"
            )).scalar()
            if pk_name and set(_pk_columns(bind, "login_attempts")) == {"ip"}:
                bind.execute(sa.text(
                    f'ALTER TABLE login_attempts DROP CONSTRAINT "{pk_name}"'
                ))
                bind.execute(sa.text(
                    "ALTER TABLE login_attempts"
                    " ADD CONSTRAINT pk_login_attempts_ip_scope"
                    " PRIMARY KEY (ip, scope)"
                ))

    # --- new tables ----------------------------------------------------
    op.create_table(
        "user_identities",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_account_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "provider_account_id",
            name="uq_user_identities_provider_account",
        ),
        if_not_exists=True,
    )
    bind.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS idx_user_identities_user"
        " ON user_identities (user_id)"
    ))

    op.create_table(
        "device_codes",
        sa.Column("device_code_hash", sa.Text(), nullable=False),
        sa.Column("user_code", sa.Text(), nullable=False),
        sa.Column("subject_user_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False,
                  server_default="pending"),
        sa.Column("interval_seconds", sa.Float(), nullable=False,
                  server_default="5"),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("last_poll_at", sa.Float(), nullable=True),
        sa.Column("resolved_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["subject_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("device_code_hash"),
        sa.UniqueConstraint("user_code"),
        if_not_exists=True,
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_table("device_codes", if_exists=True)
    if _has_table(bind, "user_identities"):
        bind.execute(sa.text(
            "DROP INDEX IF EXISTS idx_user_identities_user"
        ))
        op.drop_table("user_identities")

    if _has_table(bind, "login_attempts"):
        if set(_pk_columns(bind, "login_attempts")) == {"ip", "scope"}:
            bind.execute(sa.text(
                "ALTER TABLE login_attempts"
                " DROP CONSTRAINT pk_login_attempts_ip_scope"
            ))
            # Collisions on bare ip cannot exist while the (ip, scope) PK
            # holds with only 'owner'-scoped rows written by <=0003 code;
            # 0004-era auth-login rows collapse into one representative row
            # per ip (count preserved for the owner realm, which owns the
            # legacy constraint).
            bind.execute(sa.text(
                "DELETE FROM login_attempts a USING login_attempts b"
                " WHERE a.ip = b.ip AND a.scope > b.scope"
            ))
            bind.execute(sa.text(
                "ALTER TABLE login_attempts"
                " ADD CONSTRAINT login_attempts_pkey PRIMARY KEY (ip)"
            ))
        if _has_column(bind, "login_attempts", "scope"):
            bind.execute(sa.text(
                "ALTER TABLE login_attempts DROP COLUMN scope"
            ))

    if _has_column(bind, "projects", "archived_at"):
        bind.execute(sa.text(
            "ALTER TABLE projects DROP COLUMN archived_at"
        ))
