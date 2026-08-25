# invincible/core/accounts.py
"""Platform Phase 3 account services.

Everything behind the /auth/*, /projects, and /api-keys HTTP surface:

- ``UserService``      - registration (unique email, min-8 password) and
  argon2id authentication;
- ``SessionManager``   - stateless HMAC-signed HttpOnly browser cookies
  (``v1.<uid>.<expiry>.<sig>``) keyed off the owner secret - same
  trade-off as the OAuth-consent cookie: rotating the secret logs every
  browser out. Unset secret = fail closed (no session is ever valid);
- ``ProjectService``   - create/rename/list/archive with ownership
  predicates on every query (archive is a soft hide, rows kept);
- ``DeviceCodeStore``  - RFC 8628-style pairing: hashed device_code at
  rest, short human-typed user_code, pending -> approved/denied ->
  complete lifecycle, per-code poll interval with slow_down;
- ``IdentityStore``    - external identity links (GitHub today);
- ``GitHubOAuth``      - GitHub OAuth App client (authorize URL, code
  exchange, verified-primary-email resolution). GitHub OAuth Apps have no
  PKCE, so CSRF is handled by the signed state cookie in the endpoint.

Schema truth lives in ``core.db``; this module holds behavior only.
"""
import hashlib
import hmac
import re
import secrets
import time

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from invincible.core.db import (
    device_codes,
    projects,
    user_identities,
    users,
)
from invincible.core.identity import ApiKeyStore, hash_password, verify_password
from invincible.core.settings import settings

SESSION_COOKIE = "invincible_session"
GITHUB_STATE_COOKIE = "invincible_gh_state"
SESSION_TTL = 30 * 24 * 3600
MIN_PASSWORD_LEN = 8
DEVICE_CODE_TTL = 10 * 60
DEFAULT_POLL_INTERVAL = 5.0
# Unambiguous alphabet for the human-typed user_code (no 0/O/1/I/L).
_USER_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AccountError(Exception):
    """Service-level failure the endpoint maps onto an HTTP response."""

    def __init__(self, code: str, message: str,
                 status_code: int = 400, extra: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.extra = extra or {}


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_registration(email: str, password: str) -> tuple[str, str]:
    email = normalize_email(email)
    if not _EMAIL_RE.fullmatch(email):
        raise AccountError("invalid_email", "Enter a valid email address.")
    if len(password) < MIN_PASSWORD_LEN:
        raise AccountError(
            "weak_password",
            f"Password must be at least {MIN_PASSWORD_LEN} characters.",
        )
    return email, password


class UserService:
    """Registration + password authentication over ``users``."""

    def __init__(self, engine):
        self.engine = engine

    async def register(self, email: str, password: str) -> dict:
        """Create one account. A duplicate email is an explicit 409 -
        enumeration-safety belongs to the LOGIN path only."""
        email, password = validate_registration(email, password)
        now = time.time()
        try:
            async with self.engine.begin() as conn:
                row = (await conn.execute(
                    users.insert()
                    .values(email=email, password_hash=hash_password(password),
                            created_at=now)
                    .returning(users.c.id)
                )).one()
        except IntegrityError:
            raise AccountError(
                "duplicate_email",
                "An account with that email already exists.",
                status_code=409,
            ) from None
        return {"id": int(row[0]), "email": email, "created_at": now}

    async def authenticate(self, email: str, password: str) -> dict | None:
        """The user for correct credentials, else None. Unknown email and
        wrong password are indistinguishable to the caller."""
        async with self.engine.connect() as conn:
            row = (await conn.execute(
                select(users.c.id, users.c.email, users.c.password_hash)
                .where(users.c.email == normalize_email(email))
            )).mappings().first()
        if row is None or row["password_hash"] is None:
            return None
        if not verify_password(row["password_hash"], password):
            return None
        return {"id": int(row["id"]), "email": row["email"]}

    async def get(self, user_id: int) -> dict | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(
                select(users.c.id, users.c.email, users.c.created_at)
                .where(users.c.id == user_id)
            )).mappings().first()
        return dict(row) if row is not None else None


