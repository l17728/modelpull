"""v2.1 Sprint 12 — Centralized credential pool + envelope encryption.

The threat model
----------------
Production secrets (HF tokens, S3 secret keys, etc.) must NEVER land on
an executor host. v2.0 already shipped reverse proxies (hf_proxy,
source_proxy) so executors fetch remote bytes via the controller and
never see the bearer. This sprint adds the controller-side hygiene:

  1. A single chokepoint (CredentialPool) for "give me the token for
     tenant T's HF requests" / "give me the S3 creds for storage S".
     Today the answer comes from app config; future deployments can
     swap in an AWS-Secrets-Manager / Vault backend without touching
     the call sites.

  2. Envelope-encryption helpers for the `storage_backends.config_encrypted`
     column. Existing rows are plaintext UTF-8 JSON (per the v2.0
     "Phase 1 accepts placeholder bytes" comment). New rows can be
     wrapped with a magic-byte prefix; the decrypt helper auto-detects
     and falls back to plaintext, so the migration is non-breaking
     and reversible per-row.

The two layers are decoupled: encryption helpers are pure-function and
testable on their own; the pool composes them with config lookups.

Key management
--------------
The wrapping key comes from DLW_CONFIG_KEY (Fernet-format base64).
If unset, encryption is OFF and the pool returns plaintext — same
behavior as v2.0. This preserves the opt-in semantics already documented
for dlw.sdk._crypto."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# 4-byte magic prefix that marks a config_encrypted blob as Fernet-wrapped.
# Chosen so it can't collide with a valid JSON byte: '{' / '[' / whitespace /
# digit / minus. 0x1B (ESC) is in the ASCII control range and won't appear
# as the first byte of any JSON value.
ENVELOPE_MAGIC = b"\x1bDLW"


class _CryptoError(RuntimeError):
    """Raised on envelope decryption failure that doesn't look like an
    accidentally-plaintext row."""


def _key_from_env() -> bytes | None:
    """Return the configured Fernet key (still in url-safe-b64 string
    form) or None if encryption is off.

    The key is read at every call so a key rotation is picked up on the
    next request without restarting the controller — useful in the same
    way DLW_ADAPTIVE_OPTIMIZER_ENABLED is hot-reloaded for the replan loop."""
    raw = os.environ.get("DLW_CONFIG_KEY")
    if not raw:
        return None
    return raw.strip().encode("utf-8")


def encrypt_config(plaintext: bytes) -> bytes:
    """Wrap plaintext in ENVELOPE_MAGIC + Fernet(plaintext). If no key is
    configured, return plaintext unchanged so callers don't need a
    feature flag — encryption is best-effort opt-in."""
    key = _key_from_env()
    if key is None:
        return plaintext
    try:
        f = Fernet(key)
    except (ValueError, TypeError) as e:
        logger.warning(
            "credential_pool: DLW_CONFIG_KEY is malformed (%s); storing plaintext", e)
        return plaintext
    return ENVELOPE_MAGIC + f.encrypt(plaintext)


def decrypt_config(blob: bytes) -> bytes:
    """Inverse of encrypt_config. Auto-detects:
      - empty blob → empty bytes
      - starts with ENVELOPE_MAGIC → Fernet-decrypt
      - anything else → assume plaintext (v2.0 rows), return unchanged

    Raises _CryptoError only when a row IS magic-prefixed but the key
    can't decrypt it (rotated key without re-encrypting, or tampering)."""
    if not blob:
        return b""
    if not blob.startswith(ENVELOPE_MAGIC):
        return blob  # legacy plaintext path
    payload = blob[len(ENVELOPE_MAGIC):]
    key = _key_from_env()
    if key is None:
        raise _CryptoError(
            "config row is envelope-encrypted but DLW_CONFIG_KEY is not set")
    try:
        f = Fernet(key)
        return f.decrypt(payload)
    except (InvalidToken, ValueError, TypeError) as e:
        raise _CryptoError(f"envelope decrypt failed: {e}") from e


# ---------------------------------------------------------------------------
# CredentialPool — sole entry point call sites should use.

@dataclass(frozen=True)
class StorageCredentials:
    """Decrypted view of one storage_backends.config_encrypted row.
    config is the parsed JSON; bucket / region are surfaced because
    every call site needs them."""
    storage_id: int
    backend_type: str
    bucket: str
    region: str
    config: dict[str, Any]


class CredentialPool:
    """Stateless facade over config + DB lookups. Future backends (Vault,
    AWS Secrets Manager) plug in by subclassing or by swapping the
    module-level singleton."""

    def __init__(self, *, default_hf_token: str | None = None) -> None:
        self._default_hf_token = default_hf_token

    def get_hf_token(self, *, tenant_id: int | None = None) -> str | None:
        """Return the HF Bearer token. Sprint 12 ships the global default;
        per-tenant overrides are a Sprint 14 follow-on (would query a
        tenant-tokens table and fall back to the global)."""
        return self._default_hf_token

    def get_storage_credentials(
        self, *, storage_id: int, backend_type: str, bucket: str,
        region: str | None, config_encrypted: bytes,
    ) -> StorageCredentials:
        """Decrypt + parse one storage backend's config_encrypted blob.
        Raises _CryptoError if the blob is envelope-tagged but the key
        can't decrypt; the API layer should surface 503 in that case."""
        try:
            decrypted = decrypt_config(config_encrypted)
        except _CryptoError:
            # Re-raise so callers can convert to HTTP 503, but log here
            # so the failure is visible in audit even if the caller
            # swallows the exception.
            logger.exception(
                "credential_pool: failed to decrypt storage %d config",
                storage_id)
            raise
        if not decrypted:
            cfg: dict[str, Any] = {}
        else:
            try:
                cfg = json.loads(decrypted.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                logger.warning(
                    "credential_pool: storage %d config is not valid JSON: %s",
                    storage_id, e)
                cfg = {}
        return StorageCredentials(
            storage_id=storage_id,
            backend_type=backend_type,
            bucket=cfg.get("bucket", bucket),
            region=cfg.get("region", region or "us-east-1"),
            config=cfg)


# Module-level singleton — instantiated lazily so test setup can
# install a different default before first use.
_POOL: CredentialPool | None = None


def get_pool() -> CredentialPool:
    """Return the singleton CredentialPool, building it from settings on
    first call. Tests can call _reset_pool_for_tests() to force a
    rebuild after monkeypatching env."""
    global _POOL
    if _POOL is None:
        # Lazy import — get_settings() reads .env, we don't want that at
        # module import time during e.g. alembic migrations.
        from dlw.config import get_settings
        _POOL = CredentialPool(default_hf_token=get_settings().hf_token)
    return _POOL


def _reset_pool_for_tests() -> None:
    global _POOL
    _POOL = None
