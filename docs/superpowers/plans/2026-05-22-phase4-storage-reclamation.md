# Phase 4 — Physical GC / Quota-LRU Storage Reclamation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compute + enforce per-tenant storage quota, track every physical object key (closing the inherit-orphan gap), and reclaim fully-dereferenced physical S3 bytes via a leader-gated GC loop — destructive deletion safe-by-default (operator opt-in).

**Architecture:** Part A (pure-DB) makes `QuotaSnapshot.storage_gb_used` live and enforces `quota_storage_gb`. Part B adds a `storage_physical_keys` ledger (recorded on every subtask success incl. inherit) decoupled from the dedup `storage_objects` row; a new `_physical_gc_loop` reclaims physical keys whose content sha has no surviving dedup row, deleting S3 bytes only when `gc_delete_physical_bytes` is enabled, reusing `recovery.py`'s S3 helpers.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, alembic, asyncpg, boto3; pytest (asyncio_mode=auto), mocked S3.

**Spec:** `docs/superpowers/specs/2026-05-22-phase4-storage-reclamation-design.md` (read fully — esp. §0 safety posture, §1 scope cuts, §5 crash-order).

**Locked constraints (do NOT violate):**
- **Destructive feature, safe-by-default**: `gc_delete_physical_bytes` config defaults **False**; physical deletion only for `backend_type == "s3"`; local-fs skipped (follow-on). Delete physical bytes BEFORE the DB row (crash leaves a re-discoverable ledger row). Audit every physical deletion. All tests use a MOCK S3 client — never a live object-store call.
- Reclamation candidates = `storage_physical_keys` rows whose `(tenant,storage,sha256)` has **no** surviving `storage_objects` row (fully dereferenced) AND past grace. NEVER delete bytes for a sha that still has a live dedup row (refcount invariant).
- Migration: new table only (no ALTER of existing tables) → Python defaults, NO `server_default` on app columns (avoid `compare_server_default` drift, per the SP4a checklist). `down_revision = "b3c4d5e6f7a8"` (current head). Register `StoragePhysicalKey` in `db/models/__init__.py` + `alembic/env.py`. `EXPECTED_TABLES += "storage_physical_keys"`. Dev-DB `alembic upgrade head`. Autogenerate drift gate must be clean.
- No `api/openapi.yaml` change. No literal `null` examples. CI does NOT gate ruff — real gate is `uv run pytest` + `python tools/lint_invariants.py [--strict]`; `ruff check --select I001 --fix` new files only (never broad `--fix`).
- `storage_objects.storage_key` / `storage_id` use no FK (matches the SP3 convention); `StoragePhysicalKey` mirrors that.
- Caller-commits service convention (like `storage_objects.py`).

---

## File Structure

- **Create** `src/dlw/alembic/versions/c4d5e6f7a8b9_p4_storage_physical_keys.py` — migration.
- **Modify** `src/dlw/db/models/storage_object.py` — add `StoragePhysicalKey`.
- **Modify** `src/dlw/db/models/__init__.py` + `src/dlw/alembic/env.py` — register it.
- **Modify** `src/dlw/services/quota.py` — `storage_gb_used` compute + storage enforcement.
- **Modify** `src/dlw/services/storage_objects.py` — `record_physical_key` + `reclaim_physical_orphans`.
- **Create** `src/dlw/services/storage_client.py` — extracted S3-client/decrypt/delete helpers (shared with recovery.py pattern).
- **Modify** `src/dlw/services/scheduler.py` — record physical key in `complete_subtask`.
- **Modify** `src/dlw/config.py` — `gc_delete_physical_bytes`, `gc_archive_after_days`.
- **Modify** `src/dlw/main.py` — `_physical_gc_loop` + register in `_on_active`/`_on_step_down`.
- **Modify** `tests/db/test_alembic.py` — EXPECTED_TABLES.
- **Create** `tests/services/test_storage_quota.py`, `tests/services/test_physical_keys.py`, `tests/services/test_physical_reclaim.py`.
- **Modify** `docs/operator/` (a storage-reclamation doc).

---

## Milestone M1 — Part A: storage accounting + quota enforcement + the table

### Task 1: `StoragePhysicalKey` model + migration + EXPECTED_TABLES

