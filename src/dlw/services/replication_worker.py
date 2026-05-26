"""v2.1 Sprint 5 — Cross-region replication byte transfer worker.

execute_job() copies one storage_object from its source S3-shape backend
to a target backend, verifies sha256, records the new target StorageObject
row, and transitions the ReplicationJob through the documented state machine:

    pending ──claim──> running ──ok──> succeeded
                          │
                          ├──same sha already at target──> skipped_existing
                          ├──cancelled mid-flight──> cancelled
                          └──3 retries exhausted──> failed

Concurrency model
-----------------
The worker_loop() picks one pending job per tick with FOR UPDATE SKIP LOCKED
so multiple loops are safe to run side-by-side. Inside execute_job() the long
byte-copy phase holds NO database transaction open — bytes_transferred
progress is committed in short bursts via the on_progress callback.

Cancellation
------------
Each progress tick re-reads ReplicationJob.status; if it has flipped to
'cancelled' (by the REST cancel endpoint) we abort cleanly with status
'cancelled' (NOT 'failed'). Already-committed bytes_transferred stays as
the user-visible last value.

Skip-existing race
------------------
The StorageObject UniqueConstraint on (tenant_id, storage_id, sha256) is
the source of truth. Two workers racing the same (object → target) row
will have one succeed and the other rollback to 'skipped_existing'."""
from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dlw.db.models.replication import ReplicationJob
from dlw.db.models.storage import StorageBackend
from dlw.db.models.storage_object import StorageObject
from dlw.services.audit import write_audit
from dlw.services.storage_client import (
    make_s3_client,
    storage_config_from_backend,
)

logger = logging.getLogger(__name__)

# How much we read per S3 chunk. Picked to balance memory pressure (we hold
# the whole object in RAM before the PutObject — MPU is a Sprint-5.5 follow-up)
# against the number of progress callbacks per job. 8MB is the boto3 default
# multipart_chunksize and behaves well on both Linux and Windows.
DEFAULT_CHUNK_BYTES = 8 * 1024 * 1024

# Default per-tenant ceiling — megabytes/second (decimal MB, matching S3
# reporting conventions). Conservatively chosen so a single worker can't
# saturate a 1 Gbps uplink even with multiple parallel jobs.
DEFAULT_BANDWIDTH_MBPS = 100.0

# Number of attempts before a job is marked 'failed'. The first attempt is
# attempt 0. Exponential backoff between attempts: 1s, 2s.
MAX_RETRY = 3


class TransferAborted(RuntimeError):
    """Job status flipped to 'cancelled' while bytes were in flight."""


class StreamingClient(Protocol):
    """boto3 S3 client shape; declared so tests can stub without depending
    on a live S3 endpoint."""

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]: ...
    def put_object(self, *, Bucket: str, Key: str, Body: bytes,
                    ContentLength: int | None = ...) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ClientCtx:
    """Pair of (client, bucket) so call sites don't carry two args."""
    client: StreamingClient
    bucket: str


MakeClientFn = Callable[[StorageBackend], ClientCtx]


def default_make_client(backend: StorageBackend) -> ClientCtx:
    """Build a real boto3 client + bucket name from a StorageBackend row."""
    cfg = storage_config_from_backend(backend)
    client = make_s3_client(cfg)
    return ClientCtx(client=client, bucket=cfg.bucket)


@dataclass
class ExecuteJobResult:
    """Returned by execute_job() — callers (worker loop, tests) inspect
    these instead of re-reading the DB row."""
    status: str
    bytes_transferred: int
    error_message: str | None = None
    retries: int = 0


def _now() -> datetime:
    return datetime.now(UTC)


def _bandwidth_sleep_seconds(chunk_bytes: int, mbps: float) -> float:
    """Token-bucket-equivalent: time to sleep after writing chunk_bytes so
    the effective rate matches mbps. mbps == decimal megabytes/sec."""
    if mbps <= 0:
        return 0.0
    return chunk_bytes / (mbps * 1_000_000.0)