class SessionManager:
    """Stateless signed-cookie browser sessions.

    Payload ``v1.<uid>.<expiry>`` + HMAC-SHA256; verification is timing-
    safe and fails closed when no owner secret is configured (the key
    would otherwise be publicly computable).
    """

    @staticmethod
    def _key() -> bytes | None:
        secret = settings.owner_secret() or settings.legacy_owner_secret()
        if not secret:
            return None
        return hashlib.sha256(secret.encode("utf-8")).digest()

    @classmethod
    def available(cls) -> bool:
        return cls._key() is not None

    @classmethod
    def create(cls, user_id: int) -> str:
        key = cls._key()
        if key is None:
            raise AccountError(
                "sessions_disabled",
                "Set INVINCIBLE_OWNER_SECRET to enable account sessions.",
                status_code=503,
            )
        expiry = int(time.time()) + SESSION_TTL
        payload = f"v1.{user_id}.{expiry}"
        signature = hmac.new(key, payload.encode("ascii"), hashlib.sha256
                             ).hexdigest()
        return f"{payload}.{signature}"

    @classmethod
    def verify(cls, value: str | None) -> int | None:
        """The user id for a live, authentic cookie; else None."""
        key = cls._key()
        if key is None or not value:
            return None
        parts = value.split(".")
        if len(parts) != 4 or parts[0] != "v1":
            return None
        _, uid_raw, expiry_raw, signature = parts
        payload = f"v1.{uid_raw}.{expiry_raw}"
        expected = hmac.new(
            key, payload.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return None
        try:
            expiry = int(expiry_raw)
        except ValueError:
            return None
        if time.time() >= expiry:
            return None
        try:
            return int(uid_raw)
        except ValueError:
            return None


class ProjectService:
    """CRUD + soft archive over ``projects``, always owner-scoped."""

    def __init__(self, engine):
        self.engine = engine

    async def create(self, user_id: int, name: str) -> dict:
        name = (name or "").strip()
        if not name or len(name) > 100:
            raise AccountError(
                "invalid_name", "Project name must be 1-100 characters.")
        now = time.time()
        try:
            async with self.engine.begin() as conn:
                row = (await conn.execute(
                    projects.insert()
                    .values(user_id=user_id, name=name, created_at=now)
                    .returning(projects.c.id)
                )).one()
        except IntegrityError:
            raise AccountError(
                "duplicate_project",
                "You already have a project with that name.",
                status_code=409,
            ) from None
        return {"id": int(row[0]), "name": name, "created_at": now}

    async def list(self, user_id: int,
                   include_archived: bool = False) -> list[dict]:
        query = select(
            projects.c.id,
            projects.c.name,
            projects.c.is_default,
            projects.c.archived_at,
            projects.c.created_at,
        ).where(projects.c.user_id == user_id).order_by(projects.c.id)
        if not include_archived:
            query = query.where(projects.c.archived_at.is_(None))
        async with self.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [dict(r) for r in rows]

    async def _owned(self, user_id: int, project_id: int) -> dict:
        """The project row ONLY when it belongs to user_id - existence
        never leaks across users (Phase 2 predicate discipline)."""
        async with self.engine.connect() as conn:
            row = (await conn.execute(
                select(projects.c.id, projects.c.name, projects.c.is_default)
                .where(projects.c.id == project_id,
                       projects.c.user_id == user_id)
            )).mappings().first()
        if row is None:
            raise AccountError(
                "not_found", "No such project.", status_code=404)
        return dict(row)

    async def rename(self, user_id: int, project_id: int, name: str) -> dict:
        owned = await self._owned(user_id, project_id)
        name = (name or "").strip()
        if not name or len(name) > 100:
            raise AccountError(
                "invalid_name", "Project name must be 1-100 characters.")
        try:
            async with self.engine.begin() as conn:
                await conn.execute(
                    update(projects)
                    .where(projects.c.id == project_id)
                    .values(name=name)
                )
        except IntegrityError:
            raise AccountError(
                "duplicate_project",
                "You already have a project with that name.",
                status_code=409,
            ) from None
        return {**owned, "name": name}

    async def archive(self, user_id: int, project_id: int) -> dict:
        owned = await self._owned(user_id, project_id)
        if owned["is_default"]:
            raise AccountError(
                "default_project", "The default project cannot be archived.",
                status_code=409,
            )
        async with self.engine.begin() as conn:
            await conn.execute(
                update(projects)
                .where(projects.c.id == project_id,
                       projects.c.archived_at.is_(None))
                .values(archived_at=time.time())
            )
        return {**owned, "archived": True}


class DeviceCodeStore:
    """RFC 8628-flavored pairing between a CLI and a logged-in browser.

    The raw ``device_code`` exists exactly twice: at creation (returned to
    the CLI) and at the successful token poll (returned inside the minted
    API-key response). Storage holds its sha256 hex only.
    """

    def __init__(self, engine, api_keys: ApiKeyStore | None = None):
        self.engine = engine
        self.api_keys = api_keys or ApiKeyStore(engine)

    @staticmethod
    def _hash(raw: str) -> str:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def _sweep_expired(self, conn) -> None:
        await conn.execute(
            delete(device_codes).where(device_codes.c.expires_at < time.time())
        )

    async def create(self, *, interval: float = DEFAULT_POLL_INTERVAL,
                     ttl: float = DEVICE_CODE_TTL) -> dict:
        now = time.time()
        async with self.engine.begin() as conn:
            await self._sweep_expired(conn)
            while True:
                user_code = "".join(
                    secrets.choice(_USER_CODE_ALPHABET) for _ in range(8)
                )
                taken = (await conn.execute(
                    select(device_codes.c.device_code_hash)
                    .where(device_codes.c.user_code == user_code)
                )).first()
                if taken is None:
                    break
            raw = secrets.token_urlsafe(32)
            await conn.execute(
                device_codes.insert().values(
                    device_code_hash=self._hash(raw),
                    user_code=user_code,
                    interval_seconds=interval,
                    created_at=now,
                    expires_at=now + ttl,
                )
            )
        return {
            "device_code": raw,
            "user_code": user_code,
            "expires_in": int(ttl),
            "interval": int(interval),
        }

    async def get_by_user_code(self, user_code: str) -> dict | None:
        """Live (pending, unexpired) request for the approval page."""
        async with self.engine.begin() as conn:
            await self._sweep_expired(conn)
            row = (await conn.execute(
                select(device_codes.c.status,
                       device_codes.c.expires_at)
                .where(device_codes.c.user_code == user_code.strip().upper())
            )).mappings().first()
        if row is None or row["status"] != "pending" \
                or time.time() >= row["expires_at"]:
            return None
        return dict(row)

    async def approve(self, user_code: str, subject_user_id: int) -> bool:
        """Bind the logged-in approver; one-shot (pending rows only)."""
        async with self.engine.begin() as conn:
            result = await conn.execute(
                update(device_codes)
                .where(device_codes.c.user_code == user_code.strip().upper(),
                       device_codes.c.status == "pending")
                .values(status="approved", subject_user_id=subject_user_id,
                        resolved_at=time.time())
            )
        return bool(result.rowcount)

    async def deny(self, user_code: str) -> bool:
        async with self.engine.begin() as conn:
            result = await conn.execute(
                update(device_codes)
                .where(device_codes.c.user_code == user_code.strip().upper(),
                       device_codes.c.status == "pending")
                .values(status="denied", resolved_at=time.time())
            )
        return bool(result.rowcount)

    async def poll(self, raw_device_code: str) -> dict:
        """One CLI poll. Returns ``{"status": ..., ...}`` or raises
        AccountError with an RFC-ish code (``slow_down`` carries the
        required extra wait in ``extra``). An approved claim mints the
        API key - raw value appears here and only here."""
        now = time.time()
        code_hash = self._hash(raw_device_code or "")
        async with self.engine.begin() as conn:
            row = (await conn.execute(
                select(device_codes.c.status,
                       device_codes.c.expires_at,
                       device_codes.c.interval_seconds,
                       device_codes.c.last_poll_at)
                .where(device_codes.c.device_code_hash == code_hash)
            )).mappings().first()
            if row is None:
                raise AccountError("expired_token", "Unknown device code.",
                                   status_code=400)
            if now >= row["expires_at"]:
                await conn.execute(
                    delete(device_codes)
                    .where(device_codes.c.device_code_hash == code_hash)
                )
                raise AccountError("expired_token",
                                   "The device code has expired.",
                                   status_code=400)
            last = row["last_poll_at"]
            interval = row["interval_seconds"]
            if last is not None and now - last < interval:
                await conn.execute(
                    update(device_codes)
                    .where(device_codes.c.device_code_hash == code_hash)
                    .values(last_poll_at=now)
                )
                raise AccountError(
                    "slow_down",
                    "Poll too frequently; back off.",
                    extra={"interval": max(interval, DEFAULT_POLL_INTERVAL)},
                )
            await conn.execute(
                update(device_codes)
                .where(device_codes.c.device_code_hash == code_hash)
                .values(last_poll_at=now)
            )

            if row["status"] == "denied":
                raise AccountError("access_denied",
                                   "The request was denied.",
                                   status_code=403)
            if row["status"] == "complete":
                raise AccountError("expired_token",
                                   "This approval was already claimed.",
                                   status_code=400)
            if row["status"] != "approved":
                return {"status": "pending",
                        "interval": int(interval),
                        "expires_in": max(0, int(row["expires_at"] - now))}

            # Atomic single-winner claim: exactly one poll converts an
            # approved row into complete and receives the subject.
            claimed = (await conn.execute(
                update(device_codes)
                .where(device_codes.c.device_code_hash == code_hash,
                       device_codes.c.status == "approved")
                .values(status="complete")
                .returning(device_codes.c.subject_user_id)
            )).first()
            if claimed is None or claimed[0] is None:
                raise AccountError("expired_token",
                                   "This approval was already claimed.",
                                   status_code=400)
            user_id = int(claimed[0])

        key = await self.api_keys.create(user_id, label=f"device-{now:.0f}")
        return {"status": "complete", "user_id": user_id, "api_key": key}


class IdentityStore:
    """External identity links (provider + provider_account_id)."""

    def __init__(self, engine):
        self.engine = engine

    async def link(self, user_id: int, provider: str,
                   provider_account_id: str) -> dict:
        now = time.time()
        async with self.engine.begin() as conn:
            existing = (await conn.execute(
                select(user_identities.c.id)
                .where(user_identities.c.provider == provider,
                       user_identities.c.provider_account_id
                       == provider_account_id)
            )).first()
            if existing is not None:
                return {"id": int(existing[0]), "user_id": user_id}
            row = (await conn.execute(
                user_identities.insert()
                .values(user_id=user_id, provider=provider,
                        provider_account_id=provider_account_id,
                        created_at=now)
                .returning(user_identities.c.id)
            )).one()
        return {"id": int(row[0]), "user_id": user_id}

    async def get_user(self, provider: str,
                       provider_account_id: str) -> int | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(
                select(user_identities.c.user_id)
                .where(user_identities.c.provider == provider,
                       user_identities.c.provider_account_id
                       == provider_account_id)
            )).first()
        return int(row[0]) if row is not None else None


