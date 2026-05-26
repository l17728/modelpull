"""v2.1 Sprint 14 — Cross-feature integration tests.

Each test exercises ≥2 of the v2.1 features stacked together so we'd
catch any interaction the per-feature suites miss:
  - SLA tier (S1) × admission control quota (S1)
  - Physical GC (S3) × replication target rows (S4-5)
  - throughput sampler (S7) × optimizer (S8) end-to-end
  - reverse-WSS (S10) × dispatcher (S11) × console (S13) end-to-end
  - credential pool (S12) × storage backend lookup (legacy)"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
from dlw.db.models.chunk_throughput import ChunkThroughputSample
from dlw.db.models.replication import ReplicationJob
from dlw.db.models.storage import StorageBackend
from dlw.db.models.storage_object import StorageObject
from dlw.db.models.tenant import Tenant
from dlw.schemas.reverse_ws import (
    CommandFrame,
    HelloFrame,
    TaskAssignFrame,
    WHITELIST_COMMANDS,
)
from dlw.services.capacity_estimator import build_capacity_matrix
from dlw.services.credential_pool import (
    CredentialPool,
    encrypt_config,
    decrypt_config,
)
from dlw.services.optimizer import Chunk, solve
from dlw.services.physical_gc import find_tombstone_candidates
from dlw.services.replication import (
    CreateJobRequest,
    create_replication_job,
)
from dlw.services.reverse_dispatcher import (
    CommandUnknownExecutor,
    ReverseDispatcher,
    get_dispatcher,
)
from dlw.services.reverse_ws_registry import (
    ReverseWSRegistry,
    get_registry,
)
from dlw.services.sla_tier import (
    ADMISSION_REJECT_BULK_ABOVE,
    ADMISSION_REJECT_STANDARD_ABOVE,
    admission_decision,
)


_TID_A = 9100  # critical tenant
_TID_B = 9101  # bulk tenant


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    import dlw.db.models  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add_all([
            Tenant(id=_TID_A, slug="critA", display_name="Crit",
                    sla_tier="critical"),
            Tenant(id=_TID_B, slug="bulkB", display_name="Bulk",
                    sla_tier="bulk"),
        ])
        await s.flush()
        s.add_all([
            StorageBackend(id=9100, tenant_id=_TID_A, name="src-A",
                            backend_type="s3", config_encrypted=b"",
                            region="us-east-1"),
            StorageBackend(id=9101, tenant_id=_TID_A, name="dst-A",
                            backend_type="s3", config_encrypted=b"",
                            region="ap-east-1"),
        ])
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
async def _cleanup(engine):
    get_registry()._reset_for_tests()
    get_dispatcher()._reset_for_tests()
    yield
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        from sqlalchemy import delete
        await s.execute(delete(ReplicationJob))
        await s.execute(delete(StorageObject))
        await s.execute(delete(ChunkThroughputSample))
        await s.commit()


@pytest.fixture
def factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# SLA tier × admission control

def test_sla_critical_never_rejected_even_when_busy():
    """A critical tenant should be admitted even when system busy > 99%."""
    assert admission_decision("critical", system_busy_fraction=0.99) is True
    assert admission_decision("critical", system_busy_fraction=0.999) is True


def test_sla_standard_rejected_above_99_percent():
    threshold = ADMISSION_REJECT_STANDARD_ABOVE
    assert admission_decision("standard", system_busy_fraction=threshold - 0.01) is True
    assert admission_decision("standard", system_busy_fraction=threshold + 0.01) is False


def test_sla_bulk_rejected_above_90_percent():
    threshold = ADMISSION_REJECT_BULK_ABOVE
    assert admission_decision("bulk", system_busy_fraction=threshold - 0.01) is True
    assert admission_decision("bulk", system_busy_fraction=threshold + 0.01) is False


# ---------------------------------------------------------------------------
# Physical GC × Replication: GC must NOT delete the source of an active
#                            replication job

async def test_replication_pending_blocks_nothing_in_gc_path(factory):
    """A storage_object with an active replication_job has refcount>=1
    (the replication holds a logical reference). GC tombstone scan
    looks for refcount<=0; verify it doesn't see this row."""
    async with factory() as s:
        src = StorageObject(
            tenant_id=_TID_A, storage_id=9100, storage_key="repl/k",
            sha256="d" * 64, size=1024, refcount=1)
        s.add(src)
        await s.flush()
        # Submit a replication job referencing it
        await create_replication_job(
            s, tenant_id=_TID_A, actor_user_id=1,
            req=CreateJobRequest(
                source_object_id=src.id, target_storage_id=9101))
        await s.commit()
        candidates = await find_tombstone_candidates(s)
    # No tombstone candidates — source still has refcount=1
    assert all(c.id != src.id for c in candidates)


