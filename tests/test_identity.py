# tests/test_identity.py
"""Platform Phase 1 identity primitives and API-key lifecycle.

argon2id password hashing, inv_-prefixed key minting (sha256 at rest,
visible prefix), resolve/revoke semantics with last-used telemetry, the
default-project get-or-create, and audit-log rows. Hermetic except where
pg_engine is required (storage paths).
"""
import pytest

from invincible.core.db import (
    LOCAL_OWNER_EMAIL,
    LOCAL_PROJECT_NAME,
    ensure_local_owner,
)
from invincible.core.db import (
    projects as projects_table,
)
from invincible.core.db import (
    users as users_table,
)
from invincible.core.identity import (
    API_KEY_PREFIX,
    ApiKeyStore,
    AuditLog,
    ensure_default_project,
    generate_api_key,
    hash_password,
    verify_password,
)

# --- password hashing (pure) --------------------------------------------------


def test_argon2id_roundtrip_and_rejection():
    hashed = hash_password("correct horse")
    assert hashed != "correct horse"
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, "correct horse") is True
    assert verify_password(hashed, "wrong horse") is False


def test_verify_password_handles_null_and_garbage():
    assert verify_password(None, "anything") is False
    assert verify_password("", "anything") is False
    assert verify_password("not-a-hash", "anything") is False


# --- key minting (pure) --------------------------------------------------------


def test_generate_api_key_shape_and_uniqueness():
    raw, key_hash, prefix = generate_api_key()
    assert raw.startswith(API_KEY_PREFIX)
    assert len(raw) > len(API_KEY_PREFIX) + 20
    # prefix is a visible head of the raw value
    assert raw.startswith(prefix)
    assert prefix == API_KEY_PREFIX + raw[len(API_KEY_PREFIX):][:8]
    import hashlib

    assert key_hash == hashlib.sha256(raw.encode()).hexdigest()
    # no raw material leaks into the hash-adjacent fields
    assert raw not in key_hash

    other = generate_api_key()
    assert other[0] != raw and other[1] != key_hash


# --- storage-backed lifecycle ---------------------------------------------------


@pytest.fixture
async def keys(pg_engine):
    return ApiKeyStore(engine=pg_engine)


async def _new_user(pg_engine, email="user@example.com") -> int:
    async with pg_engine.begin() as conn:
        uid = (await conn.execute(
            users_table.insert()
            .values(email=email, created_at=1.0)
            .returning(users_table.c.id)
        )).scalar_one()
    return int(uid)


async def test_ensure_local_owner_is_idempotent(pg_engine):
    first = await ensure_local_owner(pg_engine)
    second = await ensure_local_owner(pg_engine)
    assert first == second
    async with pg_engine.connect() as conn:
        users = (await conn.execute(users_table.select())).all()
        projects = (await conn.execute(projects_table.select())).all()
    assert [(u.email, bool(u.is_system)) for u in users] == [
        (LOCAL_OWNER_EMAIL, True)
    ]
    assert [(p.name, bool(p.is_default)) for p in projects] == [
        (LOCAL_PROJECT_NAME, True)
    ]


async def test_api_key_lifecycle(pg_engine, keys):
    uid = await _new_user(pg_engine)
    record = await keys.create(uid, label="cli")
    assert record["raw"].startswith(API_KEY_PREFIX)

    resolved = await keys.resolve(record["raw"])
    assert resolved is not None
    assert resolved == {"id": record["id"], "user_id": uid}

    # wrong/foreign tokens never resolve
    assert await keys.resolve(record["raw"] + "x") is None
    assert await keys.resolve("not-even-prefixed") is None

    assert await keys.revoke(record["id"]) is True
    # idempotent: revoking again reports nothing newly revoked
    assert await keys.revoke(record["id"]) is False
    assert await keys.resolve(record["raw"]) is None


async def test_revoked_keys_excluded_from_resolution(pg_engine, keys):
    uid = await _new_user(pg_engine)
    a = await keys.create(uid)
    b = await keys.create(uid)
    await keys.revoke(a["prefix"])
    assert await keys.resolve(a["raw"]) is None
    assert await keys.resolve(b["raw"]) == {
        "id": b["id"], "user_id": uid,
    }


async def test_last_used_at_touched_on_resolve(pg_engine, keys):
    uid = await _new_user(pg_engine)
    record = await keys.create(uid)
    rows = await keys.list(user_id=uid)
    assert rows[0]["last_used_at"] is None
    await keys.resolve(record["raw"])
    rows = await keys.list(user_id=uid)
    assert rows[0]["last_used_at"] is not None


async def test_list_never_leaks_raw_or_hash(pg_engine, keys):
    uid = await _new_user(pg_engine)
    record = await keys.create(uid, label="secret-label")
    listed = await keys.list()
    assert len(listed) == 1
    entry = listed[0]
    assert record["raw"] not in json_dumps(entry)
    assert "key_hash" not in entry
    assert entry["prefix"] == record["prefix"]
    assert entry["label"] == "secret-label"


def json_dumps(obj) -> str:
    import json

    return json.dumps(obj, default=str)


async def test_ensure_default_project_creates_once_reuses(pg_engine):
    uid = await _new_user(pg_engine)
    first = await ensure_default_project(pg_engine, uid)
    second = await ensure_default_project(pg_engine, uid)
    assert first == second
    async with pg_engine.connect() as conn:
        rows = (await conn.execute(projects_table.select())).all()
    assert len(rows) == 1
    assert rows[0].name == "personal"
    assert bool(rows[0].is_default)


async def test_audit_log_records_and_lists(pg_engine):
    log = AuditLog(engine=pg_engine)
    await log.record(
        "auth.api_key_created",
        actor_user_id=None,
        actor_kind="system",
        resource_type="api_key",
        resource_id="inv_abcd1234",
        meta={"label": "x"},
    )
    entries = await log.recent(limit=5)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["action"] == "auth.api_key_created"
    assert entry["actor_kind"] == "system"
    assert entry["resource_type"] == "api_key"
    assert entry["meta"] == {"label": "x"}
