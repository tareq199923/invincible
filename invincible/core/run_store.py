# invincible/core/run_store.py
"""Provider-run records on PostgreSQL (Phase 16).

One row per upstream attempt - successes, failovers, errors. Queryable
execution history feeding the continuity brief, the graph API, and the
future dashboard. Shape carried over from Phase 13.5; ``meta`` is JSONB.

Phase 4 adds token accounting: ``input_tokens`` / ``output_tokens`` carry
real upstream usage where the provider reported it; estimates are flagged
via ``meta["usage_estimated"]`` (see :meth:`RunStore.attach_output`).
"""

import time

from sqlalchemy import text

from invincible.core.db import runs


class RunStore:
    def __init__(self, engine):
        self.engine = engine

    async def init(self) -> None:
        """Schema owned by core.db metadata."""

    async def close(self) -> None:
        """Engine owned/disposed by the lifespan."""

    async def record(self, entry: dict) -> int:
        """Insert one run row; returns the new row id.

        Required keys: request_id, provider_name, model_id, attempt_index,
        outcome, started_at. Optional: session_id, session_pk (Phase 2
        owning surrogate session), error_class, finished_at,
        input_tokens / output_tokens (Phase 4),
        meta (JSON-serializable mapping).
        """
        meta = entry.get("meta")
        async with self.engine.begin() as conn:
            result = await conn.execute(
                runs.insert().values(
                    request_id=entry["request_id"],
                    session_id=entry.get("session_id"),
                    session_pk=entry.get("session_pk"),
                    provider_name=entry["provider_name"],
                    model_id=entry["model_id"],
                    attempt_index=entry["attempt_index"],
                    outcome=entry["outcome"],
                    error_class=entry.get("error_class"),
                    input_tokens=entry.get("input_tokens"),
                    output_tokens=entry.get("output_tokens"),
                    started_at=entry["started_at"],
                    finished_at=entry.get("finished_at"),
                    # JSONB column: bind the object; SQLAlchemy serializes.
                    meta=meta if meta is not None else None,
                )
            )
            return result.inserted_primary_key[0]

    async def attach_output(
        self, *, request_id: str, output_tokens: int, estimated: bool = True,
    ) -> bool:
        """Stamp the streamed-response token estimate onto the winning run
        row (Phase 4). Streaming never sees upstream usage counts without a
        wire change, so the endpoint attaches a chars/4 estimate of what it
        actually accumulated; ``meta.usage_estimated`` records provenance.

        Returns True when a winning 'ok' row was found and updated.
        """
        sql = text(
            "UPDATE runs SET"
            " output_tokens = :out,"
            " meta = jsonb_set(COALESCE(meta, '{}'::jsonb),"
            "                  '{usage_estimated}',"
            "                  to_jsonb(CAST(:est AS BOOLEAN)), true)"
            " WHERE id = (SELECT id FROM runs"
            "             WHERE request_id = :rid AND outcome = 'ok'"
            "             ORDER BY id DESC LIMIT 1)"
        )
        async with self.engine.begin() as conn:
            result = await conn.execute(sql, {
                "out": int(output_tokens),
                "est": bool(estimated),
                "rid": request_id,
            })
            return result.rowcount > 0

    async def recent(
        self, session_id: str | None = None, limit: int = 50,
        *, session_pk: int | None = None,
    ) -> list[dict]:
        """Most recent runs, newest first; optionally scoped to a session.

        ``session_pk`` (Phase 2) scopes to the owning surrogate session -
        the isolation predicate. The loose string filter remains for
        unscoped callers.
        """
        query = runs.select().order_by(runs.c.id.desc()).limit(limit)
        if session_pk is not None:
            query = (
                runs.select()
                .where(runs.c.session_pk == session_pk)
                .order_by(runs.c.id.desc())
                .limit(limit)
            )
        elif session_id is not None:
            query = (
                runs.select()
                .where(runs.c.session_id == session_id)
                .order_by(runs.c.id.desc())
                .limit(limit)
            )
        async with self.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [dict(r) for r in rows]

    async def usage_summary(
        self, user_id: int, *, days: int = 7,
    ) -> list[dict]:
        """Day x provider x model aggregation over ONE owner's runs
        (Phase 5 dashboard usage view).

        Ownership flows entirely through the sessions join - ``runs``
        carry no user column, so ``session_pk IN (owner's sessions)`` IS
        the isolation predicate and pre-Phase-2 rows (``session_pk``
        NULL) can never match. Day buckets are UTC calendar dates of
        ``started_at``; the window covers the last ``days`` days ending
        now (clamped 1..90). Failovers count every non-'ok' attempt.
        """
        since = time.time() - max(1, min(days, 90)) * 86400
        sql = text(
            # AT TIME ZONE 'UTC' pins the calendar bucket to UTC -
            # to_char on a bare timestamptz would render in the
            # session's TimeZone instead.
            "SELECT to_char(to_timestamp(started_at) AT TIME ZONE 'UTC',"
            "                 'YYYY-MM-DD') AS day,"
            "       provider_name, model_id,"
            "       count(*) AS attempts,"
            "       count(*) FILTER (WHERE outcome <> 'ok') AS failovers,"
            "       COALESCE(sum(input_tokens), 0)  AS input_tokens,"
            "       COALESCE(sum(output_tokens), 0) AS output_tokens"
            "   FROM runs"
            "   WHERE session_pk IN"
            "           (SELECT id FROM sessions WHERE user_id = :uid)"
            "     AND started_at >= :since"
            "   GROUP BY day, provider_name, model_id"
            "   ORDER BY day DESC, provider_name, model_id"
        )
        async with self.engine.connect() as conn:
            rows = (await conn.execute(
                sql, {"uid": user_id, "since": since})).mappings().all()
        return [dict(r) for r in rows]


def new_run_entry(
    *,
    request_id: str,
    provider_name: str,
    model_id: str,
    attempt_index: int,
    started_at: float,
    outcome: str,
    session_id: str | None = None,
    session_pk: int | None = None,
    error_class: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    meta: dict | None = None,
) -> dict:
    """Assemble one record with finished_at stamped now."""
    import time

    return {
        "request_id": request_id,
        "session_id": session_id,
        "session_pk": session_pk,
        "provider_name": provider_name,
        "model_id": model_id,
        "attempt_index": attempt_index,
        "outcome": outcome,
        "error_class": error_class,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "started_at": started_at,
        "finished_at": time.time(),
        "meta": meta,
    }
