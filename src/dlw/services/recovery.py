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


async def _abort_multipart_silently(
    s3: Any, bucket: str, key: str, upload_id: str,
) -> None:
    """Swallow ClientError; S3 lifecycle is the safety net (Phase 3 ops)."""
    try:
        await asyncio.to_thread(
            lambda: s3.abort_multipart_upload(
                Bucket=bucket, Key=key, UploadId=upload_id,
            )
        )
    except Exception as e:
        logger.warning(
            "abort_multipart_upload failed (Bucket=%s Key=%s UploadId=%s): %s",
            bucket, key, upload_id, e,
        )


async def _delete_object_silently(
    s3: Any, bucket: str, key: str,
) -> None:
    try:
        await asyncio.to_thread(
            lambda: s3.delete_object(Bucket=bucket, Key=key)
        )
    except Exception as e:
        logger.warning("delete_object failed (Bucket=%s Key=%s): %s", bucket, key, e)


def _reset_subtask_to_pending(sub: FileSubTask) -> None:
    sub.status = "pending"
    sub.executor_id = None
    sub.executor_epoch = None
    sub.assignment_token = None
    sub.assigned_at = None
    sub.multipart_upload_id = None


async def _maybe_transition_parent(session: AsyncSession, task_id: Any) -> None:
    """W6-C: After a recovery flip of subtask.status, check parent task.

    Mirrors the tail of complete_subtask: if all siblings succeeded, mark
    parent succeeded; if any failed, mark parent failed. Locking the parent
    FOR UPDATE prevents two recovery iterations from racing on the same task.
    """
    parent = await session.get(DownloadTask, task_id, with_for_update=True)
    if parent is None:
        return
    siblings = (await session.execute(
        select(FileSubTask).where(FileSubTask.task_id == task_id)
    )).scalars().all()
    statuses = {s.status for s in siblings}
    if "failed" in statuses:
        parent.status = "failed"
        parent.completed_at = datetime.now(UTC)
    elif statuses == {"succeeded"}:
        parent.status = "succeeded"
        parent.completed_at = datetime.now(UTC)