class GitHubOAuth:
    """Minimal GitHub OAuth App client (authorization-code flow).

    ``default_transport`` lets tests inject httpx.MockTransport without
    touching real github.com (same discipline as router tests).
    """

    AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
    TOKEN_URL = "https://github.com/login/oauth/access_token"
    USER_URL = "https://api.github.com/user"
    EMAILS_URL = "https://api.github.com/user/emails"
    SCOPE = "read:user user:email"

    default_transport: httpx.AsyncBaseTransport | None = None

    def __init__(self, client_id: str, client_secret: str, *,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self._client = httpx.AsyncClient(
            transport=transport or type(self).default_transport,
            timeout=15,
        )

    @classmethod
    def from_settings(cls) -> "GitHubOAuth | None":
        client_id = settings.github_client_id()
        client_secret = settings.github_client_secret()
        if not client_id or not client_secret:
            return None
        return cls(client_id, client_secret)

    async def aclose(self) -> None:
        await self._client.aclose()

    def build_authorize_url(self, state: str, redirect_uri: str) -> str:
        query = httpx.QueryParams({
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": self.SCOPE,
            "state": state,
        })
        return f"{self.AUTHORIZE_URL}?{query}"

    async def exchange_code(self, code: str, redirect_uri: str) -> str:
        response = await self._client.post(
            self.TOKEN_URL,
            json={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={"Accept": "application/json"},
        )
        data = _json_of(response)
        token = data.get("access_token")
        if response.status_code != 200 or not token:
            raise AccountError(
                "github_exchange_failed",
                "GitHub did not issue an access token.",
                status_code=502,
            )
        return str(token)

    async def fetch_profile(self, access_token: str) -> dict:
        response = await self._client.get(
            self.USER_URL,
            headers=_bearer(access_token),
        )
        data = _json_of(response)
        if response.status_code != 200 or "id" not in data:
            raise AccountError(
                "github_unreachable",
                "Could not read the GitHub profile.",
                status_code=502,
            )
        return {
            "id": str(data["id"]),
            "login": str(data.get("login") or ""),
            "email": data.get("email"),
        }

    async def primary_verified_email(self, access_token: str) -> str | None:
        """The primary AND verified email, else any verified one. Only
        verified addresses may drive auto-linking/registration."""
        response = await self._client.get(
            self.EMAILS_URL,
            headers=_bearer(access_token),
        )
        emails = _json_of(response)
        if response.status_code != 200 or not isinstance(emails, list):
            return None
        verified = [
            entry.get("email")
            for entry in emails
            if isinstance(entry, dict)
            and entry.get("verified")
            and entry.get("email")
        ]
        for entry in emails:
            if isinstance(entry, dict) and entry.get("verified") \
                    and entry.get("primary"):
                return str(entry["email"])
        return str(verified[0]) if verified else None


def _bearer(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }


def _json_of(response: httpx.Response):
    try:
        return response.json()
    except ValueError:
        return {}