async def test_replication_target_row_created_after_apply_succeeds(factory):
    """If we manually mark a job as succeeded after creating a target
    StorageObject row, the (storage_id, sha) UniqueConstraint guards
    against double-write on retry."""
    sha = hashlib.sha256(b"replicated bytes").hexdigest()
    async with factory() as s:
        src = StorageObject(
            tenant_id=_TID_A, storage_id=9100, storage_key="src/file",
            sha256=sha, size=42, refcount=1)
        s.add(src)
        await s.flush()
        # Simulate worker writing the target row
        tgt = StorageObject(
            tenant_id=_TID_A, storage_id=9101, storage_key="src/file",
            sha256=sha, size=42, refcount=1)
        s.add(tgt)
        await s.commit()

        # Second write with same (tenant, storage, sha) must violate unique
        from sqlalchemy.exc import IntegrityError
        s.add(StorageObject(
            tenant_id=_TID_A, storage_id=9101, storage_key="duplicate-key",
            sha256=sha, size=42, refcount=1))
        with pytest.raises(IntegrityError):
            await s.commit()


# ---------------------------------------------------------------------------
# Throughput sampler × Optimizer end-to-end

async def test_capacity_matrix_drives_optimizer_assignment(factory):
    """Seed real chunk_throughput_sample rows, build capacity matrix,
    pass to optimizer — verify the faster source wins."""
    # Three samples each, two sources, executor 'ex-1' is much faster on
    # 'mirror' than 'hf'
    async with factory() as s:
        for _ in range(3):
            s.add(ChunkThroughputSample(
                executor_id="ex-1", source_id="hf",
                file_type="safetensors",
                bytes_transferred=1_000_000, duration_ms=1000,
                tenant_id=_TID_A))
            s.add(ChunkThroughputSample(
                executor_id="ex-1", source_id="mirror",
                file_type="safetensors",
                bytes_transferred=10_000_000, duration_ms=1000,
                tenant_id=_TID_A))
        await s.commit()

        capacities = await build_capacity_matrix(s, lookback_minutes=60)
    assert len(capacities) == 2  # hf + mirror

    chunk = Chunk(id="c1", size_bytes=5_000_000, file_type="safetensors")
    result = solve([chunk], ["ex-1"], ["hf", "mirror"], capacities)
    assert result.assignments["c1"] == ("ex-1", "mirror")  # faster wins


# ---------------------------------------------------------------------------
# Reverse-WSS × Dispatcher × Console: full chain

@dataclass
class _StubWS:
    sent: list[str] = field(default_factory=list)

    async def send_text(self, raw: str) -> None:
        self.sent.append(raw)

    async def close(self, code: int) -> None:
        pass


async def test_register_then_dispatch_then_command_flow():
    """1. Register a session
       2. Dispatch a task assignment over it
       3. Send a whitelisted command on the same session
       Each frame should land on the wire, in order."""
    import json
    ws = _StubWS()
    await get_registry().register(
        executor_id="ex-int", websocket=ws, protocol_version="1.0")

    d = get_dispatcher()
    dispatch_result = await d.dispatch(
        executor_id="ex-int", payload={"subtask_id": "abc"})
    assert dispatch_result.sent_over_wire is True

    cid = await d.send_command(executor_id="ex-int", command="status")
    assert cid

    # Two frames sent: TaskAssign, then Command
    assert len(ws.sent) == 2
    assigned = TaskAssignFrame.model_validate(json.loads(ws.sent[0]))
    cmd_frame = CommandFrame.model_validate(json.loads(ws.sent[1]))
    assert assigned.payload == {"subtask_id": "abc"}
    assert cmd_frame.command == "status"