async def run_recovery_routine(session: AsyncSession) -> RecoveryStats:
    """One-shot startup recovery. Must complete before serving traffic.

    W6-G note: Step 1 filters WHERE status='assigned' AND multipart_upload_id
    IS NOT NULL. In Phase 1 production this returns ZERO rows because W4's
    HfS3StreamDownloader never persists multipart_upload_id to DB (transient
    in memory only). Step 1 is infrastructure for P2-W2 multipart-resume.
    Tests seed synthetic state to validate the logic ahead of P2-W2 wiring.

    Phase 1 simplification: file_subtasks uses only pending/assigned/
    succeeded/failed/cancelled. The intermediate downloading/uploading/
    verifying_remote states from spec §3 don't exist yet — those arrive in
    P2-W2. So this routine:
      1. three-way (head + size) for status='assigned' WITH multipart_upload_id
         (executor crashed mid-upload)
      2. resets status='assigned' WITHOUT multipart_upload_id whose assigned_at
         is stale (executor crashed before multipart started)
      3. cleanup orphan multipart_upload_ids on terminal subtasks

    W6-E: do NOT await session.commit() here — caller commits.
    """
    stats = RecoveryStats()
    threshold = datetime.now(UTC) - timedelta(seconds=120)  # 2× heartbeat default

    # Step 1: three-way for in-flight with multipart
    in_flight = (await session.execute(
        select(FileSubTask)
          .where(FileSubTask.status == "assigned")
          .where(FileSubTask.multipart_upload_id.is_not(None))
    )).scalars().all()

    for sub in in_flight:
        try:
            storage_cfg, parent = await _load_storage_config(session, sub)
            s3 = _make_s3_client(storage_cfg)
            key = _compose_key(parent, sub, storage_cfg)
        except RuntimeError:
            # Orphan: parent or storage missing — log + skip
            logger.warning(
                "recovery: subtask %s missing parent/storage; skipping", sub.id,
            )
            continue

        try:
            result = await verify_remote_state(session, sub)
        except Exception as e:
            logger.warning(
                "recovery: verify_remote_state %s failed: %s; skipping", sub.id, e,
            )
            continue

        stats.three_way_checked += 1
        if result == "verified":
            sub.status = "succeeded"
            sub.completed_at = datetime.now(UTC)
            # W6-C: trigger parent-task transition (otherwise recovery-completed
            # subtasks leave the parent stuck pending/downloading forever).
            await _maybe_transition_parent(session, sub.task_id)
            stats.verified_recovered += 1
        elif result == "missing":
            if sub.multipart_upload_id:
                await _abort_multipart_silently(
                    s3, storage_cfg.bucket, key, sub.multipart_upload_id,
                )
            _reset_subtask_to_pending(sub)
            stats.reset_to_pending += 1
        else:  # size_mismatch
            if sub.multipart_upload_id:
                await _abort_multipart_silently(
                    s3, storage_cfg.bucket, key, sub.multipart_upload_id,
                )
            await _delete_object_silently(s3, storage_cfg.bucket, key)
            _reset_subtask_to_pending(sub)
            stats.size_mismatch_purged += 1

    # Step 2: reset long-assigned without multipart
    n = (await session.execute(
        update(FileSubTask)
        .where(FileSubTask.status == "assigned")
        .where(FileSubTask.multipart_upload_id.is_(None))
        .where(
            or_(
                FileSubTask.assigned_at.is_(None),
                FileSubTask.assigned_at < threshold,
            )
        )
        .values(
            status="pending",
            executor_id=None,
            executor_epoch=None,
            assignment_token=None,
            assigned_at=None,
        )
    )).rowcount or 0
    stats.no_multipart_reset = n

    # Step 3: cleanup orphan multipart uploads on terminal subtasks
    orphans = (await session.execute(
        select(FileSubTask)
          .where(FileSubTask.multipart_upload_id.is_not(None))
          .where(FileSubTask.status.in_(["succeeded", "failed", "cancelled"]))
    )).scalars().all()
    for sub in orphans:
        try:
            storage_cfg, parent = await _load_storage_config(session, sub)
            s3 = _make_s3_client(storage_cfg)
            key = _compose_key(parent, sub, storage_cfg)
            await _abort_multipart_silently(
                s3, storage_cfg.bucket, key, sub.multipart_upload_id,
            )
        except RuntimeError:
            logger.warning("recovery: orphan multipart on missing parent; skipping")
        sub.multipart_upload_id = None
        stats.orphan_aborted += 1

    # W6-E: do NOT commit here — caller (lifespan or test) commits.
    # Phase 1 W2 pattern: scheduler functions don't commit internally.
    logger.info("recovery_routine done: %s", stats.as_dict())
    return stats


async def sweep_executor_timeouts(
    session: AsyncSession,
    *,
    heartbeat_threshold_seconds: int = 90,
) -> dict[str, int]:
    """Per 03 §5: scan executors that missed heartbeats; transition through
    the state machine; reclaim subtasks only on entry to suspect / faulty.

    Returns observability counters: {"transitioned": N, "reclaimed": M}.
    Caller commits.
    """
    from dlw.services.state_machine import transition_executor   # local: avoids cycle

    threshold = datetime.now(UTC) - timedelta(seconds=heartbeat_threshold_seconds)
    candidates = (await session.execute(
        select(Executor)
        .where(Executor.status.in_(("healthy", "degraded", "suspect")))
        .where(Executor.last_heartbeat_at < threshold)
        .with_for_update(skip_locked=True)
    )).scalars().all()

    counters = {"transitioned": 0, "reclaimed": 0}
    for ex in candidates:
        t = await transition_executor(
            session, ex,
            event="heartbeat_timeout",
            reason="sweep_timeout",
            metadata={"threshold_s": heartbeat_threshold_seconds},
        )
        if t is None:
            continue
        counters["transitioned"] += 1
        if t.to_status in ("suspect", "faulty"):
            counters["reclaimed"] += await reclaim_subtasks(session, ex.id, ex.epoch)
    return counters