**Files:**
- Modify: `src/dlw/db/models/storage_object.py`, `src/dlw/db/models/__init__.py`, `src/dlw/alembic/env.py`
- Create: `src/dlw/alembic/versions/c4d5e6f7a8b9_p4_storage_physical_keys.py`
- Modify: `tests/db/test_alembic.py`

- [ ] **Step 1: Add the model.** In `src/dlw/db/models/storage_object.py`, append (reuse the file's existing imports — it already imports `BigInteger, DateTime, Integer, String, UniqueConstraint, func, Mapped, mapped_column, Base`; add any missing):

```python
class StoragePhysicalKey(Base):
    """Phase 4: durable ledger of every physical object key written (download +
    inherit), decoupled from the dedup storage_objects row. Enables reclamation
    of inherit-copied new-revision keys that storage_objects never tracked."""
    __tablename__ = "storage_physical_keys"
    __table_args__ = (UniqueConstraint("tenant_id", "storage_id", "storage_key"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tenants.id"), nullable=False)
    storage_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
```
(Add `ForeignKey` to the import line if absent. `server_default=func.now()` on `created_at` matches the sibling `StorageObject.created_at` — that's an existing-pattern server_default for a timestamp, NOT the drift-prone app-value case.)

- [ ] **Step 2: Register.** In `src/dlw/db/models/__init__.py` add `StoragePhysicalKey` to the `storage_object` import + `__all__`. In `src/dlw/alembic/env.py` add it to the explicit import list.

- [ ] **Step 3: EXPECTED_TABLES (failing test first).** In `tests/db/test_alembic.py`, add `"storage_physical_keys",` to `EXPECTED_TABLES` (alphabetical — after `"storage_objects",`).

- [ ] **Step 4: Verify it FAILS:** `cd "D:/download_weights" && uv run pytest tests/db/test_alembic.py::test_upgrade_head_creates_all_tables -v` → FAIL (table missing).

- [ ] **Step 5: Write the migration.** Create `src/dlw/alembic/versions/c4d5e6f7a8b9_p4_storage_physical_keys.py`:

```python
"""p4 storage physical keys

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-05-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: str | None = "b3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "storage_physical_keys",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.BigInteger(),
                  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("storage_id", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(1024), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "storage_id", "storage_key",
                            name="uq_phys_key_tenant_storage_key"),
    )
    op.create_index("idx_phys_key_gc", "storage_physical_keys",
                    ["tenant_id", "storage_id", "sha256"])


def downgrade() -> None:
    op.drop_index("idx_phys_key_gc", table_name="storage_physical_keys")
    op.drop_table("storage_physical_keys")
```

- [ ] **Step 6: Apply + verify tests PASS:**
  `cd "D:/download_weights" && uv run alembic -c alembic.ini upgrade head` → ok.
  `cd "D:/download_weights" && uv run pytest tests/db/test_alembic.py -v` → PASS (upgrade + downgrade→base→re-upgrade clean).

- [ ] **Step 7: BLOCKING drift gate.** `cd "D:/download_weights" && uv run alembic -c alembic.ini revision --autogenerate -m _drift_check`, read the generated `upgrade()`; it MUST be empty w.r.t. `storage_physical_keys` (no `create_table`/`alter`). Delete the drift-check file (do not commit). If drift appears, fix model/migration to agree, re-run until clean.

- [ ] **Step 8: Commit.**
```bash
cd "D:/download_weights" && git add src/dlw/db/models/storage_object.py src/dlw/db/models/__init__.py src/dlw/alembic/env.py src/dlw/alembic/versions/c4d5e6f7a8b9_p4_storage_physical_keys.py tests/db/test_alembic.py && git commit -m "feat(phase4): storage_physical_keys table + model"
```

### Task 2: storage_gb_used compute + quota enforcement

**Files:**
- Modify: `src/dlw/services/quota.py`
- Test: `tests/services/test_storage_quota.py`

- [ ] **Step 1: Write the failing tests.** Create `tests/services/test_storage_quota.py` (mirror the DB-fixture style of `tests/services/test_storage_objects.py` — read it for the `engine`/bootstrap/`session` pattern):

```python
"""Phase 4 Part A: storage usage accounting + quota enforcement."""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
from dlw.db.models.storage_object import StorageObject
from dlw.services.quota import (QuotaExceeded, aggregate_snapshots,
                                check_quota_for_new_task)


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.tenant import Tenant
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        # tenant 1: small quota; tenant 2: large
        s.add(Tenant(id=1, slug="t1", display_name="T1", quota_storage_gb=1))
        s.add(Tenant(id=2, slug="t2", display_name="T2", quota_storage_gb=1000))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def session(engine):
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        yield s


async def test_aggregate_computes_storage_gb_used(session):
    # tenant 1: 2 GiB of objects → storage_gb_used == 2
    gib = 1024 ** 3
    session.add_all([
        StorageObject(tenant_id=1, storage_id=1, storage_key="k1",
                      sha256="a" * 64, size=gib, refcount=1),
        StorageObject(tenant_id=1, storage_id=1, storage_key="k2",
                      sha256="b" * 64, size=gib, refcount=1)])
    await session.commit()
    await aggregate_snapshots(session)
    await session.commit()
    from dlw.db.models.usage import QuotaSnapshot
    snap = await session.get(QuotaSnapshot, 1)
    assert snap.storage_gb_used == 2


async def test_quota_blocks_at_or_over_storage(session):
    # tenant 1 quota_storage_gb=1, used=2 (from prior test) → blocked
    with pytest.raises(QuotaExceeded) as ei:
        await check_quota_for_new_task(session, 1)
    assert ei.value.metric == "storage"


async def test_quota_passes_under_storage(session):
    # tenant 2 large quota, no objects → passes (no raise)
    await aggregate_snapshots(session)
    await session.commit()
    await check_quota_for_new_task(session, 2)   # must not raise
```

(NOTE: `check_quota_for_new_task` also checks bytes_month/concurrent — tenant rows here have those quotas = 0/default; ensure the storage check doesn't get masked. With `quota_bytes_month=0` the bytes check is skipped (`if tenant.quota_bytes_month and ...`), and concurrent count is 0, so the storage raise is what fires. If `quota_concurrent` default 10 interferes, it won't — 0 live tasks.)

- [ ] **Step 2: Verify FAIL:** `cd "D:/download_weights" && uv run pytest tests/services/test_storage_quota.py -v` → `storage_gb_used` is 0 / no storage raise.

- [ ] **Step 3: Implement.** In `src/dlw/services/quota.py`:
  - Add import: `from dlw.db.models.storage_object import StorageObject`.
  - In `aggregate_snapshots`, inside the per-tenant loop, after computing `concurrent`, add the storage sum and set it on the snapshot:
    ```python
        storage_bytes = await session.scalar(
            select(func.coalesce(func.sum(StorageObject.size), 0)).where(
                StorageObject.tenant_id == tid)) or 0
        ...
        snap.storage_gb_used = int(storage_bytes) // (1024 ** 3)
    ```
    (Place `snap.storage_gb_used = ...` alongside the other `snap.* = ...` assignments. Integer floor division — a tenant under 1 GiB shows 0; matches "GiB used" semantics. Use floor, not ceil: enforcement is `>=`, and ceil would block a tenant at 0.4 GiB with a 1 GiB quota prematurely once they hit 1.0; floor is the conservative/standard reading.)
  - In `check_quota_for_new_task`, after the concurrent check, add:
    ```python
        if tenant.quota_storage_gb and snap.storage_gb_used >= tenant.quota_storage_gb:
            raise QuotaExceeded("storage")
    ```
    (Uses the already-loaded+locked `snap`. Guard on `tenant.quota_storage_gb` truthiness like the bytes check, so a 0/unset quota means unlimited.)

- [ ] **Step 4: Verify PASS:** `cd "D:/download_weights" && uv run pytest tests/services/test_storage_quota.py -v` → all pass.

- [ ] **Step 5: Commit.**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/services/quota.py tests/services/test_storage_quota.py
git add src/dlw/services/quota.py tests/services/test_storage_quota.py && git commit -m "feat(phase4): compute storage_gb_used + enforce quota_storage_gb"
```

### Task 3: M1 backend gate

- [ ] **Step 1:** `cd "D:/download_weights" && uv run pytest -q` → all pass. (Watch for any existing quota test asserting the old non-storage behavior — fix only if a real regression.)
- [ ] **Step 2:** `cd "D:/download_weights" && python tools/lint_invariants.py --strict` → OK.
- [ ] **Step 3:** `cd "D:/download_weights" && uv run alembic -c alembic.ini current` → `c4d5e6f7a8b9 (head)`. No commit.

---

## Milestone M2 — Part B: physical-key ledger + reclamation

### Task 4: `record_physical_key` + wire into `complete_subtask`

**Files:**
- Modify: `src/dlw/services/storage_objects.py`, `src/dlw/services/scheduler.py`
- Test: `tests/services/test_physical_keys.py`

- [ ] **Step 1: VERIFY the inherit dst-key assumption (BLOCKER check).** Read `src/dlw/executor/runner.py` inherit-completion + how the executor reports `s3_key` for a successful inherit, and trace into `complete_subtask` (`scheduler.py:197` sets `sub.s3_key = s3_key`). Confirm that on a successful inherit subtask, `sub.s3_key` is the NEW-revision destination key (`compose_key(assignment)`), not the source key. Run a quick trace; if `sub.s3_key` is NOT the dst key on inherit success, STOP and report — the orphan-close depends on it (fallback: derive the key from `compose_key`-equivalent at completion, or read the executor report field that holds the written key). Document what you found.

- [ ] **Step 2: Write the failing tests.** Create `tests/services/test_physical_keys.py`:

```python
"""Phase 4: physical-key ledger records every written key (download + inherit)."""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
from dlw.db.models.storage_object import StoragePhysicalKey
from dlw.services.storage_objects import record_physical_key


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.tenant import Tenant
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=1, slug="t1", display_name="T1"))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def session(engine):
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        yield s


