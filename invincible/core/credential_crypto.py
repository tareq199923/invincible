# invincible/core/credential_crypto.py
"""Symmetric encryption for user-supplied BYOK API keys (Platform Phase 9).

Primitive choice: **Fernet** (``cryptography.fernet``) - AES-128-CBC with
an HMAC-SHA256 authenticator (encrypt-then-MAC), authenticated like GCM
but with fewer sharp edges around nonce/AD handling. The choice and its
honest limits live in docs/SECURITY.md.

Key discipline:

- One master key, ``INVINCIBLE_CREDENTIAL_KEY`` (a Fernet key: 32 url-safe
  base64 bytes - generate with `invincible secret credential-key`).
- The value is read live on every operation (settings.py convention).
- Missing/malformed key raises :class:`CredentialKeyError`: every BYOK
  surface fails CLOSED instead of storing plaintext.
- A wrong/rotated key during decryption raises
  :class:`CredentialDecryptError` - callers catch it and degrade (skip or
  report failure); it never leaks ciphertext or key material.
"""
from cryptography.fernet import Fernet, InvalidToken

from invincible.core.settings import settings

GENERATION_HINT = (
    "generate one with `invincible secret credential-key`"
)


class CredentialKeyError(Exception):
    """INVINCIBLE_CREDENTIAL_KEY is unset or not a valid Fernet key."""


class CredentialDecryptError(Exception):
    """Stored credential ciphertext does not match the configured key
    (rotated/mismatched master key). Message carries no secret material."""


def _cipher() -> Fernet:
    raw = settings.credential_key()
    if not raw:
        raise CredentialKeyError(
            "INVINCIBLE_CREDENTIAL_KEY is not set - BYOK credential "
            f"storage refuses to run ({GENERATION_HINT})"
        )
    try:
        return Fernet(raw.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise CredentialKeyError(
            "INVINCIBLE_CREDENTIAL_KEY is not a valid Fernet key "
            f"({GENERATION_HINT})"
        ) from exc


def encrypt(plaintext: str) -> bytes:
    """Encrypt one user API key for storage. Raises CredentialKeyError
    when the master key is missing/malformed (fail closed)."""
    return _cipher().encrypt(plaintext.encode("utf-8"))


def decrypt(ciphertext: bytes | bytearray | memoryview) -> str:
    """Decrypt one stored credential. Raises CredentialKeyError when the
    master key is unusable, CredentialDecryptError on any mismatch -
    both caught by callers, never allowed to crash uncaught."""
    try:
        return _cipher().decrypt(bytes(ciphertext)).decode("utf-8")
    except InvalidToken as exc:
        raise CredentialDecryptError(
            "Stored credential does not match the configured "
            "INVINCIBLE_CREDENTIAL_KEY (key rotated without re-encryption)"
        ) from exc
