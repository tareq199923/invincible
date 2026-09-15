"""Phase 1 self-service gateway: per-user routing chains and settings.

Two additive changes:

- ``user_settings`` - one row per user holding their routing mode
  ({"mode": "auto"} | {"mode": "pinned", ...} | {"mode": "chain", ...},
  referencing their own ``user_provider_credentials`` ids) and their
  request-shaping overrides (memory / continuity / compression / relay /
  history_max_turns). Missing keys fall through to the env defaults, so
  the server_default '{}' rows behave exactly like the pre-Phase-1 world.
- ``user_provider_credentials.sort_order`` - the user's own ordering of
  their credentials (the pre-Phase-1 routing order was created_at, id,
  so the backfill freezes that order into the new column).

Guarded on information_schema like earlier revisions so fresh databases
whose lifespan ``create_all`` already built the new shape skip cleanly.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-15

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
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
    if not _has_table(bind, "user_settings"):
        bind.execute(sa.text(
            "CREATE TABLE user_settings ("
            " user_id BIGINT NOT NULL,"
            " routing JSONB NOT NULL DEFAULT '{}'::jsonb,"
            " overrides JSONB NOT NULL DEFAULT '{}'::jsonb,"
            " updated_at DOUBLE PRECISION NOT NULL,"
            " CONSTRAINT pk_user_settings PRIMARY KEY (user_id),"
            " CONSTRAINT fk_user_settings_user FOREIGN KEY (user_id)"
            " REFERENCES users (id)"
            ")"
        ))
    if not _has_column(bind, "user_provider_credentials", "sort_order"):
        bind.execute(sa.text(
            "ALTER TABLE user_provider_credentials "
            "ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
        ))
        # Freeze the historical (created_at, id) routing order into the
        # new column so the first render after upgrade matches what
        # users' requests did yesterday.
        bind.execute(sa.text(
            "UPDATE user_provider_credentials AS upc "
            "SET sort_order = ranked.rn - 1 FROM ("
            "  SELECT id, ROW_NUMBER() OVER ("
            "    ORDER BY created_at, id) AS rn"
            "  FROM user_provider_credentials"
            ") AS ranked WHERE ranked.id = upc.id"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "user_provider_credentials", "sort_order"):
        bind.execute(sa.text(
            "ALTER TABLE user_provider_credentials DROP COLUMN sort_order"
        ))
    if _has_table(bind, "user_settings"):
        bind.execute(sa.text("DROP TABLE user_settings"))