async def test_record_is_idempotent(session):
    for _ in range(2):
        await record_physical_key(session, tenant_id=1, storage_id=1,
                                  sha256="a" * 64, storage_key="repo/rev1/f",
                                  size=10)
        await session.commit()
    rows = (await session.execute(select(StoragePhysicalKey).where(
        StoragePhysicalKey.storage_key == "repo/rev1/f"))).scalars().all()
    assert len(rows) == 1


async def test_two_revisions_same_sha_two_keys(session):
    # the orphan case: same content sha, inherit copied to a 2nd key
    await record_physical_key(session, tenant_id=1, storage_id=1,
                              sha256="c" * 64, storage_key="repo/rev1/g", size=5)
    await record_physical_key(session, tenant_id=1, storage_id=1,
                              sha256="c" * 64, storage_key="repo/rev2/g", size=5)
    await session.commit()
    rows = (await session.execute(select(StoragePhysicalKey).where(
        StoragePhysicalKey.sha256 == "c" * 64))).scalars().all()
    assert {r.storage_key for r in rows} == {"repo/rev1/g", "repo/rev2/g"}
```

- [ ] **Step 3: Verify FAIL** (`record_physical_key` missing).

- [ ] **Step 4: Implement `record_physical_key`** in `src/dlw/services/storage_objects.py`:
```python
async def record_physical_key(
    session: AsyncSession, *, tenant_id: int, storage_id: int,
    sha256: str, storage_key: str, size: int,
) -> None:
    """Phase 4: durable ledger of a physical object key. Idempotent."""
    await session.execute(pg_insert(StoragePhysicalKey).values(
        tenant_id=tenant_id, storage_id=storage_id, sha256=sha256,
        storage_key=storage_key, size=size).on_conflict_do_nothing(
            index_elements=["tenant_id", "storage_id", "storage_key"]))
