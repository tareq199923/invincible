# invincible/core/db_import.py
"""One-shot legacy SQLite -> PostgreSQL importer (Phase 16).

Reads a Phase <= 15 ``sessions.db`` (aiosqlite-era schema) with the stdlib
``sqlite3`` module in read-only mode and copies every row into the Phase 16
PostgreSQL tables, preserving primary keys so foreign keys stay intact:

- ``sessions_v2`` -> ``sessions`` (``turns``/``messages`` keep their names)
- ``facts`` -> ``facts``
- ``oauth_clients`` / ``oauth_codes`` / ``oauth_tokens`` -> same names
  (redirect_uris JSON text becomes JSONB, used/revoked ints become bools)

Pending actions are deliberately NOT imported: staged actions expire after
10 minutes by design, so importing them would only resurrect stale rows.

Existing target rows are never overwritten (``on_conflict_do_nothing``);
after inserting explicit identity values, the sequences are re-synced so
future appends continue past the imported max(id). The whole import runs
in one transaction: either the legacy database lands completely or not at
all.
"""
import json
import sqlite3

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from invincible.core.db import (
    facts,
    messages,
    oauth_clients,
    oauth_codes,
    oauth_tokens,
    sessions,
    turns,
)


def _table_exists(legacy: sqlite3.Connection, name: str) -> bool:
    row = legacy.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return bool(row)


def _rows(legacy: sqlite3.Connection, query: str) -> list[tuple]:
    return legacy.execute(query).fetchall()


async def import_legacy_sqlite(engine, sqlite_path: str) -> dict[str, int]:
    """Import all legacy rows into PG; returns per-table counts of rows
    actually inserted (existing target rows are skipped, not overwritten)."""
    counts: dict[str, int] = {}
    legacy = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        async with engine.begin() as conn:

            async def _insert(table, columns: tuple, rows: list[tuple]) -> int:
                if not rows:
                    return 0
                values = [dict(zip(columns, row, strict=True)) for row in rows]
                result = await conn.execute(
                    pg_insert(table)
                    .values(values)
                    # Bare ON CONFLICT DO NOTHING: skip rows that collide on
                    # ANY unique constraint (natural keys like uq_facts_triple
                    # or uq_turns_session_seq, not just the surrogate PK).
                    .on_conflict_do_nothing()
                )
                return result.rowcount or 0

            has_sessions = _table_exists(legacy, "sessions_v2")
            has_turns = _table_exists(legacy, "turns")
            has_messages = _table_exists(legacy, "messages")

            counts["sessions"] = await _insert(
                sessions,
                ("session_id", "created_at", "updated_at"),
                _rows(
                    legacy,
                    "SELECT session_id, created_at, updated_at "
                    "FROM sessions_v2",
                ) if has_sessions else [],
            )

            turn_rows = (
                _rows(legacy, "SELECT id, session_id, seq FROM turns")
                if has_turns else []
            )
            counts["turns"] = await _insert(
                turns, ("id", "session_id", "seq"), turn_rows
            )

            message_rows = (
                [
                    (mid, turn_id, seq, role, json.loads(payload))
                    for mid, turn_id, seq, role, payload in _rows(
                        legacy,
                        "SELECT id, turn_id, seq, role, payload "
                        "FROM messages",
                    )
                ]
                if has_messages else []
            )
            counts["messages"] = await _insert(
                messages,
                ("id", "turn_id", "seq", "role", "payload"),
                message_rows,
            )

            counts["facts"] = await _insert(
                facts,
                ("user_id", "session_id", "entity", "relation", "target",
                 "created_at"),
                _rows(
                    legacy,
                    "SELECT user_id, session_id, entity, relation, target,"
                    " created_at FROM facts",
                ) if _table_exists(legacy, "facts") else [],
            )

            counts["oauth_clients"] = await _insert(
                oauth_clients,
                ("client_id", "client_name", "redirect_uris", "created_at"),
                [
                    (cid, name, json.loads(uris), created)
                    for cid, name, uris, created in _rows(
                        legacy,
                        "SELECT client_id, client_name, redirect_uris,"
                        " created_at FROM oauth_clients",
                    )
                ] if _table_exists(legacy, "oauth_clients") else [],
            )

            counts["oauth_codes"] = await _insert(
                oauth_codes,
                ("code", "client_id", "redirect_uri", "code_challenge",
                 "expires_at", "used"),
                [
                    (code, cid, uri, challenge, expires, bool(used))
                    for code, cid, uri, challenge, expires, used in _rows(
                        legacy,
                        "SELECT code, client_id, redirect_uri,"
                        " code_challenge, expires_at, used FROM oauth_codes",
                    )
                ] if _table_exists(legacy, "oauth_codes") else [],
            )

            counts["oauth_tokens"] = await _insert(
                oauth_tokens,
                ("token_hash", "token_type", "client_id", "expires_at",
                 "revoked", "created_at"),
                [
                    (thash, ttype, cid, expires, bool(revoked), created)
                    for thash, ttype, cid, expires, revoked, created in _rows(
                        legacy,
                        "SELECT token_hash, token_type, client_id,"
                        " expires_at, revoked, created_at FROM oauth_tokens",
                    )
                ] if _table_exists(legacy, "oauth_tokens") else [],
            )

            # Identity sequences must skip past the explicit ids we just
            # inserted, or the next implicit insert collides. setval(v, false)
            # makes v itself the next value.
            for table in ("turns", "messages", "facts"):
                await conn.execute(text(
                    f"SELECT setval("
                    f" pg_get_serial_sequence('{table}', 'id'),"
                    f" GREATEST(COALESCE((SELECT MAX(id) FROM {table}), 0)"
                    f" + 1, 1),"
                    f" false)"
                ))
    finally:
        legacy.close()
    return counts