async def _read_object_streaming(
    *, source: ClientCtx, source_key: str,
    expected_size: int, expected_sha256: str,
    on_progress: Callable[[int], Awaitable[bool]],
    bandwidth_mbps: float,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
) -> bytes:
    """Read source object in chunks, hash + size check, return full bytes.

    on_progress(bytes_so_far) is awaited per chunk; if it returns False the
    function raises TransferAborted so the caller can transition the job
    to 'cancelled' without committing a target write."""
    def _get_body() -> Any:
        resp = source.client.get_object(Bucket=source.bucket, Key=source_key)
        return resp["Body"]

    body = await asyncio.to_thread(_get_body)

    hasher = hashlib.sha256()
    buf = bytearray()
    transferred = 0
    while True:
        chunk = await asyncio.to_thread(body.read, chunk_bytes)
        if not chunk:
            break
        buf.extend(chunk)
        hasher.update(chunk)
        transferred += len(chunk)
        if not await on_progress(transferred):
            raise TransferAborted("cancelled mid-transfer")
        await asyncio.sleep(_bandwidth_sleep_seconds(len(chunk), bandwidth_mbps))

    actual_sha = hasher.hexdigest()
    if actual_sha != expected_sha256:
        raise ValueError(
            f"sha256 mismatch on source read: "
            f"expected {expected_sha256!r}, got {actual_sha!r}")
    if transferred != expected_size:
        raise ValueError(
            f"size mismatch on source read: "
            f"expected {expected_size}, got {transferred}")
    return bytes(buf)


async def _write_object(
    *, target: ClientCtx, target_key: str, payload: bytes,
) -> None:
    """Put the buffered payload to target. Single-shot PutObject; MPU is
    a Sprint-5.5 follow-up for >100MB objects."""
    def _put() -> None:
        target.client.put_object(
            Bucket=target.bucket, Key=target_key,
            Body=payload, ContentLength=len(payload))
    await asyncio.to_thread(_put)


async def _check_skip_existing(
    session: AsyncSession, *, tenant_id: int, target_storage_id: int,
    sha256: str,
) -> StorageObject | None:
    """Returns target StorageObject if same sha is already replicated."""
    stmt = select(StorageObject).where(
        StorageObject.tenant_id == tenant_id,
        StorageObject.storage_id == target_storage_id,
        StorageObject.sha256 == sha256)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _is_cancelled(
    session_factory: async_sessionmaker, job_id: int,
) -> bool:
    """Re-read job.status to detect mid-flight cancellation. Opens its own
    short session so it doesn't block the in-flight transfer's own session."""
    async with session_factory() as s:
        job = await s.get(ReplicationJob, job_id)
        return job is None or job.status == "cancelled"


async def _commit_progress(
    session_factory: async_sessionmaker, job_id: int, bytes_so_far: int,
) -> bool:
    """Persist bytes_transferred and return False if the job was cancelled."""
    async with session_factory() as s:
        job = await s.get(ReplicationJob, job_id)
        if job is None or job.status == "cancelled":
            return False
        job.bytes_transferred = bytes_so_far
        await s.commit()
    return True


@dataclass(frozen=True)
class _JobSnapshot:
    """Fields we need post-session for the long transfer phase."""
    job_id: int
    tenant_id: int
    source_backend: StorageBackend
    target_backend: StorageBackend
    source_key: str
    target_key: str
    sha256: str
    size: int
    target_storage_id: int