```
(Add `StoragePhysicalKey` to the model import at the top of the file.)

- [ ] **Step 5: Wire into `complete_subtask`** (`src/dlw/services/scheduler.py`). In the success block (the `if final_status == "succeeded" and sub.s3_key and sub.actual_sha256:` at ~line 216), record the physical key for BOTH download and inherit successes — add right after the `record_object(...)` call (still inside that `if`):
```python
        await record_physical_key(
            session, tenant_id=sub.tenant_id, storage_id=parent.storage_id,
            sha256=sub.actual_sha256, storage_key=sub.s3_key,
            size=sub.bytes_downloaded or 0)
```
(Import `record_physical_key` alongside the existing `record_object` import. This runs for inherit successes too — `record_object` no-ops the refcount for inherit, but `record_physical_key` still records the new-rev key. NOTE: an inherit success may have `sub.bytes_downloaded == 0` (server-side copy, no bytes transferred) — size 0 in the ledger is acceptable; the byte accounting comes from `storage_objects.size`. If the inherit dst size is needed, derive from the matching `storage_objects.size`; size-0 is fine for reclamation which only needs the key.)

- [ ] **Step 6: Verify PASS** + run `tests/services/test_physical_keys.py` and the existing scheduler/record tests (`tests/services/test_record_object_on_complete.py`) to confirm no regression in `complete_subtask`:
  `cd "D:/download_weights" && uv run pytest tests/services/test_physical_keys.py tests/services/test_record_object_on_complete.py -v` → all pass.

- [ ] **Step 7: Commit.**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/services/storage_objects.py src/dlw/services/scheduler.py tests/services/test_physical_keys.py
git add src/dlw/services/storage_objects.py src/dlw/services/scheduler.py tests/services/test_physical_keys.py && git commit -m "feat(phase4): record physical keys on subtask completion (closes inherit orphan)"
```

