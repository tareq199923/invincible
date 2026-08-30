# invincible/core/oauth_store.py
"""OAuth authorization-server store on PostgreSQL (Phase 16).

Dynamic client registrations, single-use authorization codes, and
access/refresh tokens for the MCP bearer-token model. Tokens are stored as
SHA-256 hashes, never in the clear; ``redirect_uris`` lives in JSONB.
"""
import base64
import hashlib
import secrets
import time
from urllib.parse import urlparse

from sqlalchemy import delete, or_, select, update

from invincible.core.db import oauth_clients, oauth_codes, oauth_tokens

CODE_TTL = 300            # authorization codes live 5 minutes
ACCESS_TOKEN_TTL = 3600   # access tokens live 1 hour
REFRESH_TOKEN_TTL = 30 * 24 * 3600  # refresh tokens live 30 days


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> float:
    return time.time()


class OAuthError(Exception):
    def __init__(self, error: str, description: str = None):
        super().__init__(error)
        self.error = error
        self.description = description


class OAuthStore:
    """Persistent grants store for the /oauth endpoints."""

    def __init__(self, engine):
        self.engine = engine

    async def init(self) -> None:
        """Schema owned by core.db metadata."""

    async def close(self) -> None:
        """Engine owned/disposed by the lifespan."""

    async def _expire_lazy(self) -> None:
        now = _now()
        async with self.engine.begin() as conn:
            await conn.execute(
                delete(oauth_codes).where(oauth_codes.c.expires_at <= now)
            )
            await conn.execute(
                delete(oauth_tokens).where(oauth_tokens.c.expires_at <= now)
            )

    # --- clients (RFC 7591 dynamic registration) ---

    @staticmethod
    def _valid_redirect_uri(uri: str) -> bool:
        try:
            parsed = urlparse(uri)
        except ValueError:
            return False
        if parsed.scheme == "https":
            return bool(parsed.netloc) and not parsed.fragment
        if parsed.scheme == "http":
            host = (parsed.hostname or "").lower()
            return host in ("localhost", "127.0.0.1")
        return False

    async def register_client(
        self, redirect_uris: list, client_name: str = "",
        owner_user_id: int | None = None,
    ) -> dict:
        if not isinstance(redirect_uris, list) or not redirect_uris:
            raise OAuthError("invalid_request", "redirect_uris is required")
        if not all(isinstance(u, str) for u in redirect_uris):
            raise OAuthError("invalid_request", "redirect_uris must be strings")
        bad = [u for u in redirect_uris if not self._valid_redirect_uri(u)]
        if bad:
            raise OAuthError(
                "invalid_redirect_uri",
                "redirect URIs must be https:// or http://localhost/* "
                f"(rejected: {', '.join(sorted(bad))})",
            )
        client_id = secrets.token_urlsafe(16)
        async with self.engine.begin() as conn:
            await conn.execute(
                oauth_clients.insert().values(
                    client_id=client_id,
                    client_name=client_name.strip(),
                    redirect_uris=redirect_uris,
                    # Phase 2 subject: the user on whose behalf this
                    # client operates (stamped by the consent flow).
                    owner_user_id=owner_user_id,
                    created_at=_now(),
                )
            )
        return {
            "client_id": client_id,
            "client_name": client_name.strip(),
            "redirect_uris": redirect_uris,
        }

    async def get_client(self, client_id: str) -> dict | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(
                oauth_clients.select().where(
                    oauth_clients.c.client_id == client_id)
            )).mappings().first()
        return dict(row) if row else None

    async def attach_owner(self, client_id: str, user_id: int) -> None:
        """Stamp the authorizing user onto a client at consent time
        (Phase 2 subject binding). Idempotent."""
        from sqlalchemy import update

        async with self.engine.begin() as conn:
            await conn.execute(
                update(oauth_clients)
                .where(oauth_clients.c.client_id == client_id)
                .values(owner_user_id=user_id)
            )

    async def consume_code_subject(
        self, code: str, client_id: str, redirect_uri: str,
        code_verifier: str,
    ) -> int | None:
        """Phase 2 variant of :meth:`consume_code` that also returns the
        authorizing subject recorded at approval time (None on legacy
        codes). The code is marked used exactly like the base path."""
        await self._expire_lazy()
        async with self.engine.connect() as conn:
            row = (await conn.execute(
                select(
                    oauth_codes.c.code,
                    oauth_codes.c.client_id,
                    oauth_codes.c.redirect_uri,
                    oauth_codes.c.code_challenge,
                    oauth_codes.c.used,
                    oauth_codes.c.subject_user_id,
                ).where(oauth_codes.c.code == code)
            )).first()
        if row is None:
            raise OAuthError(
                "invalid_grant", "authorization code is invalid or expired"
            )
        _, stored_client, stored_redirect, challenge, used, subject = row
        if used:
            raise OAuthError(
                "invalid_grant", "authorization code has already been used"
            )
        if stored_client != client_id or stored_redirect != redirect_uri:
            raise OAuthError(
                "invalid_grant",
                "authorization code was not issued for this request",
            )
        verifier_challenge = _s256_challenge(code_verifier)
        if not secrets.compare_digest(verifier_challenge, challenge):
            raise OAuthError("invalid_grant", "PKCE verification failed")
        async with self.engine.begin() as conn:
            await conn.execute(
                update(oauth_codes)
                .where(oauth_codes.c.code == code)
                .values(used=True)
            )
        return subject

    # --- authorization codes ---

    async def create_code(
        self, client_id: str, redirect_uri: str, code_challenge: str,
        ttl: float = CODE_TTL, subject_user_id: int | None = None,
    ) -> str:
        code = secrets.token_urlsafe(24)
        async with self.engine.begin() as conn:
            await conn.execute(
                oauth_codes.insert().values(
                    code=code,
                    client_id=client_id,
                    redirect_uri=redirect_uri,
                    code_challenge=code_challenge,
                    subject_user_id=subject_user_id,
                    expires_at=_now() + ttl,
                    used=False,
                )
            )
        return code

    async def consume_code(
        self, code: str, client_id: str, redirect_uri: str,
        code_verifier: str,
    ) -> None:
        await self._expire_lazy()
        async with self.engine.connect() as conn:
            row = (await conn.execute(
                select(
                    oauth_codes.c.code,
                    oauth_codes.c.client_id,
                    oauth_codes.c.redirect_uri,
                    oauth_codes.c.code_challenge,
                    oauth_codes.c.used,
                ).where(oauth_codes.c.code == code)
            )).first()
        if row is None:
            raise OAuthError(
                "invalid_grant", "authorization code is invalid or expired"
            )
        _, stored_client, stored_redirect, challenge, used = row
        if used:
            raise OAuthError(
                "invalid_grant", "authorization code has already been used"
            )
        if stored_client != client_id or stored_redirect != redirect_uri:
            raise OAuthError(
                "invalid_grant",
                "authorization code was not issued for this request",
            )
        verifier_challenge = _s256_challenge(code_verifier)
        if not secrets.compare_digest(verifier_challenge, challenge):
            raise OAuthError("invalid_grant", "PKCE verification failed")
        async with self.engine.begin() as conn:
            await conn.execute(
                update(oauth_codes)
                .where(oauth_codes.c.code == code)
                .values(used=True)
            )

    # --- tokens ---

    async def _insert_token(
        self, token_type: str, client_id: str, ttl: float,
        subject_user_id: int | None = None,
    ) -> str:
        raw = secrets.token_urlsafe(32)
        async with self.engine.begin() as conn:
            await conn.execute(
                oauth_tokens.insert().values(
                    token_hash=token_hash(raw),
                    token_type=token_type,
                    client_id=client_id,
                    subject_user_id=subject_user_id,
                    expires_at=_now() + ttl,
                    revoked=False,
                    created_at=_now(),
                )
            )
        return raw

    async def issue_token_pair(
        self, client_id: str, subject_user_id: int | None = None,
    ) -> dict:
        access = await self._insert_token(
            "access", client_id, ACCESS_TOKEN_TTL, subject_user_id)
        refresh = await self._insert_token(
            "refresh", client_id, REFRESH_TOKEN_TTL, subject_user_id)
        return {"access_token": access, "refresh_token": refresh}

    async def _row_for(self, raw_token: str):
        await self._expire_lazy()
        async with self.engine.connect() as conn:
            return (await conn.execute(
                select(
                    oauth_tokens.c.token_hash,
                    oauth_tokens.c.token_type,
                    oauth_tokens.c.client_id,
                    oauth_tokens.c.subject_user_id,
                    oauth_tokens.c.expires_at,
                    oauth_tokens.c.revoked,
                ).where(oauth_tokens.c.token_hash == token_hash(raw_token))
            )).first()

    async def validate_access(self, raw_token: str) -> dict | None:
        row = await self._row_for(raw_token)
        if row is None:
            return None
        _, token_type, client_id, subject_user_id, _, revoked = row
        if token_type != "access" or revoked:
            return None
        return {"client_id": client_id, "subject_user_id": subject_user_id}

    async def rotate_refresh(self, raw_token: str) -> dict:
        row = await self._row_for(raw_token)
        if row is None:
            raise OAuthError("invalid_grant",
                             "refresh token is invalid or expired")
        token_hash_value, token_type, client_id, subject, _, revoked = row
        if token_type != "refresh" or revoked:
            raise OAuthError("invalid_grant",
                             "refresh token is invalid or expired")
        await self._revoke_by_hash(token_hash_value)
        return await self.issue_token_pair(client_id, subject)

    async def revoke(self, raw_token: str) -> bool:
        row = await self._row_for(raw_token)
        if row is None:
            return False
        token_hash_value = row[0]
        if row[5]:
            return False
        await self._revoke_by_hash(token_hash_value)
        return True

    async def _revoke_by_hash(self, token_hash_value: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                update(oauth_tokens)
                .where(oauth_tokens.c.token_hash == token_hash_value)
                .values(revoked=True)
            )

    # --- CLI visibility / control ---

    async def revoke_client_tokens(self, client_id: str) -> int:
        await self._expire_lazy()
        async with self.engine.begin() as conn:
            result = await conn.execute(
                update(oauth_tokens)
                .where(oauth_tokens.c.client_id == client_id,
                       oauth_tokens.c.revoked.is_(False))
                .values(revoked=True)
            )
        return result.rowcount

    async def list_clients(self) -> list:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(
                oauth_clients.select()
                .order_by(oauth_clients.c.created_at)
            )).mappings().all()
        clients = []
        for row in rows:
            client = dict(row)
            clients.append({
                "client_id": client["client_id"],
                "client_name": client["client_name"],
                "redirect_uris": client["redirect_uris"],
                "created_at": client["created_at"],
            })
        return clients

    async def list_clients_manageable(self, user_ids: list[int]) -> list:
        """Clients any of these principals may manage on the dashboard:
        owned by one of them, or unowned (local-owner era registrations).
        Same shape as ``list_clients``; the only scoping difference is
        ownership."""
        if not user_ids:
            return []
        async with self.engine.connect() as conn:
            rows = (await conn.execute(
                oauth_clients.select()
                .where(or_(
                    oauth_clients.c.owner_user_id.in_(user_ids),
                    oauth_clients.c.owner_user_id.is_(None),
                ))
                .order_by(oauth_clients.c.created_at)
            )).mappings().all()
        clients = []
        for row in rows:
            client = dict(row)
            clients.append({
                "client_id": client["client_id"],
                "client_name": client["client_name"],
                "redirect_uris": client["redirect_uris"],
                "created_at": client["created_at"],
            })
        return clients

    async def get_client_owner(self, client_id: str) -> int | None:
        async with self.engine.connect() as conn:
            return (await conn.execute(
                select(oauth_clients.c.owner_user_id)
                .where(oauth_clients.c.client_id == client_id)
            )).scalar()

    async def list_active_tokens(self, client_id: str = None) -> list:
        await self._expire_lazy()
        query = (
            select(
                oauth_tokens.c.token_hash,
                oauth_tokens.c.token_type,
                oauth_tokens.c.client_id,
                oauth_tokens.c.expires_at,
                oauth_tokens.c.revoked,
            )
            .order_by(oauth_tokens.c.created_at)
        )
        if client_id is not None:
            query = query.where(oauth_tokens.c.client_id == client_id)
        async with self.engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [
            {
                "token_hash": th[:12],
                "token_type": tt,
                "client_id": cid,
                "expires_at": exp,
                "revoked": bool(rev),
            }
            for (th, tt, cid, exp, rev) in rows
        ]


def _s256_challenge(verifier: str) -> str:
    """RFC 7636 S256 code challenge computation."""
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
