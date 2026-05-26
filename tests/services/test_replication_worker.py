"""v2.1 Sprint 5 — Replication worker tests.

Uses stub source/target clients (no real S3). The stub mirrors only the
two boto3 calls the worker uses: get_object and put_object."""
from __future__ import annotations

import asyncio
import io
import time
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
from dlw.db.models.replication import ReplicationJob
from dlw.db.models.storage import StorageBackend
from dlw.db.models.storage_object import StorageObject
from dlw.db.models.tenant import Tenant
from dlw.observability.metrics import (
    REPLICATION_BYTES_TOTAL,
    REPLICATION_JOBS_TOTAL,
    _reset_for_tests,
)
from dlw.services.replication_worker import (
    ClientCtx,
    ExecuteJobResult,
    _bandwidth_sleep_seconds,
    claim_one_pending,
    execute_job,
    worker_loop,
)


# ---------------------------------------------------------------------------
# Stub client — minimal boto3 shape

class _BodyReader:
    """Mimics StreamingBody.read(n) — synchronous, used inside to_thread."""

    def __init__(self, data: bytes) -> None:
        self._buf = io.BytesIO(data)

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)


@dataclass
class StubClient:
    """Fake boto3 S3 client. `objects` is a {(bucket, key): bytes} map.

    Tests can pre-seed it for source bucket, then inspect it after the
    worker writes to the target."""
    objects: dict[tuple[str, str], bytes] = field(default_factory=dict)
    fail_get_n_times: int = 0
    fail_put_n_times: int = 0
    _get_attempts: int = 0
    _put_attempts: int = 0

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self._get_attempts += 1
        if self.fail_get_n_times > 0:
            self.fail_get_n_times -= 1
            raise RuntimeError(f"stub: simulated GetObject failure")
        if (Bucket, Key) not in self.objects:
            raise KeyError((Bucket, Key))
        return {"Body": _BodyReader(self.objects[(Bucket, Key)])}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes,
                    ContentLength: int | None = None) -> dict[str, Any]:
        self._put_attempts += 1
        if self.fail_put_n_times > 0:
            self.fail_put_n_times -= 1
            raise RuntimeError(f"stub: simulated PutObject failure")
        self.objects[(Bucket, Key)] = bytes(Body)
        return {"ETag": '"stub"'}


# ---------------------------------------------------------------------------
# Per-module bootstrap

_T_ID = 700
SRC_STORAGE_ID = 700
TGT_STORAGE_ID = 701


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    import dlw.db.models  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=_T_ID, slug="repW", display_name="RepW"))
        await s.flush()
        s.add(StorageBackend(
            id=SRC_STORAGE_ID, tenant_id=_T_ID, name="src-bucket",
            backend_type="s3", config_encrypted=b"", region="us-east-1"))
        s.add(StorageBackend(
            id=TGT_STORAGE_ID, tenant_id=_T_ID, name="tgt-bucket",
            backend_type="s3", config_encrypted=b"", region="ap-east-1"))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
async def _per_test_cleanup(engine):
    """Wipe replication_jobs + storage_objects + reset Prometheus
    counters between tests so they don't leak state into each other."""
    _reset_for_tests()
    yield
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        from sqlalchemy import delete
        await s.execute(delete(ReplicationJob))
        await s.execute(delete(StorageObject))
        await s.commit()


@pytest.fixture
def factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Helpers

async def _seed_source_object(
    factory_, *, payload: bytes, sha256: str | None = None,
) -> StorageObject:
    """Insert a storage_object row representing the source file, return it."""
    import hashlib
    if sha256 is None:
        sha256 = hashlib.sha256(payload).hexdigest()
    async with factory_() as s:
        obj = StorageObject(
            tenant_id=_T_ID, storage_id=SRC_STORAGE_ID,
            storage_key="src/key.bin", sha256=sha256, size=len(payload),
            refcount=1)
        s.add(obj)
        await s.commit()
        await s.refresh(obj)
        return obj


async def _seed_job(
    factory_, *, source_object_id: int, status: str = "pending",
) -> int:
    async with factory_() as s:
        job = ReplicationJob(
            tenant_id=_T_ID, source_object_id=source_object_id,
            target_storage_id=TGT_STORAGE_ID, status=status)
        s.add(job)
        await s.commit()
        return job.id


def _make_client_factory(client: StubClient):
    """A make_client function the worker can call; binds the stub for all
    backends (same client/objects for source + target since this is in-process)."""
    def _make(backend: StorageBackend) -> ClientCtx:
        return ClientCtx(client=client, bucket=backend.name)
    return _make


# ---------------------------------------------------------------------------
# 1. Happy path