### Task 5: storage-client helper + `reclaim_physical_orphans`

**Files:**
- Create: `src/dlw/services/storage_client.py`
- Modify: `src/dlw/services/storage_objects.py`
- Test: `tests/services/test_physical_reclaim.py`

- [ ] **Step 1: Extract the storage-client helper.** Create `src/dlw/services/storage_client.py` with controller-side S3 helpers (mirror `recovery.py::_make_s3_client` / `_delete_object_silently` / the config-decode in `_load_storage_config`). Keep `recovery.py` working — this is a NEW module it could later import, but do NOT refactor recovery.py in this task (avoid churn):
```python
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
```

- [ ] **Step 2: Write the failing reclamation tests.** Create `tests/services/test_physical_reclaim.py`. Use a fake client + a `make_client` callable + a recording `audit`:

```python
"""Phase 4: physical reclamation of fully-dereferenced keys (mock S3)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
from dlw.db.models.storage_object import StorageObject, StoragePhysicalKey
from dlw.services.storage_objects import reclaim_physical_orphans


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Tenant
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=1, slug="t1", display_name="T1"))
        await s.flush()
        s.add(StorageBackend(id=1, tenant_id=1, name="bkt", backend_type="s3",
                             config_encrypted=b"{}"))
        s.add(StorageBackend(id=2, tenant_id=1, name="loc", backend_type="local",
                             config_encrypted=b"{}"))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def session(engine):
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        yield s


class _FakeS3:
    def __init__(self):
        self.deleted = []
    def delete_object(self, Bucket, Key):  # noqa: N803
        self.deleted.append((Bucket, Key))


def _old(dt_days=2):
    return datetime.now(UTC) - timedelta(days=dt_days)


async def test_dereferenced_s3_keys_deleted_when_enabled(session):
    fake = _FakeS3()
    # sha 'a' fully dereferenced (no storage_objects row) → reclaim
    session.add(StoragePhysicalKey(tenant_id=1, storage_id=1, sha256="a"*64,
                                   storage_key="repo/rev1/f", size=10,
                                   created_at=_old()))
    # sha 'b' still live (has a storage_objects row) → must NOT reclaim
    session.add(StorageObject(tenant_id=1, storage_id=1, storage_key="repo/rev1/g",
                              sha256="b"*64, size=10, refcount=1))
    session.add(StoragePhysicalKey(tenant_id=1, storage_id=1, sha256="b"*64,
                                   storage_key="repo/rev1/g", size=10,
                                   created_at=_old()))
    await session.commit()
    audited = []
    res = await reclaim_physical_orphans(
        session, grace_seconds=3600, delete_enabled=True,
        make_client=lambda sid: (fake, "bkt", "s3"),
        audit=lambda **k: audited.append(k))
    await session.commit()
    assert ("bkt", "repo/rev1/f") in fake.deleted
    assert all(k != "repo/rev1/g" for _, k in fake.deleted)   # live sha untouched
    remaining = (await session.execute(select(StoragePhysicalKey.storage_key))).scalars().all()
    assert "repo/rev1/f" not in remaining and "repo/rev1/g" in remaining
    assert res["deleted"] == 1 and audited


async def test_disabled_is_dry_run(session):
    fake = _FakeS3()
    session.add(StoragePhysicalKey(tenant_id=1, storage_id=1, sha256="d"*64,
                                   storage_key="repo/rev1/h", size=10,
                                   created_at=_old()))
    await session.commit()
    res = await reclaim_physical_orphans(
        session, grace_seconds=3600, delete_enabled=False,
        make_client=lambda sid: (fake, "bkt", "s3"),
        audit=lambda **k: None)
    await session.commit()
    assert fake.deleted == []
    assert res["candidates"] >= 1 and res["deleted"] == 0
    # row preserved for a future enable
    rows = (await session.execute(select(StoragePhysicalKey.storage_key)
            .where(StoragePhysicalKey.storage_key == "repo/rev1/h"))).scalars().all()
    assert rows == ["repo/rev1/h"]


async def test_non_s3_backend_skipped(session):
    fake = _FakeS3()
    session.add(StoragePhysicalKey(tenant_id=1, storage_id=2, sha256="e"*64,
                                   storage_key="loc/rev1/i", size=10,
                                   created_at=_old()))
    await session.commit()
    res = await reclaim_physical_orphans(
        session, grace_seconds=3600, delete_enabled=True,
        make_client=lambda sid: (fake, "loc", "local"),  # backend_type=local
        audit=lambda **k: None)
    await session.commit()
    assert fake.deleted == []   # local skipped
    rows = (await session.execute(select(StoragePhysicalKey.storage_key)
            .where(StoragePhysicalKey.storage_key == "loc/rev1/i"))).scalars().all()
    assert rows == ["loc/rev1/i"]   # not deleted


async def test_grace_respected(session):
    fake = _FakeS3()
    session.add(StoragePhysicalKey(tenant_id=1, storage_id=1, sha256="f"*64,
                                   storage_key="repo/rev1/fresh", size=10,
                                   created_at=datetime.now(UTC)))  # fresh
    await session.commit()
    res = await reclaim_physical_orphans(
        session, grace_seconds=3600, delete_enabled=True,
        make_client=lambda sid: (fake, "bkt", "s3"),
        audit=lambda **k: None)
    assert all(k != "repo/rev1/fresh" for _, k in fake.deleted)
```

