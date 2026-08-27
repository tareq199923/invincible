"""Platform Phase 9: per-user BYOK provider credentials.

``user_provider_credentials`` stores each user's own provider connections
(catalog entry or fully custom) with the API key encrypted at rest via
core/credential_crypto.py (Fernet over INVINCIBLE_CREDENTIAL_KEY). Plain-
text keys are never persisted; ``key_masked`` is a one-way display hint
computed once at create. Unique (user_id, provider_name) keeps one row per
labelled connection.

Guarded on information_schema like earlier revisions so fresh databases
whose lifespan ``create_all`` already built the new shape skip cleanly.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-27

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
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
    if _has_table(op.get_bind(), "user_provider_credentials"):
        return
    op.create_table(
        "user_provider_credentials",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "user_id", sa.BigInteger(),
            sa.ForeignKey("users.id"), nullable=False,
        ),
        sa.Column("provider_name", sa.Text(), nullable=False),
        sa.Column("catalog_key", sa.Text(), nullable=True),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("encrypted_api_key", sa.LargeBinary(), nullable=False),
        sa.Column(
            "key_masked", sa.Text(), nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "status", sa.Text(), nullable=False,
            server_default=sa.text("'untested'"),
        ),
        sa.Column("last_tested_at", sa.Float(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "provider_name",
            name="uq_user_provider_credentials_user_name",
        ),
    )
    op.create_index(
        "idx_user_provider_credentials_user",
        "user_provider_credentials",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "user_provider_credentials"):
        op.drop_index(
            "idx_user_provider_credentials_user",
            table_name="user_provider_credentials",
        )
        op.drop_table("user_provider_credentials")
