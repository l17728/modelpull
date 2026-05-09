"""Storage backend DTOs.

Phase 1 W4: StorageConfig is the decrypted view of
storage_backends.config_encrypted (which Phase 1 stores as plain JSON bytes).
Phase 3 plan introduces envelope encryption — magic-byte prefix detection
will keep this Pydantic model unchanged.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class StorageConfig(BaseModel):
    """Decrypted Phase 1 storage backend config; embedded in /poll response."""
    bucket: str = Field(min_length=1, max_length=128)
    region: str = Field(default="us-east-1", max_length=64)
    endpoint_url: str | None = Field(default=None, max_length=256)
    key_prefix: str = Field(default="", max_length=512)