- [ ] **Step 3: Verify FAIL** (`reclaim_physical_orphans` missing).

- [ ] **Step 4: Implement `reclaim_physical_orphans`** in `src/dlw/services/storage_objects.py`. Signature uses an injected `make_client(storage_id) -> (client, bucket, backend_type)` and `audit(**kwargs)` so tests stay network-free:
```python
from sqlalchemy import exists  # add to imports

async def reclaim_physical_orphans(
    session: AsyncSession, *, grace_seconds: int, delete_enabled: bool,
    make_client, audit,
) -> dict:
    """Reclaim physical keys whose content sha has NO surviving storage_objects
    row (fully dereferenced) and are past grace. S3 backends only; bytes deleted
    only when delete_enabled. Deletes bytes BEFORE the ledger row (crash-safe).
    `make_client(storage_id) -> (client, bucket, backend_type)`. Caller commits."""
    from datetime import timedelta
    cutoff = datetime.now(UTC) - timedelta(seconds=grace_seconds)
    live = (exists().where(
        StorageObject.tenant_id == StoragePhysicalKey.tenant_id,
        StorageObject.storage_id == StoragePhysicalKey.storage_id,
        StorageObject.sha256 == StoragePhysicalKey.sha256))
    rows = (await session.execute(
        select(StoragePhysicalKey)
        .where(StoragePhysicalKey.created_at < cutoff, ~live)
        .order_by(StoragePhysicalKey.created_at)
        .with_for_update(skip_locked=True))).scalars().all()
    deleted = 0
    clients: dict[int, tuple] = {}
    for row in rows:
        if not delete_enabled:
            continue
        if row.storage_id not in clients:
            clients[row.storage_id] = make_client(row.storage_id)
        client, bucket, backend_type = clients[row.storage_id]
        if backend_type != "s3":
            continue   # local-fs: executor-side reclaim is a follow-on
        from dlw.services.storage_client import delete_object_silently
        ok = await delete_object_silently(client, bucket, row.storage_key)
        if not ok:
            continue
        audit(action="storage.gc.physical", tenant_id=row.tenant_id,
              storage_key=row.storage_key, size=row.size)
        await session.delete(row)   # row removed AFTER bytes deleted
        deleted += 1
    return {"candidates": len(rows), "deleted": deleted}
```
(`StoragePhysicalKey`, `StorageObject` already imported in this module.)

- [ ] **Step 5: Verify PASS:** `cd "D:/download_weights" && uv run pytest tests/services/test_physical_reclaim.py -v` → all 4 pass. Fix the impl (not tests) if needed.

