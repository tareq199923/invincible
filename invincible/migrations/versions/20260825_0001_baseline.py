"""Phase 16 baseline: full PostgreSQL schema.

Mirrors ``invincible.core.db.metadata`` table-for-table and column-for-
column - a drift test compares this revision's output against create_all.

Every CREATE is ``IF NOT EXISTS`` (and every DROP ``IF EXISTS``) so running
``invincible db upgrade`` over a database bootstrapped by lifespan
``create_all`` succeeds and simply starts migration tracking - the normal
adoption path for pre-existing Phase 16 dev databases.

Revision ID: 0001
Revises:
Create Date: 2026-08-25

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("session_id"),
        if_not_exists=True,
    )
    op.create_table(
        "turns",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "session_id", sa.Text(),
            sa.ForeignKey("sessions.session_id"), nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "seq", name="uq_turns_session_seq"),
        if_not_exists=True,
    )
    op.create_index(
        "idx_turns_session", "turns", ["session_id", "seq"],
        unique=False, if_not_exists=True,
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "turn_id", sa.BigInteger(),
            sa.ForeignKey("turns.id"), nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("turn_id", "seq", name="uq_messages_turn_seq"),
        if_not_exists=True,
    )
    op.create_index(
        "idx_messages_turn", "messages", ["turn_id", "seq"],
        unique=False, if_not_exists=True,
    )
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
        if_not_exists=True,
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("provider_name", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("attempt_index", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("error_class", sa.Text(), nullable=True),
        sa.Column("started_at", sa.Float(), nullable=False),
        sa.Column("finished_at", sa.Float(), nullable=True),
        sa.Column("meta", postgresql.JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )
    op.create_index(
        "idx_runs_session", "runs", ["session_id", "started_at"],
        unique=False, if_not_exists=True,
    )
    op.create_index(
        "idx_runs_outcome", "runs", ["outcome"],
        unique=False, if_not_exists=True,
    )
    op.create_table(
        "task_states",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column(
            "task_key", sa.Text(), nullable=False, server_default="default"
        ),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default="active"
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id", "task_key", "version",
            name="uq_task_states_version",
        ),
        if_not_exists=True,
    )
    op.create_index(
        "idx_task_states_session", "task_states",
        ["session_id", "task_key", sa.text("version DESC")],
        unique=False, if_not_exists=True,
    )
    op.create_table(
        "checkpoints",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column(
            "task_key", sa.Text(), nullable=False, server_default="default"
        ),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )
    op.create_index(
        "idx_checkpoints_session", "checkpoints",
        ["session_id", "task_key", sa.text("created_at DESC")],
        unique=False, if_not_exists=True,
    )
    op.create_table(
        "oauth_clients",
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column(
            "client_name", sa.String(), nullable=False, server_default=""
        ),
        sa.Column("redirect_uris", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("client_id"),
        if_not_exists=True,
    )
    op.create_table(
        "oauth_codes",
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("redirect_uri", sa.String(), nullable=False),
        sa.Column("code_challenge", sa.String(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column(
            "used", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.PrimaryKeyConstraint("code"),
        if_not_exists=True,
    )
    op.create_table(
        "oauth_tokens",
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("token_type", sa.String(), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column(
            "revoked", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("token_hash"),
        if_not_exists=True,
    )
    op.create_table(
        "pending_actions",
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("args", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("token"),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("pending_actions", if_exists=True)
    op.drop_table("oauth_tokens", if_exists=True)
    op.drop_table("oauth_codes", if_exists=True)
    op.drop_table("oauth_clients", if_exists=True)
    op.drop_index(
        "idx_checkpoints_session", table_name="checkpoints", if_exists=True)
    op.drop_table("checkpoints", if_exists=True)
    op.drop_index(
        "idx_task_states_session", table_name="task_states", if_exists=True)
    op.drop_table("task_states", if_exists=True)
    op.drop_index("idx_runs_outcome", table_name="runs", if_exists=True)
    op.drop_index("idx_runs_session", table_name="runs", if_exists=True)
    op.drop_table("runs", if_exists=True)
    op.drop_table("facts", if_exists=True)
    op.drop_index("idx_messages_turn", table_name="messages", if_exists=True)
    op.drop_table("messages", if_exists=True)
    op.drop_index("idx_turns_session", table_name="turns", if_exists=True)
    op.drop_table("turns", if_exists=True)
    op.drop_table("sessions", if_exists=True)