async def _claim_and_snapshot(
    session: AsyncSession, *, job_id: int,
) -> tuple[_JobSnapshot, str] | tuple[None, str]:
    """Validate + transition pending → running in one short transaction.

    Returns either ``(snapshot, "running")`` to continue the transfer, or
    ``(None, terminal_status)`` if the job short-circuits (already in a
    terminal state, source/target missing, or same sha already replicated)."""
    job = await session.get(ReplicationJob, job_id)
    if job is None:
        return (None, "not_found")
    if job.status != "pending":
        return (None, job.status)

    source_obj = await session.get(StorageObject, job.source_object_id)
    if source_obj is None:
        job.status = "failed"
        job.error_message = "source object disappeared"
        job.completed_at = _now()
        return (None, "failed")

    target_backend = await session.get(StorageBackend, job.target_storage_id)
    if target_backend is None:
        job.status = "failed"
        job.error_message = "target storage disappeared"
        job.completed_at = _now()
        return (None, "failed")

    source_backend = await session.get(StorageBackend, source_obj.storage_id)
    if source_backend is None:
        job.status = "failed"
        job.error_message = "source storage backend disappeared"
        job.completed_at = _now()
        return (None, "failed")

    existing = await _check_skip_existing(
        session, tenant_id=job.tenant_id,
        target_storage_id=job.target_storage_id,
        sha256=source_obj.sha256)
    if existing is not None:
        job.status = "skipped_existing"
        job.started_at = job.started_at or _now()
        job.completed_at = _now()
        await write_audit(
            session, action="replication.job.skipped_existing",
            resource_type="replication_job", resource_id=str(job.id),
            outcome="success", tenant_id=job.tenant_id,
            actor_user_id=None,
            payload={"sha256": source_obj.sha256})
        return (None, "skipped_existing")

    # Happy path — claim and snapshot
    job.status = "running"
    job.started_at = _now()
    snapshot = _JobSnapshot(
        job_id=job.id,
        tenant_id=job.tenant_id,
        source_backend=source_backend,
        target_backend=target_backend,
        source_key=source_obj.storage_key,
        target_key=source_obj.storage_key,  # reuse source key by default
        sha256=source_obj.sha256,
        size=source_obj.size,
        target_storage_id=job.target_storage_id,
    )
    return (snapshot, "running")


async def _finalize_success(
    session: AsyncSession, *, snapshot: _JobSnapshot,
    transferred: int, attempt: int,
) -> str:
    """Phase 3: record target StorageObject + flip job to succeeded.

    UniqueConstraint races (parallel job for same sha) degrade gracefully
    to skipped_existing."""
    job = await session.get(ReplicationJob, snapshot.job_id)
    if job is None:
        return "vanished"
    if job.status == "cancelled":
        return "cancelled"

    final_status: str
    try:
        obj = StorageObject(
            tenant_id=snapshot.tenant_id,
            storage_id=snapshot.target_storage_id,
            storage_key=snapshot.target_key,
            sha256=snapshot.sha256,
            size=snapshot.size,
            refcount=1)
        session.add(obj)
        await session.flush()
    except IntegrityError:
        await session.rollback()
        # Refetch — the session rolled back, drop the local ref.
        job = await session.get(ReplicationJob, snapshot.job_id)
        if job is None:
            return "vanished"
        final_status = "skipped_existing"
    else:
        final_status = "succeeded"

    job.status = final_status
    job.bytes_transferred = transferred
    job.completed_at = _now()
    job.retry_count = attempt
    await write_audit(
        session, action=f"replication.job.{final_status}",
        resource_type="replication_job",
        resource_id=str(job.id), outcome="success",
        tenant_id=snapshot.tenant_id, actor_user_id=None,
        payload={"bytes_transferred": transferred, "attempt": attempt})
    return final_status