- [ ] **Step 6: Commit.**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/services/storage_client.py src/dlw/services/storage_objects.py tests/services/test_physical_reclaim.py
git add src/dlw/services/storage_client.py src/dlw/services/storage_objects.py tests/services/test_physical_reclaim.py && git commit -m "feat(phase4): reclaim_physical_orphans + storage_client helper (S3, gated)"
```

### Task 6: config + `_physical_gc_loop` wiring

**Files:**
- Modify: `src/dlw/config.py`, `src/dlw/main.py`

- [ ] **Step 1: Config.** In `src/dlw/config.py`, after the existing `gc_*` fields, add:
```python
    gc_delete_physical_bytes: bool = Field(default=False)
    gc_archive_after_days: int = Field(default=90, ge=0)
```

- [ ] **Step 2: Wire the loop** in `src/dlw/main.py`. Mirror `_gc_loop` exactly. Add a `physical_gc_task_holder = {"t": None}` near the other holders, and define inside `lifespan`:
```python
    async def _physical_gc_loop() -> None:
        from dlw.db.models.storage import StorageBackend
        from dlw.services.audit import write_audit
        from dlw.services.storage_client import (make_s3_client,
                                                 storage_config_from_backend)
        from dlw.services.storage_objects import reclaim_physical_orphans
        while True:
            try:
                await asyncio.sleep(_gs().gc_interval_seconds)
                grace = max(_gs().gc_grace_seconds,
                            _gs().gc_archive_after_days * 86400)
                async with factory() as session:
                    audited: list[dict] = []

                    def _audit(**kw):
                        audited.append(kw)

                    def _make_client(storage_id: int):
                        # sync within the loop body is fine; backends are few
                        import asyncio as _a
                        backend = _a.get_event_loop().run_until_complete  # NO
                        ...
                    res = await reclaim_physical_orphans(
                        session, grace_seconds=grace,
                        delete_enabled=_gs().gc_delete_physical_bytes,
                        make_client=_make_client, audit=_audit)
                    for a in audited:
                        await write_audit(
                            session, action=a["action"],
                            resource_type="storage_physical_keys",
                            resource_id=a["storage_key"], outcome="success",
                            tenant_id=a["tenant_id"], actor_user_id=None,
                            payload={"size": a["size"]})
                    await session.commit()
                    if res["deleted"]:
                        logger.info("physical gc reclaimed %d objects",
                                    res["deleted"])
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("physical gc tick failed; retrying")
```
PROBLEM: `make_client` must load the `StorageBackend` (async DB read) but `reclaim_physical_orphans` calls it synchronously inside the row loop. RESOLUTION: pre-load the backends BEFORE calling reclaim. Change the loop to first fetch all referenced backends into a dict, then pass a pure-sync `make_client` closure over that dict:
```python
                async with factory() as session:
                    # pre-load backends for storage_ids present in the ledger
                    from sqlalchemy import select as _select
                    bks = (await session.execute(_select(StorageBackend))).scalars().all()
                    bmap = {b.id: b for b in bks}
                    cache: dict[int, tuple] = {}

                    def _make_client(sid: int):
                        if sid not in cache:
                            b = bmap.get(sid)
                            if b is None:
                                cache[sid] = (None, "", "missing")
                            else:
                                cfg = storage_config_from_backend(b)
                                cache[sid] = (make_s3_client(cfg) if b.backend_type == "s3"
                                              else None, cfg.bucket, b.backend_type)
                        return cache[sid]
                    audited = []
                    res = await reclaim_physical_orphans(
                        session, grace_seconds=grace,
                        delete_enabled=_gs().gc_delete_physical_bytes,
                        make_client=_make_client,
                        audit=lambda **kw: audited.append(kw))
                    for a in audited:
                        await write_audit(session, action=a["action"],
                            resource_type="storage_physical_keys",
                            resource_id=a["storage_key"], outcome="success",
                            tenant_id=a["tenant_id"], actor_user_id=None,
                            payload={"size": a["size"]})
                    await session.commit()
