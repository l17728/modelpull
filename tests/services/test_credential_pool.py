"""v2.1 Sprint 12 — credential_pool tests.

Pure-function coverage: round-trip encrypt/decrypt, magic-byte detection,
plaintext passthrough, and CredentialPool's structured returns. No DB
needed — the pool's methods take their inputs as plain bytes / values
the caller already pulled from a session."""
from __future__ import annotations

import base64
import json
import os

import pytest
from cryptography.fernet import Fernet

from dlw.services.credential_pool import (
    ENVELOPE_MAGIC,
    CredentialPool,
    _CryptoError,
    _reset_pool_for_tests,
    decrypt_config,
    encrypt_config,
    get_pool,
)


@pytest.fixture
def fernet_key() -> str:
    """A fresh 32-byte urlsafe-b64 key for each test."""
    return Fernet.generate_key().decode("utf-8")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Pool reads DLW_CONFIG_KEY at every call — make sure tests are
    isolated. Each test that wants encryption sets the var itself."""
    monkeypatch.delenv("DLW_CONFIG_KEY", raising=False)
    _reset_pool_for_tests()
    yield
    _reset_pool_for_tests()


# ---------------------------------------------------------------------------
# encrypt_config / decrypt_config: round-trip + edge cases

def test_round_trip_with_key(monkeypatch, fernet_key):
    monkeypatch.setenv("DLW_CONFIG_KEY", fernet_key)
    plaintext = b'{"bucket": "my-bucket", "secret": "s3cr3t"}'
    wrapped = encrypt_config(plaintext)
    assert wrapped.startswith(ENVELOPE_MAGIC)
    assert wrapped != plaintext  # not just passthrough
    unwrapped = decrypt_config(wrapped)
    assert unwrapped == plaintext


def test_encrypt_without_key_returns_plaintext(monkeypatch):
    """No DLW_CONFIG_KEY → encryption is off, plaintext flows through."""
    monkeypatch.delenv("DLW_CONFIG_KEY", raising=False)
    plaintext = b'{"k": "v"}'
    assert encrypt_config(plaintext) == plaintext


def test_decrypt_plaintext_passes_through(monkeypatch):
    """Legacy v2.0 rows are bare JSON bytes — decrypt must return them
    unchanged whether or not a key is configured."""
    monkeypatch.delenv("DLW_CONFIG_KEY", raising=False)
    legacy = b'{"old": "row"}'
    assert decrypt_config(legacy) == legacy


def test_decrypt_plaintext_with_key_passes_through(monkeypatch, fernet_key):
    """Even with a key configured, non-magic-prefixed blobs are treated
    as plaintext — required for the rolling re-encryption migration."""
    monkeypatch.setenv("DLW_CONFIG_KEY", fernet_key)
    legacy = b'{"old": "row"}'
    assert decrypt_config(legacy) == legacy


def test_decrypt_empty_blob_returns_empty():
    assert decrypt_config(b"") == b""


def test_decrypt_envelope_without_key_raises(monkeypatch, fernet_key):
    """If a row is envelope-encrypted but the key is missing, we must
    raise — silently returning the ciphertext would corrupt downstream."""
    monkeypatch.setenv("DLW_CONFIG_KEY", fernet_key)
    wrapped = encrypt_config(b"secret")
    monkeypatch.delenv("DLW_CONFIG_KEY")
    with pytest.raises(_CryptoError):
        decrypt_config(wrapped)


def test_decrypt_envelope_with_wrong_key_raises(monkeypatch, fernet_key):
    """Rotated key without re-encrypting → loud failure, not silent."""
    monkeypatch.setenv("DLW_CONFIG_KEY", fernet_key)
    wrapped = encrypt_config(b"secret")
    other_key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("DLW_CONFIG_KEY", other_key)
    with pytest.raises(_CryptoError):
        decrypt_config(wrapped)


def test_malformed_key_logs_and_falls_back_to_plaintext(monkeypatch):
    monkeypatch.setenv("DLW_CONFIG_KEY", "this-is-not-a-valid-fernet-key")
    plaintext = b'{"k": "v"}'
    # encrypt_config swallows the malformed-key error and returns plaintext
    result = encrypt_config(plaintext)
    assert result == plaintext


# ---------------------------------------------------------------------------
# CredentialPool

def test_pool_get_hf_token_returns_default():
    pool = CredentialPool(default_hf_token="hf_xxx")
    assert pool.get_hf_token() == "hf_xxx"
    assert pool.get_hf_token(tenant_id=1) == "hf_xxx"
    assert pool.get_hf_token(tenant_id=42) == "hf_xxx"


def test_pool_get_hf_token_no_default():
    pool = CredentialPool(default_hf_token=None)
    assert pool.get_hf_token() is None


def test_pool_decrypts_storage_config_with_key(monkeypatch, fernet_key):
    """End-to-end: create a row with envelope encryption, the pool
    decrypts + parses + surfaces bucket/region."""
    monkeypatch.setenv("DLW_CONFIG_KEY", fernet_key)
    cfg_json = json.dumps({
        "bucket": "weights-bucket",
        "region": "ap-east-1",
        "secret": "redacted"}).encode("utf-8")
    blob = encrypt_config(cfg_json)
    pool = CredentialPool()
    creds = pool.get_storage_credentials(
        storage_id=1, backend_type="s3",
        bucket="fallback", region="us-east-1",
        config_encrypted=blob)
    assert creds.bucket == "weights-bucket"
    assert creds.region == "ap-east-1"
    assert creds.config["secret"] == "redacted"


def test_pool_returns_passed_bucket_when_config_missing():
    """Empty config_encrypted → bucket/region come from the explicit
    args (the v2.0 fallback path)."""
    pool = CredentialPool()
    creds = pool.get_storage_credentials(
        storage_id=2, backend_type="s3",
        bucket="explicit-bucket", region="explicit-region",
        config_encrypted=b"")
    assert creds.bucket == "explicit-bucket"
    assert creds.region == "explicit-region"


def test_pool_handles_invalid_json_gracefully():
    """Garbage in config_encrypted should NOT crash — log + return
    {} so the rest of the system limps along on defaults."""
    pool = CredentialPool()
    creds = pool.get_storage_credentials(
        storage_id=3, backend_type="s3",
        bucket="b", region="r",
        config_encrypted=b"not json at all !!!")
    assert creds.bucket == "b"
    assert creds.region == "r"
    assert creds.config == {}


def test_pool_singleton_caches_until_reset(monkeypatch):
    """get_pool() returns the same instance until _reset_pool_for_tests
    is called — important for the per-test isolation pattern."""
    monkeypatch.setattr(
        "dlw.config.get_settings",
        lambda: type("S", (), {"hf_token": "token-1"})())
    p1 = get_pool()
    assert p1.get_hf_token() == "token-1"
    # Same instance returned without rebuild
    p2 = get_pool()
    assert p1 is p2
    _reset_pool_for_tests()
    monkeypatch.setattr(
        "dlw.config.get_settings",
        lambda: type("S", (), {"hf_token": "token-2"})())
    p3 = get_pool()
    assert p3 is not p1
    assert p3.get_hf_token() == "token-2"