async def test_execute_job_happy_path(factory):
    payload = b"hello, replication world!" * 100
    obj = await _seed_source_object(factory, payload=payload)
    job_id = await _seed_job(factory, source_object_id=obj.id)

    client = StubClient(objects={("src-bucket", "src/key.bin"): payload})
    result = await execute_job(
        factory, job_id=job_id,
        make_client=_make_client_factory(client),
        bandwidth_mbps=0)  # 0 = unthrottled

    assert isinstance(result, ExecuteJobResult)
    assert result.status == "succeeded"
    assert result.bytes_transferred == len(payload)
    assert client.objects[("tgt-bucket", "src/key.bin")] == payload

    # DB: job terminal, target StorageObject row exists
    async with factory() as s:
        job = await s.get(ReplicationJob, job_id)
        assert job.status == "succeeded"
        assert job.completed_at is not None
        assert job.started_at is not None
        assert job.bytes_transferred == len(payload)

        target_obj = (await s.execute(
            select(StorageObject).where(
                StorageObject.storage_id == TGT_STORAGE_ID,
                StorageObject.sha256 == obj.sha256))).scalar_one()
        assert target_obj.size == len(payload)
        assert target_obj.tenant_id == _T_ID


# 2. skipped_existing — target already has same sha

async def test_execute_job_skip_existing(factory):
    payload = b"already replicated content"
    src_obj = await _seed_source_object(factory, payload=payload)
    # Pre-seed the target with the same sha BEFORE the job is claimed
    async with factory() as s:
        s.add(StorageObject(
            tenant_id=_T_ID, storage_id=TGT_STORAGE_ID,
            storage_key="existing/key", sha256=src_obj.sha256,
            size=len(payload), refcount=1))
        await s.commit()

    job_id = await _seed_job(factory, source_object_id=src_obj.id)

    client = StubClient(objects={("src-bucket", "src/key.bin"): payload})
    result = await execute_job(
        factory, job_id=job_id,
        make_client=_make_client_factory(client),
        bandwidth_mbps=0)

    assert result.status == "skipped_existing"
    assert result.bytes_transferred == 0
    # No PutObject ever called
    assert ("tgt-bucket", "src/key.bin") not in client.objects
    assert client._put_attempts == 0
    assert client._get_attempts == 0  # short-circuited before transfer

    async with factory() as s:
        job = await s.get(ReplicationJob, job_id)
        assert job.status == "skipped_existing"
        assert job.completed_at is not None


# 3a. cancelled BEFORE claim — phase 1 short-circuit

async def test_execute_job_cancelled_before_claim(factory):
    """User-initiated cancel arrives before the worker picks the job up.
    Phase-1 claim sees status='cancelled' and short-circuits without
    contacting any backend."""
    payload = b"never transferred"
    obj = await _seed_source_object(factory, payload=payload)
    # Seed the job already cancelled
    async with factory() as s:
        job = ReplicationJob(
            tenant_id=_T_ID, source_object_id=obj.id,
            target_storage_id=TGT_STORAGE_ID, status="cancelled")
        s.add(job)
        await s.commit()
        job_id = job.id

    client = StubClient(objects={("src-bucket", "src/key.bin"): payload})
    result = await execute_job(
        factory, job_id=job_id,
        make_client=_make_client_factory(client),
        bandwidth_mbps=0)

    assert result.status == "cancelled"
    assert client._get_attempts == 0
    assert client._put_attempts == 0


# 3b. cancelled mid-transfer — on_progress detects the flip

async def test_execute_job_cancelled_during_transfer(factory):
    """A cancel flipped the status DURING the byte-copy. The next progress
    tick sees status='cancelled' and the worker aborts before writing the
    target object."""
    # Payload large enough to produce at least 2 chunks so the FIRST
    # on_progress fires, lets us cancel via a side-channel, and the SECOND
    # on_progress observes the flip.
    payload = b"y" * (16 * 1024 * 1024 + 100)
    obj = await _seed_source_object(factory, payload=payload)
    job_id = await _seed_job(factory, source_object_id=obj.id)

    # Patch _commit_progress to flip status on second call
    import dlw.services.replication_worker as worker_mod
    real_commit = worker_mod._commit_progress
    call_count = {"n": 0}

    async def _patched_commit(session_factory, jid, bytes_so_far):
        call_count["n"] += 1
        if call_count["n"] == 2:
            # Flip to cancelled before this commit reads the row
            async with session_factory() as s:
                j = await s.get(ReplicationJob, jid)
                j.status = "cancelled"
                await s.commit()
        return await real_commit(session_factory, jid, bytes_so_far)

    worker_mod._commit_progress = _patched_commit
    try:
        client = StubClient(objects={("src-bucket", "src/key.bin"): payload})
        result = await execute_job(
            factory, job_id=job_id,
            make_client=_make_client_factory(client),
            bandwidth_mbps=0)
    finally:
        worker_mod._commit_progress = real_commit

    assert result.status == "cancelled"
    assert ("tgt-bucket", "src/key.bin") not in client.objects

    async with factory() as s:
        job = await s.get(ReplicationJob, job_id)
        assert job.status == "cancelled"