```
(Use this second, correct version. `reclaim_physical_orphans` treats `backend_type` not in `("s3",)` as skip, so a `"missing"`/`"local"` backend is safely skipped.)

- [ ] **Step 3: Register in `_on_active` / `_on_step_down`.** In `_on_active`, add `physical_gc_task_holder["t"] = asyncio.create_task(_physical_gc_loop())` (next to `gc_task_holder`). In `_on_step_down`, cancel it with the same boilerplate as the other holders (gather/timeout).

- [ ] **Step 4: Smoke import + a focused lifespan test.** Create/extend `tests/test_phase4_lifespan.py` mirroring `tests/test_sp3_lifespan.py` — assert the app builds and (if that test introspects task holders) the physical-gc holder is wired. At minimum: `cd "D:/download_weights" && uv run python -c "import dlw.main; print('ok')"` → ok. If `test_sp3_lifespan.py` has a pattern for asserting a loop is registered on active, replicate it for `_physical_gc_loop`.

- [ ] **Step 5: Commit.**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/config.py src/dlw/main.py
git add src/dlw/config.py src/dlw/main.py tests/test_phase4_lifespan.py && git commit -m "feat(phase4): _physical_gc_loop leader-gated reclamation (default-off)"
```

### Task 7: M2 full backend gate

- [ ] **Step 1:** `cd "D:/download_weights" && uv run pytest -q` → all pass. Fix regressions.
- [ ] **Step 2:** `cd "D:/download_weights" && python tools/lint_invariants.py --strict` → OK.
- [ ] **Step 3:** No commit.

---

## Milestone M3 — Docs

### Task 8: Operator docs

**Files:**
- Create/modify a doc under `docs/operator/` (check for an existing storage/ops doc; if `docs/operator/` has a natural home like a storage or GC section, append there; else create `docs/operator/storage-reclamation.md`).

- [ ] **Step 1: Write the operator doc** (prose; if it's a `docs/v2.0/`-globbed file beware markdownlint, but `docs/operator/**` is NOT markdown-linted per prior SP notes — still write clean prose). Cover: storage quota is now computed (`storage_gb_used` from the dedup ledger, GiB floor) + enforced (new tasks blocked at/over `quota_storage_gb`); the `storage_physical_keys` ledger records every physical key incl. inherit copies (closing the orphan gap); physical reclamation deletes bytes only for refcount=0 (fully dereferenced) content past grace; it is **off by default** (`DLW_GC_DELETE_PHYSICAL_BYTES=true` to enable) and **S3-only** (local-fs reclamation is an executor-side follow-on); the time window is `max(gc_grace_seconds, gc_archive_after_days)`; deletion is audited (`storage.gc.physical`) and ordered bytes-before-row for crash-safety.

- [ ] **Step 2: Commit.**
```bash
cd "D:/download_weights" && git add docs/operator/ && git commit -m "docs(phase4): operator notes for storage reclamation"
```

---

## Self-Review

**1. Spec coverage:** §1 Part A → Tasks 1-2 ✓; §2 data model → Task 1 ✓; §3 record physical key → Task 4 ✓; §4 accounting → Task 2 ✓; §5 reclamation → Tasks 5-6 ✓; §6 tests → Tasks 2,4,5,6 ✓; §7 milestones → M1/M2/M3 ✓.

**2. Placeholder scan:** Task 6 Step 2 deliberately shows a WRONG first draft then the corrected `_make_client` (pre-load backends) — the implementer uses the SECOND version; this is pedagogical, not a placeholder. Task 4 Step 1 is a real BLOCKER-verification step (inherit `sub.s3_key`), not missing logic. Local-fs deferral + dry-run default are documented design cuts.

**3. Type consistency:** `record_physical_key(session, *, tenant_id, storage_id, sha256, storage_key, size)`; `reclaim_physical_orphans(session, *, grace_seconds, delete_enabled, make_client, audit) -> {"candidates","deleted"}`; `make_client(storage_id) -> (client, bucket, backend_type)`; `audit(action=, tenant_id=, storage_key=, size=)`; `StoragePhysicalKey` columns — consistent across tasks/tests.

**Open risks for reviewers:** (a) does `sub.s3_key` hold the inherit DST key on success (Task 4 Step 1 BLOCKER check)? (b) `reclaim_physical_orphans`'s `~exists()` correlated subquery correctness + `with_for_update(skip_locked=True)` interaction with the join; (c) the `_make_client` pre-load-backends closure in `main.py` (the first draft is intentionally wrong — confirm the corrected version is used); (d) is GiB **floor** vs **ceil** for `storage_gb_used` the right call for the enforcement semantics; (e) physical-delete-before-row ordering — is best-effort-per-row + caller-commit actually crash-safe (a crash after S3 delete but before commit re-runs and re-deletes an absent key = safe no-op — confirm).
