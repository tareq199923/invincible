# invincible/core/oauth_store.py
"""SQLite-backed store for the self-hosted OAuth authorization server.

Holds dynamic client registrations, single-use authorization codes, and
access/refresh tokens for the MCP bearer-token auth model. Lives in the same
SQLite file as conversations (sessions.db), so the existing read_file /
write_file denylist entries for that file also protect the OAuth tables.

Tokens are stored as SHA-256 hashes, never in the clear: a read of the
database file yields nothing usable. Lookups re-hash the presented token and
match on the digest.
"""
import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlparse

import aiosqlite

from invincible.core.session_store import default_db_path

CODE_TTL = 300            # authorization codes live 5 minutes
ACCESS_TOKEN_TTL = 3600   # access tokens live 1 hour
REFRESH_TOKEN_TTL = 30 * 24 * 3600  # refresh tokens live 30 days


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> float:
    return time.time()


class OAuthError(Exception):
    """Raised for invalid_grant style failures; carries the RFC 6749 error
    code and optional description the endpoint should respond with."""

    def __init__(self, error: str, description: str = None):
        super().__init__(error)
        self.error = error
        self.description = description


class OAuthStore:
    """Persistent grants store for the /oauth endpoints.

    Same db_path resolution as SessionStore (INVINCIBLE_DB_PATH or
    sessions.db in the CWD) so both stores share one file and one
    security boundary.
    """

    def __init__(self, db_path: str = None):
        self.db_path = db_path or default_db_path()
        self._db: aiosqlite.Connection | None = None

    async def init(self):
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS oauth_clients (
                client_id TEXT PRIMARY KEY,
                client_name TEXT NOT NULL DEFAULT '',
                redirect_uris TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS oauth_codes (
                code TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                code_challenge TEXT NOT NULL,
                expires_at REAL NOT NULL,
                used INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS oauth_tokens (
                token_hash TEXT PRIMARY KEY,
                token_type TEXT NOT NULL,
                client_id TEXT NOT NULL,
                expires_at REAL NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            """
        )
        await self._db.commit()

    async def close(self):
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _expire_lazy(self):
        """Discard codes/tokens that have outlived their TTL. Called on
        lookups so the tables never grow without bound."""
        now = _now()
        await self._db.execute(
            "DELETE FROM oauth_codes WHERE expires_at <= ?", (now,)
        )
        await self._db.execute(
            "DELETE FROM oauth_tokens WHERE expires_at <= ?", (now,)
        )
        await self._db.commit()

    # --- clients (RFC 7591 dynamic registration) ---

    def _valid_redirect_uri(self, uri: str) -> bool:
        """OAuth 2.1 communication-security rule: redirect URIs must be
        HTTPS, or loopback HTTP (localhost / 127.0.0.1)."""
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
        self, redirect_uris: list, client_name: str = ""
    ) -> dict:
        """Register a public client (no client_secret by design)."""
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
        await self._db.execute(
            "INSERT INTO oauth_clients"
            " (client_id, client_name, redirect_uris, created_at)"
            " VALUES (?, ?, ?, ?)",
            (client_id, client_name.strip(), json.dumps(redirect_uris), _now()),
        )
        await self._db.commit()
        return {
            "client_id": client_id,
            "client_name": client_name.strip(),
            "redirect_uris": redirect_uris,
            "client_id_issued_at": int(_now()),
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }

    async def get_client(self, client_id: str) -> dict | None:
        async with self._db.execute(
            "SELECT client_id, client_name, redirect_uris, created_at"
            " FROM oauth_clients WHERE client_id = ?",
            (client_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        client_id, client_name, redirect_uris, created_at = row
        return {
            "client_id": client_id,
            "client_name": client_name,
            "redirect_uris": json.loads(redirect_uris),
            "created_at": created_at,
        }

    # --- authorization codes ---

    async def create_code(
        self, client_id: str, redirect_uri: str, code_challenge: str,
        ttl: float = CODE_TTL,
    ) -> str:
        code = secrets.token_urlsafe(24)
        await self._db.execute(
            "INSERT INTO oauth_codes (code, client_id, redirect_uri,"
            " code_challenge, expires_at, used) VALUES (?, ?, ?, ?, ?, 0)",
            (code, client_id, redirect_uri, code_challenge, _now() + ttl),
        )
        await self._db.commit()
        return code

    async def consume_code(
        self, code: str, client_id: str, redirect_uri: str,
        code_verifier: str,
    ) -> None:
        """Validate and burn an authorization code, verifying PKCE and the
        exact redirect_uri/client the code was bound to. Raises OAuthError
        on any mismatch, replay, or expiry."""
        await self._expire_lazy()
        async with self._db.execute(
            "SELECT code, client_id, redirect_uri, code_challenge, used"
            " FROM oauth_codes WHERE code = ?",
            (code,),
        ) as cursor:
            row = await cursor.fetchone()
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
                "invalid_grant", "authorization code was not issued for this request"
            )
        verifier_challenge = _s256_challenge(code_verifier)
        if not secrets.compare_digest(verifier_challenge, challenge):
            raise OAuthError("invalid_grant", "PKCE verification failed")
        await self._db.execute(
            "UPDATE oauth_codes SET used = 1 WHERE code = ?", (code,)
        )
        await self._db.commit()

    # --- tokens ---

    async def _insert_token(
        self, token_type: str, client_id: str, ttl: float
    ) -> str:
        raw = secrets.token_urlsafe(32)
        await self._db.execute(
            "INSERT INTO oauth_tokens (token_hash, token_type, client_id,"
            " expires_at, revoked, created_at) VALUES (?, ?, ?, ?, 0, ?)",
            (token_hash(raw), token_type, client_id, _now() + ttl, _now()),
        )
        await self._db.commit()
        return raw

    async def issue_token_pair(self, client_id: str) -> dict:
        access = await self._insert_token("access", client_id, ACCESS_TOKEN_TTL)
        refresh = await self._insert_token("refresh", client_id, REFRESH_TOKEN_TTL)
        return {"access_token": access, "refresh_token": refresh}

    async def _row_for(self, raw_token: str) -> tuple | None:
        await self._expire_lazy()
        async with self._db.execute(
            "SELECT token_hash, token_type, client_id, expires_at, revoked"
            " FROM oauth_tokens WHERE token_hash = ?",
            (token_hash(raw_token),),
        ) as cursor:
            return await cursor.fetchone()

    async def validate_access(self, raw_token: str) -> dict | None:
        """Return token info when the presented token is a live, unrevoked,
        unexpired access token, else None."""
        row = await self._row_for(raw_token)
        if row is None:
            return None
        _, token_type, client_id, _, revoked = row
        if token_type != "access" or revoked:
            return None
        return {"client_id": client_id}

    async def rotate_refresh(self, raw_token: str) -> dict:
        """Rotate a refresh token: revoke the presented one and issue a new
        access/refresh pair bound to the same client."""
        row = await self._row_for(raw_token)
        if row is None:
            raise OAuthError("invalid_grant", "refresh token is invalid or expired")
        token_hash_value, token_type, client_id, _, revoked = row
        if token_type != "refresh" or revoked:
            raise OAuthError("invalid_grant", "refresh token is invalid or expired")
        await self._db.execute(
            "UPDATE oauth_tokens SET revoked = 1 WHERE token_hash = ?",
            (token_hash_value,),
        )
        await self._db.commit()
        return await self.issue_token_pair(client_id)

    async def revoke(self, raw_token: str) -> bool:
        """Revoke a token (access or refresh). Unknown tokens count as
        "already revoked" for RFC 7009's 200 behavior. Returns True when a
        token was actually revoked."""
        row = await self._row_for(raw_token)
        if row is None:
            return False
        token_hash_value, _, _, _, revoked = row
        if revoked:
            return False
        await self._db.execute(
            "UPDATE oauth_tokens SET revoked = 1 WHERE token_hash = ?",
            (token_hash_value,),
        )
        await self._db.commit()
        return True

    # --- CLI visibility / control ---

    async def revoke_client_tokens(self, client_id: str) -> int:
        """Revoke every access/refresh token for a client. Returns the
        number of tokens revoked."""
        await self._expire_lazy()
        async with self._db.execute(
            "SELECT token_hash FROM oauth_tokens WHERE client_id = ?"
            " AND revoked = 0",
            (client_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        for (token_hash_value,) in rows:
            await self._db.execute(
                "UPDATE oauth_tokens SET revoked = 1 WHERE token_hash = ?",
                (token_hash_value,),
            )
        await self._db.commit()
        return len(rows)

    async def list_clients(self) -> list:
        async with self._db.execute(
            "SELECT client_id, client_name, redirect_uris, created_at"
            " FROM oauth_clients ORDER BY created_at"
        ) as cursor:
            rows = await cursor.fetchall()
        clients = []
        for client_id, client_name, redirect_uris, created_at in rows:
            clients.append({
                "client_id": client_id,
                "client_name": client_name,
                "redirect_uris": json.loads(redirect_uris),
                "created_at": created_at,
            })
        return clients

    async def list_active_tokens(self, client_id: str = None) -> list:
        await self._expire_lazy()
        query = (
            "SELECT token_hash, token_type, client_id, expires_at, revoked"
            " FROM oauth_tokens"
        )
        params = []
        if client_id is not None:
            query += " WHERE client_id = ?"
            params.append(client_id)
        query += " ORDER BY created_at"
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "token_hash": token_hash_value[:12],
                "token_type": token_type,
                "client_id": client_id_value,
                "expires_at": expires_at,
                "revoked": bool(revoked),
            }
            for (token_hash_value, token_type, client_id_value,
                 expires_at, revoked) in rows
        ]


def _s256_challenge(verifier: str) -> str:
    """RFC 7636 S256 code challenge computation."""
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
