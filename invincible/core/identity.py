# invincible/core/identity.py
"""Identity primitives and the API-key lifecycle (Platform Phase 1).

Password hashing is argon2id (roadmap decision); API keys are random
``inv_``-prefixed tokens stored as SHA-256 hashes with a visible prefix -
raw values appear exactly once, at creation, mirroring the OAuth-token
discipline. The HTTP account endpoints are Phase 3; until then the CLI
(``invincible api-key ...``) mints and revokes keys.

Schema truth lives in ``core.db``; this module holds behavior only.
"""
import hashlib
import secrets
import time

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from invincible.core.db import api_keys, audit_log, projects

API_KEY_PREFIX = "inv_"
# Visible portion stored for listings: "inv_" + 8 random chars.
_API_KEY_PREFIX_LEN = len(API_KEY_PREFIX) + 8


def hash_password(password: str) -> str:
    """argon2id hash (roadmap-decision primitive; login lands in P3)."""
    from argon2 import PasswordHasher

    return PasswordHasher().hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Constant-argonaut verification; False for NULL/unknown hashes."""
    if not password_hash:
        return False
    from argon2 import PasswordHasher
    from argon2.exceptions import VerificationError, VerifyMismatchError

    try:
        PasswordHasher().verify(password_hash, password)
    except (
        VerifyMismatchError,
        VerificationError,
        ValueError,  # malformed/foreign hash formats
    ):
        return False
    return True


def _hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """One minted key as ``(raw, key_hash, visible_prefix)``. The raw value
    never touches storage."""
    raw = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return raw, _hash_api_key(raw), raw[:_API_KEY_PREFIX_LEN]


async def ensure_default_project(engine, user_id: int) -> int:
    """Get-or-create a user's default project; returns its id. Sessions
    created under an API-key principal resolve into this project."""
    async with engine.begin() as conn:
        row = (await conn.execute(
            select(projects.c.id).where(
                projects.c.user_id == user_id,
                projects.c.is_default.is_(True),
            )
        )).first()
        if row is not None:
            return int(row[0])
        now = time.time()
        await conn.execute(
            pg_insert(projects)
            .values(user_id=user_id, name="personal", is_default=True,
                    created_at=now)
            .on_conflict_do_nothing(index_elements=["user_id", "name"])
        )
        project_id = (await conn.execute(
            select(projects.c.id).where(
                projects.c.user_id == user_id,
                projects.c.is_default.is_(True),
            )
        )).scalar_one()
        return int(project_id)


class ApiKeyStore:
    """Thin repository over ``api_keys``."""

    def __init__(self, engine):
        self.engine = engine

    async def create(self, user_id: int, *, label: str = "") -> dict:
        """Mint one key for ``user_id``. The RAW key appears here and only
        here - callers must present it to the user immediately."""
        raw, key_hash, prefix = generate_api_key()
        now = time.time()
        async with self.engine.begin() as conn:
            row = (await conn.execute(
                api_keys.insert()
                .values(user_id=user_id, label=label, key_hash=key_hash,
                        prefix=prefix, created_at=now)
                .returning(api_keys.c.id)
            )).one()
        return {
            "id": int(row[0]),
            "user_id": user_id,
            "label": label,
            "prefix": prefix,
            "created_at": now,
            # The one-time secret:
            "raw": raw,
        }

    async def resolve(self, raw: str) -> dict | None:
        """Look up an unrevoked key by raw value; returns
        ``{"id", "user_id"}`` or None. Touches last_used_at best-effort."""
        if not raw.startswith(API_KEY_PREFIX):
            return None
        key_hash = _hash_api_key(raw)
        async with self.engine.connect() as conn:
            row = (await conn.execute(
                select(api_keys.c.id, api_keys.c.user_id)
                .where(api_keys.c.key_hash == key_hash,
                       api_keys.c.revoked_at.is_(None))
            )).first()
        if row is None:
            return None
        await self._touch(int(row[0]))
        return {"id": int(row[0]), "user_id": int(row[1])}

    async def _touch(self, key_id: int) -> None:
        try:
            async with self.engine.begin() as conn:
                await conn.execute(
                    update(api_keys)
                    .where(api_keys.c.id == key_id)
                    .values(last_used_at=time.time())
                )
        except Exception:  # noqa: BLE001 - telemetry only, never fatal
            pass

    async def list(self, user_id: int | None = None) -> list[dict]:
        """All keys (optionally one user's). Never includes hashes."""
        query = select(
            api_keys.c.id,
            api_keys.c.user_id,
            api_keys.c.label,
            api_keys.c.prefix,
            api_keys.c.created_at,
            api_keys.c.last_used_at,
            api_keys.c.revoked_at,
        ).order_by(api_keys.c.id.desc())
        if user_id is not None:
            query = query.where(api_keys.c.user_id == user_id)
        async with self.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [dict(r) for r in rows]

    async def revoke(self, key_ref: int | str) -> bool:
        """Revoke by numeric id or visible prefix. True when a live key
        was found; already-revoked keys report False (idempotent CLI)."""
        column = api_keys.c.id if isinstance(key_ref, int) else api_keys.c.prefix
        async with self.engine.begin() as conn:
            result = await conn.execute(
                update(api_keys)
                .where(column == key_ref, api_keys.c.revoked_at.is_(None))
                .values(revoked_at=time.time())
            )
        return bool(result.rowcount)


class AuditLog:
    """Append-only sensitive-action trail (writes land in Phase 2 paths;
    the store exists so auth events can start recording now)."""

    def __init__(self, engine):
        self.engine = engine

    async def record(
        self,
        action: str,
        *,
        actor_user_id: int | None = None,
        actor_kind: str = "system",
        resource_type: str | None = None,
        resource_id: str | None = None,
        request_id: str | None = None,
        meta: dict | None = None,
    ) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                audit_log.insert().values(
                    at=time.time(),
                    actor_user_id=actor_user_id,
                    actor_kind=actor_kind,
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    request_id=request_id,
                    meta=meta,
                )
            )

    async def recent(self, limit: int = 50) -> list[dict]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(
                audit_log.select()
                .order_by(audit_log.c.at.desc(), audit_log.c.id.desc())
                .limit(limit)
            )).mappings().all()
        return [dict(r) for r in rows]


