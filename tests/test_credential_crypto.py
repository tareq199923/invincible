# tests/test_credential_crypto.py
"""Platform Phase 9 crypto primitive (PR-A).

Gates: round-trip under a valid Fernet key; missing/malformed master key
fails closed with CredentialKeyError; wrong/rotated key surfaces as
CredentialDecryptError (caught, never an uncaught crash).
"""
import pytest
from cryptography.fernet import Fernet

from invincible.core.credential_crypto import (
    CredentialDecryptError,
    CredentialKeyError,
    decrypt,
    encrypt,
)


@pytest.fixture
def fernet_key(monkeypatch):
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("INVINCIBLE_CREDENTIAL_KEY", key)
    return key


def test_round_trip(fernet_key):
    plaintext = "sk-test-provider-secret-value"
    ciphertext = encrypt(plaintext)
    assert isinstance(ciphertext, bytes)
    assert ciphertext != plaintext.encode("utf-8")
    assert decrypt(ciphertext) == plaintext


def test_missing_key_fails_closed(monkeypatch):
    monkeypatch.delenv("INVINCIBLE_CREDENTIAL_KEY", raising=False)
    with pytest.raises(CredentialKeyError):
        encrypt("anything")
    with pytest.raises(CredentialKeyError):
        decrypt(b"not-a-token")


def test_malformed_key_fails_closed(monkeypatch):
    monkeypatch.setenv("INVINCIBLE_CREDENTIAL_KEY", "not-a-valid-fernet-key")
    with pytest.raises(CredentialKeyError):
        encrypt("anything")


def test_wrong_key_raises_decrypt_error(fernet_key, monkeypatch):
    ciphertext = encrypt("secret-value")
    other = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("INVINCIBLE_CREDENTIAL_KEY", other)
    with pytest.raises(CredentialDecryptError) as exc_info:
        decrypt(ciphertext)
    # Message names the mismatch only - no ciphertext or key material.
    msg = str(exc_info.value)
    assert "INVINCIBLE_CREDENTIAL_KEY" in msg
    assert "secret-value" not in msg
    assert fernet_key not in msg
    assert other not in msg