# 4. sha mismatch → NEVER retried, immediately failed

async def test_execute_job_sha_mismatch_no_retry(factory):
    payload = b"the source bytes"
    # Lie about the sha — seed a different sha than the bytes
    obj = await _seed_source_object(
        factory, payload=payload, sha256="0" * 64)  # wrong sha
    job_id = await _seed_job(factory, source_object_id=obj.id)

    client = StubClient(objects={("src-bucket", "src/key.bin"): payload})
    result = await execute_job(
        factory, job_id=job_id,
        make_client=_make_client_factory(client),
        bandwidth_mbps=0)

    assert result.status == "failed"
    assert "sha256 mismatch" in result.error_message
    assert client._get_attempts == 1  # exactly one — no retry
    assert client._put_attempts == 0

    async with factory() as s:
        job = await s.get(ReplicationJob, job_id)
        assert job.status == "failed"
        assert "sha256 mismatch" in job.error_message


# 5. transient GetObject error → retry → succeed

async def test_execute_job_transient_error_then_success(factory, monkeypatch):
    """First GetObject raises; retry succeeds. Exponential backoff is real
    in production but we monkeypatch asyncio.sleep to 0 for test speed."""
    _orig_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *a, **kw: _orig_sleep(0))
    payload = b"resilient payload"
    obj = await _seed_source_object(factory, payload=payload)
    job_id = await _seed_job(factory, source_object_id=obj.id)

    client = StubClient(
        objects={("src-bucket", "src/key.bin"): payload},
        fail_get_n_times=1)

    result = await execute_job(
        factory, job_id=job_id,
        make_client=_make_client_factory(client),
        bandwidth_mbps=0)

    assert result.status == "succeeded"
    assert result.retries == 1
    assert client._get_attempts == 2  # 1 fail + 1 success

    async with factory() as s:
        job = await s.get(ReplicationJob, job_id)
        assert job.status == "succeeded"
        assert job.retry_count == 1


# 6. transient error × 3 → failed

async def test_execute_job_retries_exhausted(factory, monkeypatch):
    _orig_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *a, **kw: _orig_sleep(0))
    payload = b"unreachable"
    obj = await _seed_source_object(factory, payload=payload)
    job_id = await _seed_job(factory, source_object_id=obj.id)

    client = StubClient(
        objects={("src-bucket", "src/key.bin"): payload},
        fail_get_n_times=99)  # always fail

    result = await execute_job(
        factory, job_id=job_id,
        make_client=_make_client_factory(client),
        bandwidth_mbps=0)

    assert result.status == "failed"
    assert client._get_attempts == 3

    async with factory() as s:
        job = await s.get(ReplicationJob, job_id)
        assert job.status == "failed"
        assert job.retry_count == 3
        assert job.error_message is not None


# 7. Bandwidth throttling sleep math

def test_bandwidth_sleep_math():
    # 8 MB chunk at 100 MB/s → 0.08s
    assert abs(_bandwidth_sleep_seconds(8_000_000, 100.0) - 0.08) < 0.001
    # 0 MB/s == disabled → no sleep
    assert _bandwidth_sleep_seconds(8_000_000, 0.0) == 0.0
    # Negative interpreted as disabled
    assert _bandwidth_sleep_seconds(8_000_000, -1.0) == 0.0


async def test_execute_job_bandwidth_throttle_actually_sleeps(factory):
    """Tight bound: a 10MB payload at 50MB/s should take ~0.2s (chunked
    in 8MB + 2MB). We just check it's slower than unthrottled."""
    payload = b"x" * (10 * 1024 * 1024)  # 10 MB
    obj = await _seed_source_object(factory, payload=payload)
    job_id = await _seed_job(factory, source_object_id=obj.id)

    client = StubClient(objects={("src-bucket", "src/key.bin"): payload})
    t0 = time.monotonic()
    result = await execute_job(
        factory, job_id=job_id,
        make_client=_make_client_factory(client),
        bandwidth_mbps=50.0)
    elapsed = time.monotonic() - t0

    assert result.status == "succeeded"
    # 10 MB at 50 MB/s is ~0.2s. Allow lots of slack — we just want non-zero.
    assert elapsed >= 0.15, f"expected ≥0.15s throttle, got {elapsed:.3f}s"


# 9. worker_loop integration: pick + execute

