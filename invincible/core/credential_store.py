# invincible/core/credential_store.py
"""Repository over ``user_provider_credentials`` (Platform Phase 9).

Thin SQLAlchemy async Core repo, same discipline as the other stores:
ownership predicates go through ``user_id`` on every read/write, secrets
never leave this module except as Fernet ciphertext (``get_for_user`` /
``routing_rows`` return the raw row for decrypt-by-caller) or the one-way
``key_masked`` hint.
"""
import time

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from invincible.core.credential_crypto import encrypt
from invincible.core.db import user_provider_credentials

# Columns safe to hand to a response body / template. encrypted_api_key
# is deliberately absent.
PUBLIC_COLUMNS = (
    user_provider_credentials.c.id,
    user_provider_credentials.c.user_id,
    user_provider_credentials.c.provider_name,
    user_provider_credentials.c.catalog_key,
    user_provider_credentials.c.model_id,
    user_provider_credentials.c.base_url,
    user_provider_credentials.c.key_masked,
    user_provider_credentials.c.status,
    user_provider_credentials.c.last_tested_at,
    user_provider_credentials.c.created_at,
    user_provider_credentials.c.updated_at,
)


class DuplicateCredentialError(Exception):
    """This user already has a credential with the same provider_name."""


def mask_api_key(plaintext: str) -> str:
    """One-way display hint: first 3 + last 4 chars of a long key, nothing
    at all for short ones (revealing 7 of a 10-char key would be most of
    the secret)."""
    plaintext = plaintext.strip()
    if len(plaintext) < 12:
        return "••••••"
    return f"{plaintext[:3]}…{plaintext[-4:]}"


class ByokCredentialStore:
    def __init__(self, engine):
        self.engine = engine

    async def create(
        self,
        *,
        user_id: int,
        provider_name: str,
        model_id: str,
        base_url: str,
        api_key: str,
        catalog_key: str | None = None,
    ) -> dict:
        """Encrypt and store one credential; returns the public row."""
        now = time.time()
        try:
            async with self.engine.begin() as conn:
                row = (await conn.execute(
                    user_provider_credentials
                    .insert()
                    .values(
                        user_id=user_id,
                        provider_name=provider_name,
                        catalog_key=catalog_key,
                        model_id=model_id,
                        base_url=base_url,
                        encrypted_api_key=encrypt(api_key),
                        key_masked=mask_api_key(api_key),
                        created_at=now,
                        updated_at=now,
                    )
                    .returning(*PUBLIC_COLUMNS)
                )).mappings().one()
        except IntegrityError as exc:
            raise DuplicateCredentialError(
                f"A provider named '{provider_name}' is already connected"
            ) from exc
        return dict(row)

    async def list(self, user_id: int) -> list[dict]:
        """Public rows for one user, oldest first (routing order)."""
        async with self.engine.connect() as conn:
            rows = (await conn.execute(
                select(*PUBLIC_COLUMNS)
                .where(user_provider_credentials.c.user_id == user_id)
                .order_by(
                    user_provider_credentials.c.created_at,
                    user_provider_credentials.c.id,
                )
            )).mappings().all()
        return [dict(r) for r in rows]

    async def get_for_user(self, credential_id: int, user_id: int) -> dict | None:
        """Full row (INCLUDING ciphertext) when owned by user_id; None for
        foreign/unknown ids - callers render both identically."""
        async with self.engine.connect() as conn:
            row = (await conn.execute(
                user_provider_credentials.select().where(
                    user_provider_credentials.c.id == credential_id,
                    user_provider_credentials.c.user_id == user_id,
                )
            )).mappings().first()
        return dict(row) if row else None

    async def delete(self, credential_id: int, user_id: int) -> bool:
        """Ownership-predicated delete; True iff a row was removed."""
        async with self.engine.begin() as conn:
            result = await conn.execute(
                delete(user_provider_credentials).where(
                    user_provider_credentials.c.id == credential_id,
                    user_provider_credentials.c.user_id == user_id,
                )
            )
        return bool(result.rowcount)

    async def update_test_outcome(self, credential_id: int, status: str) -> None:
        now = time.time()
        async with self.engine.begin() as conn:
            await conn.execute(
                update(user_provider_credentials)
                .where(user_provider_credentials.c.id == credential_id)
                .values(
                    status=status,
                    last_tested_at=now,
                    updated_at=now,
                )
            )

    async def routing_rows(self, user_id: int) -> list[dict]:
        """Full rows (INCLUDING ciphertext) in routing order - the router's
        candidate source for this user's requests."""
        async with self.engine.connect() as conn:
            rows = (await conn.execute(
                user_provider_credentials.select()
                .where(user_provider_credentials.c.user_id == user_id)
                .order_by(
                    user_provider_credentials.c.created_at,
                    user_provider_credentials.c.id,
                )
            )).mappings().all()
        return [dict(r) for r in rows]
