"""Make ``uq_turns_session_seq`` DEFERRABLE INITIALLY DEFERRED.

``SessionStore._enforce_retention`` re-sequences a session's remaining
turns densely with ONE statement (``UPDATE turns SET seq = rn - 1 ...``).
PostgreSQL checks a non-deferrable UNIQUE constraint row by row while the
UPDATE runs, so a row moving to a ``seq`` that a not-yet-updated row still
holds raised ``UniqueViolationError`` (session 391, seq 181 in production).
Every later append to a session past the retention cap then failed to
persist its history.

Deferring the check to COMMIT lets the statement shuffle values freely;
the constraint is still enforced, just once the transaction ends. Nothing
uses ``ON CONFLICT`` against this constraint (the only ``on_conflict`` in
``session_store`` targets ``sessions``), so deferring it is safe.

Both directions are idempotent (``DROP ... IF EXISTS``) so a partial run
or a re-run is harmless.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-29

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE turns DROP CONSTRAINT IF EXISTS uq_turns_session_seq"
    )
    op.execute(
        "ALTER TABLE turns ADD CONSTRAINT uq_turns_session_seq "
        "UNIQUE (session_id, seq) DEFERRABLE INITIALLY DEFERRED"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE turns DROP CONSTRAINT IF EXISTS uq_turns_session_seq"
    )
    op.execute(
        "ALTER TABLE turns ADD CONSTRAINT uq_turns_session_seq "
        "UNIQUE (session_id, seq)"
    )
