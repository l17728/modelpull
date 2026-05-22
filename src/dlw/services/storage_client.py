"""Controller-side storage client + config decode (Phase 4 GC). Mirrors the
recovery.py S3 pattern; isolated so the GC loop can build clients per backend."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import boto3
from botocore.config import Config

from dlw.db.models.storage import StorageBackend
from dlw.schemas.storage import StorageConfig

logger = logging.getLogger(__name__)


def storage_config_from_backend(backend: StorageBackend) -> StorageConfig:
    raw = bytes(backend.config_encrypted) if backend.config_encrypted else b"{}"
    try:
        cfg = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        cfg = {}
    cfg.setdefault("bucket", backend.name)
    cfg.setdefault("region", backend.region or "us-east-1")
    return StorageConfig(**cfg)


def make_s3_client(cfg: StorageConfig) -> Any:
    return boto3.client(
        "s3", region_name=cfg.region, endpoint_url=cfg.endpoint_url,
        config=Config(region_name=cfg.region, s3={"addressing_style": "path"}))


async def delete_object_silently(client: Any, bucket: str, key: str) -> bool:
    """Best-effort delete. Returns True on success (or already-absent)."""
    try:
        await asyncio.to_thread(
            lambda: client.delete_object(Bucket=bucket, Key=key))
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("gc delete_object failed (Bucket=%s Key=%s): %s",
                       bucket, key, e)
        return False