async def execute_job(
    session_factory: async_sessionmaker, *, job_id: int,
    make_client: MakeClientFn | None = None,
    bandwidth_mbps: float = DEFAULT_BANDWIDTH_MBPS,
) -> ExecuteJobResult:
    """Drive one job through its full state machine.

    Three-phase structure:
      1. claim — short DB transaction: validate, snapshot, mark running
      2. transfer — no DB transaction held; only on_progress writes
      3. finalize — short DB transaction: record target row, mark terminal

    Retries: transient transfer errors (e.g. boto ClientError) are caught
    and retried up to MAX_RETRY times with exponential backoff. Cancellation
    and sha-mismatch are NEVER retried."""
    if make_client is None:
        make_client = default_make_client

    # Phase 1: claim
    async with session_factory() as session:
        result = await _claim_and_snapshot(session, job_id=job_id)
        await session.commit()
    snapshot, claim_status = result
    if snapshot is None:
        # Short-circuited — terminal state already set by _claim_and_snapshot
        return ExecuteJobResult(status=claim_status, bytes_transferred=0)

    # Phase 2: transfer (no DB session held)
    last_error: str | None = None
    transferred = 0
    for attempt in range(MAX_RETRY):
        try:
            source_ctx = make_client(snapshot.source_backend)
            target_ctx = make_client(snapshot.target_backend)

            async def _on_progress(bytes_so_far: int) -> bool:
                return await _commit_progress(
                    session_factory, snapshot.job_id, bytes_so_far)

            payload = await _read_object_streaming(
                source=source_ctx, source_key=snapshot.source_key,
                expected_size=snapshot.size,
                expected_sha256=snapshot.sha256,
                on_progress=_on_progress,
                bandwidth_mbps=bandwidth_mbps)
            await _write_object(
                target=target_ctx, target_key=snapshot.target_key,
                payload=payload)
            transferred = len(payload)

            # Phase 3: finalize
            async with session_factory() as session:
                final_status = await _finalize_success(
                    session, snapshot=snapshot,
                    transferred=transferred, attempt=attempt)
                await session.commit()
            logger.info(
                "replication job %d %s (%d bytes, attempt %d)",
                snapshot.job_id, final_status, transferred, attempt)
            return ExecuteJobResult(
                status=final_status, bytes_transferred=transferred,
                retries=attempt)

        except TransferAborted:
            # Cancellation is terminal — don't retry, don't write target.
            async with session_factory() as session:
                job = await session.get(ReplicationJob, snapshot.job_id)
                if job is not None and job.status != "cancelled":
                    job.status = "cancelled"
                    job.completed_at = _now()
                await session.commit()
            return ExecuteJobResult(
                status="cancelled", bytes_transferred=transferred,
                retries=attempt)

        except ValueError as e:
            # sha or size mismatch — corrupt source or in-flight tampering.
            # Never retry; it would be the same answer.
            last_error = str(e)
            logger.error(
                "replication job %d integrity check failed: %s",
                snapshot.job_id, last_error)
            break

        except Exception as e:  # noqa: BLE001 — transient transfer errors
            last_error = str(e)
            logger.warning(
                "replication job %d attempt %d failed: %s",
                snapshot.job_id, attempt, last_error)
            if attempt < MAX_RETRY - 1:
                await asyncio.sleep(2 ** attempt)

    # All retries exhausted (or hard integrity error)
    async with session_factory() as session:
        job = await session.get(ReplicationJob, snapshot.job_id)
        if job is not None and job.status not in (
                "cancelled", "succeeded", "skipped_existing"):
            job.status = "failed"
            job.error_message = last_error or "unknown error"
            job.completed_at = _now()
            job.retry_count = MAX_RETRY
            await write_audit(
                session, action="replication.job.failed",
                resource_type="replication_job", resource_id=str(job.id),
                outcome="error", tenant_id=snapshot.tenant_id,
                actor_user_id=None,
                payload={"error": last_error or "unknown",
                          "attempts": MAX_RETRY})
        await session.commit()
    return ExecuteJobResult(
        status="failed", bytes_transferred=transferred,
        error_message=last_error, retries=MAX_RETRY)


async def claim_one_pending(
    session: AsyncSession,
) -> int | None:
    """Pick one pending job (oldest first) with FOR UPDATE SKIP LOCKED so
    multiple workers don't claim the same row. Returns just the id — the
    actual state transition happens inside execute_job to keep all the
    audit + retry logic in one place."""
    stmt = (
        select(ReplicationJob)
        .where(ReplicationJob.status == "pending")
        .order_by(ReplicationJob.id.asc())
        .limit(1)
        .with_for_update(skip_locked=True))
    job = (await session.execute(stmt)).scalar_one_or_none()
    return job.id if job is not None else None


async def worker_loop(
    session_factory: async_sessionmaker, *,
    poll_interval_seconds: float = 5.0,
    bandwidth_mbps: float = DEFAULT_BANDWIDTH_MBPS,
    make_client: MakeClientFn | None = None,
) -> None:
    """Long-running background task: pick one pending job per tick, execute
    it, repeat. Cancellation-safe (CancelledError propagates)."""
    logger.info(
        "replication_worker_loop started "
        "(poll=%.1fs, bandwidth=%.1f MB/s)",
        poll_interval_seconds, bandwidth_mbps)
    while True:
        try:
            await asyncio.sleep(poll_interval_seconds)
            async with session_factory() as session:
                job_id = await claim_one_pending(session)
                await session.commit()
            if job_id is None:
                continue
            await execute_job(
                session_factory, job_id=job_id,
                bandwidth_mbps=bandwidth_mbps,
                make_client=make_client)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("replication_worker_loop tick failed; retrying")