class LoginRateLimiter:
    """Persistent fixed-window lockout for login forms (Phase 2).

    State lives in the ``login_attempts`` table, so a restart no longer
    clears an attacker's counter. Same semantics as before -
    ``max_attempts`` failures inside ``window_seconds`` from one IP lock
    it out for the rest of the window. Expired windows reset lazily on
    check/record; rows are deleted when a window resets to keep the table
    bounded. Since Phase 3 each instance targets one ``scope`` ("owner"
    for OAuth-consent login, "auth-login" for /auth/login) so realms
    never share counters.
    """

    def __init__(self, engine, *, scope: str = "owner",
                 max_attempts: int = 5,
                 window_seconds: float = 15 * 60):
        self.engine = engine
        self.scope = scope
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds

    def _expired(self, row, now: float) -> bool:
        return now - row["window_start"] >= self.window_seconds

    async def locked_out(self, ip: str) -> int | None:
        """Seconds until the lockout lifts, or None when the IP may try."""
        from invincible.core.db import login_attempts

        now = time.time()
        async with self.engine.begin() as conn:
            row = (await conn.execute(
                select(login_attempts.c.count,
                       login_attempts.c.window_start)
                .where(login_attempts.c.ip == ip,
                       login_attempts.c.scope == self.scope)
            )).mappings().first()
            if row is not None and self._expired(row, now):
                await conn.execute(
                    delete(login_attempts)
                    .where(login_attempts.c.ip == ip,
                           login_attempts.c.scope == self.scope)
                )
                return None
            if row is None or row["count"] < self.max_attempts:
                return None
            return int(
                self.window_seconds - (now - row["window_start"])
            ) + 1

    async def record_failure(self, ip: str) -> None:
        """Count one failed attempt, starting a fresh window if needed."""
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from invincible.core.db import login_attempts

        now = time.time()

        async with self.engine.begin() as conn:
            existing = (await conn.execute(
                select(login_attempts.c.window_start,
                       login_attempts.c.count)
                .where(login_attempts.c.ip == ip,
                       login_attempts.c.scope == self.scope)
            )).first()
            expired = existing is not None and self._expired(
                {"window_start": existing[0]}, now
            )
            if existing is None or expired:
                await conn.execute(
                    pg_insert(login_attempts)
                    .values(ip=ip, scope=self.scope, window_start=now,
                            count=1, updated_at=now)
                    .on_conflict_do_update(
                        index_elements=[login_attempts.c.ip,
                                        login_attempts.c.scope],
                        set_={"window_start": now, "count": 1,
                              "updated_at": now},
                    )
                )
            else:
                await conn.execute(
                    update(login_attempts)
                    .where(login_attempts.c.ip == ip,
                           login_attempts.c.scope == self.scope)
                    .values(count=existing[1] + 1, updated_at=now)
                )

    async def reset(self, ip: str) -> None:
        """Clear the IP's counter (successful login)."""
        from invincible.core.db import login_attempts

        async with self.engine.begin() as conn:
            await conn.execute(
                delete(login_attempts).where(
                    login_attempts.c.ip == ip,
                    login_attempts.c.scope == self.scope,
                )
            )
