"""Recovery routine for crashed / stale executors.

Phase 2 W1 scope:
  - verify_remote_state: head + size three-way (sha256 deferred to P2-W2)
  - run_recovery_routine: startup-once routine (recovers in-flight uploads,
    resets long-assigned, cleans orphan multiparts)
  - reclaim_stale_executors: periodic scan; marks unhealthy + reclaims

Companion spec: docs/superpowers/specs/2026-05-11-phase-2-week-1-fence-token-recovery-design.md
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError       # W6-A: boto3 raises this, not s3.exceptions.ClientError
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.executor import Executor
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.schemas.storage import StorageConfig
from dlw.services.scheduler import reclaim_subtasks

logger = logging.getLogger(__name__)


@dataclass
class RecoveryStats:
    three_way_checked: int = 0
    verified_recovered: int = 0
    reset_to_pending: int = 0
    size_mismatch_purged: int = 0
    no_multipart_reset: int = 0
    orphan_aborted: int = 0

    def as_dict(self) -> dict[str, int]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


async def _load_storage_config(
    session: AsyncSession, sub: FileSubTask
) -> tuple[StorageConfig, DownloadTask]:
    """Resolve sub → DownloadTask → StorageBackend → StorageConfig."""
    parent = await session.get(DownloadTask, sub.task_id)
    if parent is None:
        raise RuntimeError(f"task {sub.task_id} missing (FK should have caught this)")
    storage = await session.get(StorageBackend, parent.storage_id)
    if storage is None:
        raise RuntimeError(f"storage backend {parent.storage_id} missing")

    raw = bytes(storage.config_encrypted) if storage.config_encrypted else b"{}"
    try:
        cfg_dict = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        cfg_dict = {}
    cfg_dict.setdefault("bucket", storage.name)
    cfg_dict.setdefault("region", storage.region or "us-east-1")
    return StorageConfig(**cfg_dict), parent


def _compose_key(
    parent: DownloadTask, sub: FileSubTask, storage_cfg: StorageConfig
) -> str:
    prefix = storage_cfg.key_prefix.strip("/")
    parts = [p for p in (prefix, parent.repo_id, parent.revision, sub.filename) if p]
    return "/".join(parts)


def _make_s3_client(cfg: StorageConfig) -> Any:
    boto_cfg = Config(
        region_name=cfg.region,
        s3={"addressing_style": "path"},
    )
    return boto3.client(
        "s3",
        region_name=cfg.region,
        endpoint_url=cfg.endpoint_url,
        config=boto_cfg,
    )


async def verify_remote_state(
    session: AsyncSession, sub: FileSubTask,
) -> Literal["verified", "missing", "size_mismatch"]:
    """Phase 1 three-way: head + size. SHA256 deferred to P2-W2 ChecksumSHA256."""
    storage_cfg, parent = await _load_storage_config(session, sub)
    s3 = _make_s3_client(storage_cfg)
    key = _compose_key(parent, sub, storage_cfg)

    try:
        head = await asyncio.to_thread(
            lambda: s3.head_object(Bucket=storage_cfg.bucket, Key=key)
        )
    except ClientError as e:        # W6-A: botocore.exceptions.ClientError (NOT s3.exceptions.ClientError)
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return "missing"
        raise

    remote_size = head.get("ContentLength", 0)
    if sub.file_size is not None and remote_size != sub.file_size:
        return "size_mismatch"

    return "verified"


async def run_recovery_routine(
    session: AsyncSession,
    executor_id: str,
    heartbeat_interval: timedelta = timedelta(seconds=30),
) -> RecoveryStats:
    """Startup-once recovery routine. Placeholder body for P2-W1 skeleton."""
    stats = RecoveryStats()
    return stats


async def reclaim_stale_executors(
    session: AsyncSession,
    heartbeat_interval: timedelta = timedelta(seconds=30),
    stale_multiplier: int = 3,
) -> RecoveryStats:
    """Periodic scan: mark unhealthy executors + reclaim their subtasks. Placeholder for P2-W1 skeleton."""
    stats = RecoveryStats()
    return stats