async def test_worker_loop_picks_and_executes(factory):
    payload = b"loop-driven payload"
    obj = await _seed_source_object(factory, payload=payload)
    job_id = await _seed_job(factory, source_object_id=obj.id)

    client = StubClient(objects={("src-bucket", "src/key.bin"): payload})

    # Run worker_loop briefly with short poll, then cancel it
    task = asyncio.create_task(worker_loop(
        factory, poll_interval_seconds=0.05,
        bandwidth_mbps=0,
        make_client=_make_client_factory(client)))
    try:
        # Poll DB until terminal — at most 5s
        for _ in range(100):
            await asyncio.sleep(0.1)
            async with factory() as s:
                j = await s.get(ReplicationJob, job_id)
                if j.status in ("succeeded", "failed", "cancelled",
                                 "skipped_existing"):
                    break
    finally:
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=1)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    async with factory() as s:
        j = await s.get(ReplicationJob, job_id)
        assert j.status == "succeeded", j.error_message


# 10. claim_one_pending picks pending only, leaves running alone

async def test_claim_one_pending_skips_non_pending(factory):
    """Active partial-unique constraint forbids two live jobs on the same
    (source,target) so we seed 3 DIFFERENT source objects, each with a
    job in a different status. Only the pending one should be claimable."""
    import hashlib
    payloads = [b"a-payload", b"b-payload", b"c-payload"]
    async with factory() as s:
        objs = []
        for i, p in enumerate(payloads):
            o = StorageObject(
                tenant_id=_T_ID, storage_id=SRC_STORAGE_ID,
                storage_key=f"src/k{i}", sha256=hashlib.sha256(p).hexdigest(),
                size=len(p), refcount=1)
            s.add(o)
            objs.append(o)
        await s.flush()
        # job A — pending (the only claimable one)
        ja = ReplicationJob(
            tenant_id=_T_ID, source_object_id=objs[0].id,
            target_storage_id=TGT_STORAGE_ID, status="pending")
        # job B — running (must NOT be picked)
        jb = ReplicationJob(
            tenant_id=_T_ID, source_object_id=objs[1].id,
            target_storage_id=TGT_STORAGE_ID, status="running")
        # job C — succeeded (terminal; ignored)
        jc = ReplicationJob(
            tenant_id=_T_ID, source_object_id=objs[2].id,
            target_storage_id=TGT_STORAGE_ID, status="succeeded")
        s.add_all([ja, jb, jc])
        await s.commit()
        pending_id = ja.id

    async with factory() as s:
        picked = await claim_one_pending(s)
        await s.commit()
    assert picked == pending_id


async def test_claim_one_pending_returns_none_when_empty(factory):
    async with factory() as s:
        picked = await claim_one_pending(s)
        await s.commit()
    assert picked is None


# 11. Prometheus metrics are bumped on terminal states

async def test_metrics_succeeded_increments_counters(factory):
    payload = b"metric-test-payload" * 50
    obj = await _seed_source_object(factory, payload=payload)
    job_id = await _seed_job(factory, source_object_id=obj.id)

    client = StubClient(objects={("src-bucket", "src/key.bin"): payload})
    await execute_job(
        factory, job_id=job_id,
        make_client=_make_client_factory(client),
        bandwidth_mbps=0)

    jobs_counter = REPLICATION_JOBS_TOTAL.labels(
        tenant_id=str(_T_ID), status="succeeded")
    assert jobs_counter._value.get() == 1.0  # type: ignore[attr-defined]

    bytes_counter = REPLICATION_BYTES_TOTAL.labels(
        tenant_id=str(_T_ID), target_storage_id=str(TGT_STORAGE_ID),
        status="succeeded")
    assert bytes_counter._value.get() == float(len(payload))  # type: ignore[attr-defined]


async def test_metrics_skipped_existing_increments(factory):
    payload = b"skip-metric"
    src_obj = await _seed_source_object(factory, payload=payload)
    async with factory() as s:
        s.add(StorageObject(
            tenant_id=_T_ID, storage_id=TGT_STORAGE_ID,
            storage_key="dup/key", sha256=src_obj.sha256,
            size=len(payload), refcount=1))
        await s.commit()
    job_id = await _seed_job(factory, source_object_id=src_obj.id)

    client = StubClient(objects={("src-bucket", "src/key.bin"): payload})
    await execute_job(
        factory, job_id=job_id,
        make_client=_make_client_factory(client),
        bandwidth_mbps=0)

    jobs_counter = REPLICATION_JOBS_TOTAL.labels(
        tenant_id=str(_T_ID), status="skipped_existing")
    assert jobs_counter._value.get() == 1.0  # type: ignore[attr-defined]