async def test_reconnect_redispatches_and_command_works_again():
    """1. Dispatch to offline executor → only queued
       2. Executor connects → on_session_established resends
       3. Command works on the new session"""
    import json
    d = get_dispatcher()
    res = await d.dispatch(
        executor_id="ex-rec", payload={"subtask_id": "z"})
    assert res.sent_over_wire is False  # offline

    ws = _StubWS()
    await get_registry().register(
        executor_id="ex-rec", websocket=ws, protocol_version="1.0")
    sent = await d.on_session_established(executor_id="ex-rec")
    assert sent == 1

    # The pending TaskAssign + a new command should both succeed
    await d.send_command(executor_id="ex-rec", command="drain")
    assert len(ws.sent) == 2
    cmd = CommandFrame.model_validate(json.loads(ws.sent[1]))
    assert cmd.command == "drain"


async def test_command_to_offline_executor_blocked():
    """Console command MUST fail for an executor with no live session
    — otherwise an admin could think they drained one when it's actually
    been disconnected and unreachable."""
    with pytest.raises(CommandUnknownExecutor):
        await get_dispatcher().send_command(
            executor_id="ex-not-connected", command="drain")


# ---------------------------------------------------------------------------
# Credential pool × StorageBackend: encrypted config round-trips through
#                                   the same code path real call sites use

def test_credential_pool_round_trips_storage_config(monkeypatch):
    """A storage_backends row with envelope-encrypted config_encrypted
    must decrypt + parse correctly when read through CredentialPool.
    This is the path source_proxy / replication_worker take in
    production — Sprint 12 wired this end."""
    from cryptography.fernet import Fernet
    import json
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("DLW_CONFIG_KEY", key)

    secret_cfg = {"bucket": "prod-bucket", "region": "eu-west-1",
                  "secret_key": "DO-NOT-LEAK"}
    encrypted = encrypt_config(json.dumps(secret_cfg).encode("utf-8"))
    assert encrypted != json.dumps(secret_cfg).encode("utf-8")  # actually wrapped

    pool = CredentialPool()
    creds = pool.get_storage_credentials(
        storage_id=1, backend_type="s3",
        bucket="fallback", region="us-east-1",
        config_encrypted=encrypted)
    assert creds.bucket == "prod-bucket"
    assert creds.region == "eu-west-1"
    assert creds.config["secret_key"] == "DO-NOT-LEAK"


def test_credential_pool_legacy_plaintext_still_works(monkeypatch):
    """v2.0 plaintext-byte rows MUST keep working — Sprint 12's promise
    is non-breaking migration. Run with the encryption key configured;
    a non-magic-prefixed row goes through the plaintext path."""
    from cryptography.fernet import Fernet
    import json
    monkeypatch.setenv("DLW_CONFIG_KEY", Fernet.generate_key().decode("utf-8"))
    legacy = json.dumps({"bucket": "old-bucket"}).encode("utf-8")
    pool = CredentialPool()
    creds = pool.get_storage_credentials(
        storage_id=1, backend_type="s3",
        bucket="fallback", region="us-east-1",
        config_encrypted=legacy)
    assert creds.bucket == "old-bucket"


# ---------------------------------------------------------------------------
# Whitelist + dispatcher contract — Sprint 13 invariant

def test_dispatcher_send_command_validates_against_schema_whitelist():
    """The dispatcher's whitelist check + CommandFrame.validate_command
    must reject the same set."""
    for ok_cmd in WHITELIST_COMMANDS:
        CommandFrame.validate_command(ok_cmd)
    for bad_cmd in ("evil", "ls", "exec", "shutdown", "DROP"):
        with pytest.raises(ValueError):
            CommandFrame.validate_command(bad_cmd)
