# Phase 3 SP3 — Incremental Download + Global Dedup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A model-version upgrade (or any repeat download) reuses identical content (proven by HF sha256) via a cheap executor S3 `copy_object`/`os.link` instead of re-downloading; identical content is stored once per tenant+backend (refcounted) and reclaimed when unreferenced.

**Architecture:** A scheduling-phase `diff_and_dedup` stage (runs in SP2's `run_scheduling_tick` *before* `plan_task_sources`): a file whose HF `expected_sha256` already has a `storage_objects` row for `(tenant_id,storage_id,sha256)` becomes an `inherit` subtask (ref+refcount now, no download); others stay `pending` → SP2's LPT/source planner. The executor materializes `inherit` subtasks by S3 server-side copy / hardlink (it holds STS creds — INVARIANT 3). `complete_subtask` upserts `storage_objects` (`ON CONFLICT refcount+1`, idempotent if a ref already exists). `DELETE /api/v1/tasks/{id}` (terminal only) derefs; a leader-gated GC loop deletes refcount=0 rows past a grace.

**Tech Stack:** SQLAlchemy 2 async + asyncpg + alembic, `boto3` `copy_object` + stdlib `os.link` (no new deps), SP1 `Principal`/`require_perm`/`tenant_filtered`, SP1/SP2 leader-gated-loop pattern, SP2 `run_scheduling_tick` seam.

**Spec:** `docs/superpowers/specs/2026-05-19-phase-3-sp3-incremental-download-design.md`. **Branch:** `feat/phase-3-sp3-incremental-download` (off `main` @ `454ac41`, spec `23a85f1`).

**Conventions (verified against the codebase — follow exactly):**
- Bash tool errors on `cd <args>` (Windows); working dir already `D:\download_weights`. Use `uv run pytest ...` / `uv run alembic ...`.
- ORM models register in `src/dlw/db/models/__init__.py` (imports + sorted `__all__`). **Never** add to `src/dlw/db/base.py` (circular import).
- **Every DB test fixture does `drop_all → create_all` at setup AND `drop_all` at teardown** (SP2 session-DB cross-module collision lesson). Function-scoped unless safe. `import dlw.db.models  # noqa: F401` before any `create_all`. A fixture that seeds fixed-PK rows MUST teardown-drop.
- New test dirs need empty `__init__.py` (none new here — all under existing `tests/{db,services,api,e2e,executor}/`).
- API tests: `tests.conftest.make_app_with_state(ephemeral_ca, enrollment_token="e")`; auth via `tests.conftest.principal_headers(secret="unit-secret", role="tenant_admin")` + autouse fixture `monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET","unit-secret")` + `get_settings.cache_clear()`. mTLS-bypass API tests also `monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY","1")`.
- Service layer does NOT commit; caller commits.
- Real CI gates (no ruff/mypy/code-vs-yaml CI): `pytest` (`uv sync --all-groups`, uv 0.11.9); `invariant_lint` = `uv run python -m pytest tools/test_lint_invariants.py` + `python tools/lint_invariants.py` + `python tools/lint_no_direct_status_write.py`; `openapi` = `spectral lint api/openapi.yaml --fail-severity=error` + `swagger-cli validate api/openapi.yaml`; `yamllint` scans `deploy/ api/`. No new deps → `uv.lock` unchanged.
- `tools/lint_invariants.py` AST-scans `src/dlw/api/tasks.py`, `services/task_service.py`, `services/scheduler.py` for subtask/task status literals. **Task 1 adds `"inherit"` to `VALID_SUBTASK_STATUS`** so the `claim_one_subtask` `.in_(("pending","inherit"))` change (scheduler.py) and any scanned-file use are safe. `diff_and_dedup` (incremental.py) and the GC (storage_objects.py) are NOT scanned.
- Run only each task's named tests; the suite goes red between tasks until M3/M4 wiring lands (expected) — controller runs milestone E2E.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/dlw/config.py` (modify) | `gc_interval_seconds`, `gc_grace_seconds` |
| `tools/lint_invariants.py` (modify) | add `"inherit"` to `VALID_SUBTASK_STATUS` |
| `src/dlw/db/models/storage_object.py` (create) | `StorageObject`, `SubtaskObjectRef` |
| `src/dlw/db/models/__init__.py` (modify) | register the 2 new models |
| `src/dlw/db/models/task.py` (modify) | `FileSubTask.inherit_from_key` |
| `src/dlw/alembic/versions/<rev>_p3sp3_incremental.py` (create) | column + 2 tables |
| `src/dlw/schemas/task.py` (modify) | `TaskCreate.upgrade_from_revision` |
| `src/dlw/schemas/subtask.py` (modify) | `SubTaskRead.inherit_from_key` |
| `src/dlw/services/task_service.py` (modify) | thread `upgrade_from_revision` onto the task |
| `src/dlw/services/storage_objects.py` (create) | `record_object`/`record_ref_only`/`deref_subtask`/`gc_orphans` |
| `src/dlw/services/incremental.py` (create) | `diff_and_dedup` |
| `src/dlw/services/source_scheduler.py` (modify) | call `diff_and_dedup` before `plan_task_sources` |
| `src/dlw/services/scheduler.py` (modify) | `record_object` on success; claim includes `inherit` |
| `src/dlw/executor/inherit.py` (create) | `materialize_inherit` (S3 copy / os.link) |
| `src/dlw/executor/runner.py` (modify) | dispatch inherit subtasks |
| `src/dlw/api/tasks.py` (modify) | `DELETE /api/v1/tasks/{id}` |
| `src/dlw/main.py` (modify) | leader-gated `_gc_loop` |
| `api/openapi.yaml` (modify) | DELETE op + upgrade_from_revision + inherit |
| `docs/operator/incremental-download.md` (create) | operator guide |

---

# Milestone M1 — Schema + Models + Config + Lint

### Task 1: Config + `"inherit"` status + `upgrade_from_revision`/`inherit_from_key` schema fields

**Files:** Modify `src/dlw/config.py`, `tools/lint_invariants.py`, `src/dlw/schemas/task.py`, `src/dlw/schemas/subtask.py`; Test (extend) `tests/test_config.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_config.py`:
```python
def test_sp3_gc_settings_defaults():
    from dlw.config import get_settings
    get_settings.cache_clear()
    s = get_settings()
    assert s.gc_interval_seconds == 60.0
    assert s.gc_grace_seconds == 3600
    get_settings.cache_clear()


def test_sp3_inherit_is_valid_subtask_status():
    from tools.lint_invariants import VALID_SUBTASK_STATUS
    assert "inherit" in VALID_SUBTASK_STATUS


def test_sp3_taskcreate_upgrade_from_revision_optional():
    from dlw.schemas.task import TaskCreate
    t = TaskCreate(repo_id="o/r", revision="a" * 40, storage_id=1)
    assert t.upgrade_from_revision is None
    t2 = TaskCreate(repo_id="o/r", revision="a" * 40, storage_id=1,
                    upgrade_from_revision="b" * 40)
    assert t2.upgrade_from_revision == "b" * 40


def test_sp3_storageconfig_backend_type_default():
    from dlw.schemas.storage import StorageConfig
    c = StorageConfig(bucket="b")
    assert c.backend_type == "s3" and c.base_path is None
    c2 = StorageConfig(bucket="b", backend_type="local", base_path="/srv")
    assert c2.backend_type == "local" and c2.base_path == "/srv"


@pytest.mark.slow
async def test_sp3_create_task_threads_upgrade_from_revision(db_session):
    """create_task must persist upgrade_from_revision (banner 7e). The
    autouse _patch_hf_global conftest fixture makes list_repo_tree return
    2 files so create_task succeeds without an explicit HF mock."""
    from dlw.db.base import Base
    from dlw.schemas.task import TaskCreate
    from dlw.services.task_service import create_task
    async with db_session.bind.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    from dlw.db.models.tenant import Project, Tenant, User
    from dlw.db.models.storage import StorageBackend
    db_session.add(Tenant(id=1, slug="t", display_name="T"))
    await db_session.flush()
    db_session.add_all([
        Project(id=1, tenant_id=1, name="d"),
        User(id=1, tenant_id=1, oidc_subject="u", email="e",
             role="tenant_operator"),
        StorageBackend(id=1, tenant_id=1, name="s", backend_type="s3",
                       config_encrypted=b"")])
    await db_session.flush()
    task = await create_task(
        db_session,
        TaskCreate(repo_id="o/r", revision="n" * 40, storage_id=1,
                   upgrade_from_revision="o" * 40),
        owner_user_id=1, tenant_id=1, project_id=1,
        hf_endpoint="https://hf", hf_token=None)
    assert task.upgrade_from_revision == "o" * 40
```
(`pytest` is already imported at the top of `tests/test_config.py`; if not, add `import pytest`. `db_session` is the shared per-test session fixture from `tests/conftest.py`; if its bind isn't directly `.begin()`-able, use the `engine` fixture pattern instead — `async with engine.begin()...` + an `async_sessionmaker` — matching the SP3 DB-fixture convention.)

- [ ] **Step 2: Run** `uv run pytest "tests/test_config.py::test_sp3_gc_settings_defaults" "tests/test_config.py::test_sp3_inherit_is_valid_subtask_status" "tests/test_config.py::test_sp3_taskcreate_upgrade_from_revision_optional" "tests/test_config.py::test_sp3_storageconfig_backend_type_default" "tests/test_config.py::test_sp3_create_task_threads_upgrade_from_revision" -v` → FAIL.

- [ ] **Step 3: Implement**
  - `src/dlw/config.py`: after the Phase 3 SP2 block (last SP2 field `degradation_trigger_threshold`), add:
```python
    # Phase 3 SP3 — incremental / dedup GC
    gc_interval_seconds: float = Field(default=60.0, ge=5.0, le=3600.0)
    gc_grace_seconds: int = Field(default=3600, ge=0)
```
  - `tools/lint_invariants.py`: change the `VALID_SUBTASK_STATUS` set to include `"inherit"`:
```python
VALID_SUBTASK_STATUS = {
    "pending", "assigned", "succeeded", "failed", "cancelled",
    "paused_disk_full", "paused_external",          # W2b2 NEW
    "inherit",                                      # P3-SP3 NEW
}
```
  - `src/dlw/schemas/task.py` `TaskCreate`: add after `trust_non_hf_sha256`:
```python
    upgrade_from_revision: str | None = Field(default=None, max_length=64)
```
  - `src/dlw/schemas/subtask.py` `SubTaskRead`: add after `s3_key`:
```python
    inherit_from_key: str | None = Field(default=None, max_length=1024)
```
  - `src/dlw/schemas/storage.py` `StorageConfig`: add (banner 7b — the real class has only bucket/region/endpoint_url/key_prefix; add a backend discriminator + optional local path so the executor inherit branch can pick S3-copy vs hardlink):
```python
    backend_type: str = Field(default="s3", max_length=32)
    base_path: str | None = Field(default=None, max_length=1024)
```
  - `src/dlw/services/task_service.py` `create_task`: in the `DownloadTask(...)` constructor (currently `status="pending"` is the last kwarg), add `upgrade_from_revision=body.upgrade_from_revision,` (banner 7e — the column already exists on the model; it is currently never set).

- [ ] **Step 4: Run** `uv run pytest tests/test_config.py -v` (all pass, incl. the 2 new) + `python tools/lint_invariants.py` (exit 0) + `uv run python -m pytest tools/test_lint_invariants.py -q` (pass).

- [ ] **Step 5: Commit**
```bash
git add src/dlw/config.py tools/lint_invariants.py src/dlw/schemas/task.py src/dlw/schemas/subtask.py src/dlw/schemas/storage.py src/dlw/services/task_service.py tests/test_config.py
git commit -m "feat(sp3): GC config + inherit status + upgrade_from_revision wire + StorageConfig.backend_type"
```

---

### Task 2: Models + migration

**Files:** Create `src/dlw/db/models/storage_object.py`, migration; Modify `src/dlw/db/models/__init__.py`, `src/dlw/db/models/task.py`; Test `tests/db/test_p3sp3_migration.py`

- [ ] **Step 1: Write the failing test** — `tests/db/test_p3sp3_migration.py`:
```python
"""SP3 migration: storage_objects + subtask_object_refs + inherit_from_key."""
from __future__ import annotations

import pytest
from sqlalchemy import text

import dlw.db.models  # noqa: F401

pytestmark = pytest.mark.slow


async def test_tables_and_column(engine):
    from dlw.db.base import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        names = {r[0] for r in await conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public'"))}
        assert {"storage_objects", "subtask_object_refs"} <= names
        scols = {r[0] for r in await conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='file_subtasks'"))}
        assert "inherit_from_key" in scols
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def test_unique_tenant_storage_sha(engine):
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from dlw.db.base import Base
    from dlw.db.models.storage_object import StorageObject
    from dlw.db.models.tenant import Tenant
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=1, slug="t", display_name="T"))
        await s.commit()
        s.add(StorageObject(tenant_id=1, storage_id=1, storage_key="k1",
                            sha256="a" * 64, size=10))
        await s.commit()
        s.add(StorageObject(tenant_id=1, storage_id=1, storage_key="k2",
                            sha256="a" * 64, size=10))
        with pytest.raises(IntegrityError):
            await s.commit()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
```

- [ ] **Step 2: Run** `uv run pytest tests/db/test_p3sp3_migration.py -v` → FAIL.

- [ ] **Step 3: Implement** — `src/dlw/db/models/storage_object.py`:
```python
"""Global-dedup storage objects (Phase 3 SP3; doc 06 §3.1, INVARIANT 14)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from dlw.db.base import Base


class StorageObject(Base):
    __tablename__ = "storage_objects"
    __table_args__ = (
        UniqueConstraint("tenant_id", "storage_id", "sha256"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tenants.id"), nullable=False)
    storage_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    refcount: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_referenced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)


class SubtaskObjectRef(Base):
    __tablename__ = "subtask_object_refs"

    subtask_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("file_subtasks.id", ondelete="CASCADE"),
        primary_key=True)
    object_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("storage_objects.id"), primary_key=True)
```
In `src/dlw/db/models/__init__.py` add `from dlw.db.models.storage_object import StorageObject, SubtaskObjectRef` and add `"StorageObject", "SubtaskObjectRef"` to `__all__` (keep sorted). In `src/dlw/db/models/task.py` `FileSubTask`, add (use the `String` import already present):
```python
    inherit_from_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
```
Generate the migration: `uv run alembic revision -m "p3sp3 incremental"`. Set `down_revision = "bb1dd2c45a12"`; top imports `import sqlalchemy as sa`, `from alembic import op`, `from sqlalchemy.dialects import postgresql`. Body:
```python
def upgrade() -> None:
    op.add_column("file_subtasks", sa.Column(
        "inherit_from_key", sa.String(1024), nullable=True))
    op.create_table(
        "storage_objects",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(),
                  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("storage_id", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(1024), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("refcount", sa.Integer(), nullable=False,
                  server_default="1"),
        sa.Column("last_referenced_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "storage_id", "sha256"),
    )
    op.create_index("idx_storage_obj_gc", "storage_objects",
                    ["refcount", "created_at"])
    op.create_table(
        "subtask_object_refs",
        sa.Column("subtask_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("file_subtasks.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("object_id", sa.BigInteger(),
                  sa.ForeignKey("storage_objects.id"), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("subtask_object_refs")
    op.drop_index("idx_storage_obj_gc", "storage_objects")
    op.drop_table("storage_objects")
    op.drop_column("file_subtasks", "inherit_from_key")
```

- [ ] **Step 4: Run** `uv run pytest tests/db/test_p3sp3_migration.py -v` (2 pass); then `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head` (clean — report any pre-existing local-DB env issue, pytest is authoritative).

- [ ] **Step 5: Commit**
```bash
git add src/dlw/db/models/storage_object.py src/dlw/db/models/__init__.py src/dlw/db/models/task.py src/dlw/alembic/versions/ tests/db/test_p3sp3_migration.py
git commit -m "feat(sp3): StorageObject/SubtaskObjectRef models + migration"
```

---

# Milestone M2 — storage_objects service

### Task 3: `record_object` / `record_ref_only` (+ inherit double-count idempotency)

**Files:** Create `src/dlw/services/storage_objects.py`; Test `tests/services/test_storage_objects.py`

- [ ] **Step 1: Write the failing test** — `tests/services/test_storage_objects.py`:
```python
"""storage_objects: upsert/ref/refcount + inherit idempotency (SP3)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import dlw.db.models  # noqa: F401
from dlw.db.base import Base
from dlw.db.models.storage_object import StorageObject, SubtaskObjectRef
from dlw.services.storage_objects import record_object, record_ref_only

pytestmark = pytest.mark.slow


@pytest.fixture
async def seeded(engine):
    from dlw.db.models.task import DownloadTask, FileSubTask
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
        await c.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=1, slug="t", display_name="T"))
        await s.flush()
        s.add_all([Project(id=1, tenant_id=1, name="d"),
                   User(id=1, tenant_id=1, oidc_subject="u", email="e",
                        role="tenant_operator")])
        t = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                         repo_id="o/r", revision="abc", storage_id=1,
                         path_template="t", status="downloading")
        s.add(t)
        await s.flush()
        subs = []
        for i in range(3):
            sub = FileSubTask(task_id=t.id, tenant_id=1, filename=f"f{i}",
                              file_size=10, status="pending")
            s.add(sub)
            await s.flush()
            subs.append(sub.id)
        await s.commit()
    yield f, subs
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)


async def test_record_object_inserts_then_refcounts(seeded):
    f, subs = seeded
    async with f() as s:
        await record_object(s, tenant_id=1, storage_id=1, storage_key="k",
                            sha256="a" * 64, size=10, subtask_id=subs[0])
        await s.commit()
        obj = (await s.execute(select(StorageObject))).scalar_one()
        assert obj.refcount == 1
    async with f() as s:
        await record_object(s, tenant_id=1, storage_id=1, storage_key="k2",
                            sha256="a" * 64, size=10, subtask_id=subs[1])
        await s.commit()
        obj = (await s.execute(select(StorageObject))).scalar_one()
        assert obj.refcount == 2          # same sha row, refcount bumped
        refs = (await s.execute(select(SubtaskObjectRef))).scalars().all()
        assert len(refs) == 2


async def test_record_object_idempotent_when_ref_exists(seeded):
    """Inherit path: record_ref_only already added the ref+refcount; a later
    complete_subtask record_object MUST NOT double-count (banner #1 / §7)."""
    f, subs = seeded
    async with f() as s:
        obj_id = await record_ref_only(s, tenant_id=1, storage_id=1,
                                       storage_key="k", sha256="a" * 64,
                                       size=10, subtask_id=subs[0])
        await s.commit()
        o = await s.get(StorageObject, obj_id)
        assert o.refcount == 1
    async with f() as s:
        await record_object(s, tenant_id=1, storage_id=1, storage_key="k",
                            sha256="a" * 64, size=10, subtask_id=subs[0])
        await s.commit()
        o = (await s.execute(select(StorageObject))).scalar_one()
        assert o.refcount == 1            # NOT 2 — ref already existed
        refs = (await s.execute(select(SubtaskObjectRef))).scalars().all()
        assert len(refs) == 1
```

- [ ] **Step 2: Run** `uv run pytest tests/services/test_storage_objects.py -v` → FAIL.

- [ ] **Step 3: Implement** — `src/dlw/services/storage_objects.py`:
```python
"""Global-dedup storage_objects service (Phase 3 SP3; doc 06 §3.1).
Caller commits (service-layer convention)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.storage_object import StorageObject, SubtaskObjectRef


async def _ref_exists(session: AsyncSession, subtask_id: uuid.UUID) -> bool:
    return (await session.scalar(
        select(SubtaskObjectRef.object_id)
        .where(SubtaskObjectRef.subtask_id == subtask_id).limit(1))
    ) is not None


async def _upsert_object(
    session: AsyncSession, *, tenant_id: int, storage_id: int,
    storage_key: str, sha256: str, size: int, bump: bool,
) -> int:
    """Insert the (tenant,storage,sha) object or, on conflict, optionally
    bump refcount. Returns the object id."""
    stmt = pg_insert(StorageObject).values(
        tenant_id=tenant_id, storage_id=storage_id, storage_key=storage_key,
        sha256=sha256, size=size, refcount=1)
    if bump:
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "storage_id", "sha256"],
            set_={"refcount": StorageObject.refcount + 1,
                  "last_referenced_at": datetime.now(UTC)})
    else:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["tenant_id", "storage_id", "sha256"])
    await session.execute(stmt)
    return await session.scalar(
        select(StorageObject.id).where(
            StorageObject.tenant_id == tenant_id,
            StorageObject.storage_id == storage_id,
            StorageObject.sha256 == sha256))


async def record_ref_only(
    session: AsyncSession, *, tenant_id: int, storage_id: int,
    storage_key: str, sha256: str, size: int, subtask_id: uuid.UUID,
) -> int:
    """diff_and_dedup path: ensure the object exists, bump refcount, add the
    subtask ref. Returns object id."""
    oid = await _upsert_object(
        session, tenant_id=tenant_id, storage_id=storage_id,
        storage_key=storage_key, sha256=sha256, size=size, bump=True)
    await session.execute(pg_insert(SubtaskObjectRef).values(
        subtask_id=subtask_id, object_id=oid).on_conflict_do_nothing())
    return oid


async def record_object(
    session: AsyncSession, *, tenant_id: int, storage_id: int,
    storage_key: str, sha256: str, size: int, subtask_id: uuid.UUID,
) -> None:
    """complete_subtask success path. Idempotent for inherit subtasks: if a
    ref already exists for this subtask (added by record_ref_only in
    diff_and_dedup), do nothing — never double-count (banner #1 / §7)."""
    if await _ref_exists(session, subtask_id):
        return
    oid = await _upsert_object(
        session, tenant_id=tenant_id, storage_id=storage_id,
        storage_key=storage_key, sha256=sha256, size=size, bump=True)
    await session.execute(pg_insert(SubtaskObjectRef).values(
        subtask_id=subtask_id, object_id=oid).on_conflict_do_nothing())


async def deref_subtask(
    session: AsyncSession, subtask_id: uuid.UUID
) -> None:
    """DELETE path: refcount-- for each referenced object, drop the refs."""
    oids = (await session.execute(
        select(SubtaskObjectRef.object_id)
        .where(SubtaskObjectRef.subtask_id == subtask_id))).scalars().all()
    for oid in oids:
        await session.execute(
            update(StorageObject).where(StorageObject.id == oid)
            .values(refcount=StorageObject.refcount - 1))
    await session.execute(
        delete(SubtaskObjectRef)
        .where(SubtaskObjectRef.subtask_id == subtask_id))


async def gc_orphans(
    session: AsyncSession, *, grace_seconds: int
) -> int:
    """Delete storage_objects rows with refcount<=0 older than the grace.
    SKIP LOCKED so it never blocks ref/deref. Returns count. Caller commits."""
    from datetime import timedelta
    cutoff = datetime.now(UTC) - timedelta(seconds=grace_seconds)
    ids = (await session.execute(
        select(StorageObject.id)
        .where(StorageObject.refcount <= 0,
                StorageObject.created_at < cutoff)
        .with_for_update(skip_locked=True))).scalars().all()
    if not ids:
        return 0
    await session.execute(
        delete(StorageObject).where(StorageObject.id.in_(ids)))
    return len(ids)
```

- [ ] **Step 4: Run** `uv run pytest tests/services/test_storage_objects.py -v` → 2 PASS.

- [ ] **Step 5: Commit**
```bash
git add src/dlw/services/storage_objects.py tests/services/test_storage_objects.py
git commit -m "feat(sp3): storage_objects service (upsert/ref/deref/gc + inherit idempotency)"
```

---

### Task 4: `deref_subtask` / `gc_orphans` behavior tests

**Files:** Test (extend) `tests/services/test_storage_objects.py`

- [ ] **Step 1: Write the failing test** — append:
```python
async def test_deref_decrements_and_drops_refs(seeded):
    f, subs = seeded
    async with f() as s:
        await record_object(s, tenant_id=1, storage_id=1, storage_key="k",
                            sha256="a" * 64, size=10, subtask_id=subs[0])
        await record_object(s, tenant_id=1, storage_id=1, storage_key="k",
                            sha256="a" * 64, size=10, subtask_id=subs[1])
        await s.commit()
        from dlw.services.storage_objects import deref_subtask
        await deref_subtask(s, subs[0])
        await s.commit()
        o = (await s.execute(select(StorageObject))).scalar_one()
        assert o.refcount == 1
        refs = (await s.execute(select(SubtaskObjectRef))).scalars().all()
        assert {r.subtask_id for r in refs} == {subs[1]}


async def test_gc_deletes_only_zero_refcount_past_grace(seeded):
    f, subs = seeded
    from datetime import UTC, datetime, timedelta

    from dlw.services.storage_objects import deref_subtask, gc_orphans
    async with f() as s:
        await record_object(s, tenant_id=1, storage_id=1, storage_key="k",
                            sha256="a" * 64, size=10, subtask_id=subs[0])
        await record_object(s, tenant_id=1, storage_id=1, storage_key="k2",
                            sha256="b" * 64, size=10, subtask_id=subs[1])
        await s.commit()
        # zero out the "a" object and backdate it past the grace
        await deref_subtask(s, subs[0])
        await s.execute(update(StorageObject)
                        .where(StorageObject.sha256 == "a" * 64)
                        .values(created_at=datetime.now(UTC)
                                - timedelta(hours=2)))
        await s.commit()
        n = await gc_orphans(s, grace_seconds=3600)
        await s.commit()
        assert n == 1
        rows = (await s.execute(select(StorageObject))).scalars().all()
        assert [r.sha256 for r in rows] == ["b" * 64]   # refcount>0 kept


async def test_gc_respects_grace(seeded):
    f, subs = seeded
    from dlw.services.storage_objects import deref_subtask, gc_orphans
    async with f() as s:
        await record_object(s, tenant_id=1, storage_id=1, storage_key="k",
                            sha256="a" * 64, size=10, subtask_id=subs[0])
        await s.commit()
        await deref_subtask(s, subs[0])      # refcount 0 but just created
        await s.commit()
        assert await gc_orphans(s, grace_seconds=3600) == 0  # within grace
```

- [ ] **Step 2: Run** `uv run pytest tests/services/test_storage_objects.py -v` → the 3 new FAIL only if logic wrong; expected PASS (Task 3 already implemented `deref_subtask`/`gc_orphans`). If any fail, fix `storage_objects.py` (not the test).

- [ ] **Step 3: (no new impl — Task 3 covered it; this task hardens behavior coverage)**

- [ ] **Step 4: Run** `uv run pytest tests/services/test_storage_objects.py -v` → 5 PASS.

- [ ] **Step 5: Commit**
```bash
git add tests/services/test_storage_objects.py
git commit -m "test(sp3): deref decrement + gc zero-refcount/grace coverage"
```

---

# Milestone M3 — diff/dedup + scheduler wiring

### Task 5: `diff_and_dedup`

**Files:** Create `src/dlw/services/incremental.py`; Test `tests/services/test_incremental.py`

- [ ] **Step 1: Write the failing test** — `tests/services/test_incremental.py`:
```python
"""diff_and_dedup: existing object → inherit; else pending (SP3)."""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import dlw.db.models  # noqa: F401
from dlw.db.base import Base
from dlw.db.models.storage_object import StorageObject
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.services.incremental import diff_and_dedup

pytestmark = pytest.mark.slow


@pytest.fixture
async def f(engine):
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
        await c.run_sync(Base.metadata.create_all)
    fac = async_sessionmaker(engine, expire_on_commit=False)
    async with fac() as s:
        s.add(Tenant(id=1, slug="t", display_name="T"))
        await s.flush()
        s.add_all([Project(id=1, tenant_id=1, name="d"),
                   User(id=1, tenant_id=1, oidc_subject="u", email="e",
                        role="tenant_operator")])
        await s.commit()
    yield fac
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)


async def _mk_task(s, **kw):
    t = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                     repo_id="o/r", revision="new", storage_id=1,
                     path_template="t", status="scheduling", **kw)
    s.add(t)
    await s.flush()
    return t


async def test_existing_object_becomes_inherit(f):
    async with f() as s:
        s.add(StorageObject(tenant_id=1, storage_id=1, storage_key="old/key",
                            sha256="a" * 64, size=10))
        t = await _mk_task(s)
        s.add(FileSubTask(task_id=t.id, tenant_id=1, filename="w.bin",
                          file_size=10, expected_sha256="a" * 64,
                          status="pending"))
        await s.commit()
        await diff_and_dedup(s, t)
        await s.commit()
        sub = (await s.execute(select(FileSubTask))).scalar_one()
        assert sub.status == "inherit"
        assert sub.inherit_from_key == "old/key"
        obj = (await s.execute(select(StorageObject))).scalar_one()
        assert obj.refcount == 2


async def test_no_object_stays_pending(f):
    async with f() as s:
        t = await _mk_task(s)
        s.add(FileSubTask(task_id=t.id, tenant_id=1, filename="w.bin",
                          file_size=10, expected_sha256="z" * 64,
                          status="pending"))
        await s.commit()
        await diff_and_dedup(s, t)
        await s.commit()
        sub = (await s.execute(select(FileSubTask))).scalar_one()
        assert sub.status == "pending" and sub.inherit_from_key is None


async def test_null_sha_never_inherited(f):
    async with f() as s:
        s.add(StorageObject(tenant_id=1, storage_id=1, storage_key="k",
                            sha256="a" * 64, size=10))
        t = await _mk_task(s)
        s.add(FileSubTask(task_id=t.id, tenant_id=1, filename="c.json",
                          file_size=4, expected_sha256=None,
                          status="pending"))
        await s.commit()
        await diff_and_dedup(s, t)
        await s.commit()
        sub = (await s.execute(select(FileSubTask))).scalar_one()
        assert sub.status == "pending"


async def test_cross_task_dedup(f):
    """Unified dedup: an object from an unrelated prior task is reused."""
    async with f() as s:
        s.add(StorageObject(tenant_id=1, storage_id=1, storage_key="prior/k",
                            sha256="d" * 64, size=99))
        t = await _mk_task(s, upgrade_from_revision=None)
        s.add(FileSubTask(task_id=t.id, tenant_id=1, filename="m.safetensors",
                          file_size=99, expected_sha256="d" * 64,
                          status="pending"))
        await s.commit()
        await diff_and_dedup(s, t)
        await s.commit()
        sub = (await s.execute(select(FileSubTask))).scalar_one()
        assert sub.status == "inherit" and sub.inherit_from_key == "prior/k"
```

- [ ] **Step 2: Run** `uv run pytest tests/services/test_incremental.py -v` → FAIL.

- [ ] **Step 3: Implement** — `src/dlw/services/incremental.py`:
```python
"""Scheduling-phase incremental diff + global dedup (Phase 3 SP3; doc §2).
Runs before SP2 plan_task_sources. Caller commits."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.storage_object import StorageObject
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.services.storage_objects import record_ref_only


async def diff_and_dedup(session: AsyncSession, task: DownloadTask) -> None:
    """For each still-pending subtask whose HF expected_sha256 already has a
    storage_objects row for (tenant, storage, sha): flip it to `inherit`
    (ref+refcount now, no download). Unifies upgrade_from_revision diff with
    cross-task dedup — completed subtasks of any prior task/revision already
    produced storage_objects, so one lookup covers both."""
    subs = (await session.execute(
        select(FileSubTask).where(
            FileSubTask.task_id == task.id,
            FileSubTask.status == "pending"))).scalars().all()
    for sub in subs:
        if sub.expected_sha256 is None:
            continue
        obj = (await session.execute(
            select(StorageObject).where(
                StorageObject.tenant_id == task.tenant_id,
                StorageObject.storage_id == task.storage_id,
                StorageObject.sha256 == sub.expected_sha256))
        ).scalar_one_or_none()
        if obj is None:
            continue
        sub.status = "inherit"
        sub.inherit_from_key = obj.storage_key
        await record_ref_only(
            session, tenant_id=task.tenant_id, storage_id=task.storage_id,
            storage_key=obj.storage_key, sha256=sub.expected_sha256,
            size=sub.file_size or obj.size, subtask_id=sub.id)
```

- [ ] **Step 4: Run** `uv run pytest tests/services/test_incremental.py -v` → 4 PASS.

- [ ] **Step 5: Commit**
```bash
git add src/dlw/services/incremental.py tests/services/test_incremental.py
git commit -m "feat(sp3): diff_and_dedup (existing-object => inherit, unified dedup)"
```

---

### Task 6: Wire `diff_and_dedup` into the scheduling loop + planner skips non-pending

**Files:** Modify `src/dlw/services/source_scheduler.py`; Test `tests/services/test_source_scheduler.py` (extend)

- [ ] **Step 1: Write the failing test** — append to `tests/services/test_source_scheduler.py` (it already has the `factory` fixture + `_FakeReg`/`_FakeDriver`/`_IdResolver` from SP2):
```python
async def test_run_scheduling_tick_inherits_before_planning(factory):
    """diff_and_dedup runs before plan_task_sources: a subtask whose sha has
    an existing storage_object is `inherit` (not source-planned)."""
    from dlw.config import get_settings
    from dlw.db.models.source import SourceManifest  # noqa: F401 (type ref)
    from dlw.db.models.storage_object import StorageObject
    from dlw.db.models.task import DownloadTask, FileSubTask
    from dlw.services.source_scheduler import run_scheduling_tick
    from sqlalchemy import select
    async with factory() as s:
        s.add(StorageObject(tenant_id=1, storage_id=1, storage_key="old/k",
                            sha256="a" * 64, size=10))
        t = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                         repo_id="o/r", revision="new", storage_id=1,
                         path_template="t", status="pending")
        s.add(t)
        await s.flush()
        s.add(FileSubTask(task_id=t.id, tenant_id=1, filename="w.bin",
                          file_size=10, expected_sha256="a" * 64,
                          status="pending"))
        await s.commit()
        reg = _FakeReg({"huggingface": _FakeDriver("huggingface", [], True)})
        await run_scheduling_tick(s, reg, _IdResolver(), get_settings())
        await s.commit()
        sub = (await s.execute(select(FileSubTask))).scalar_one()
        assert sub.status == "inherit"
        obj = (await s.execute(select(StorageObject))).scalar_one()
        assert obj.refcount == 2
        t2 = (await s.execute(select(DownloadTask))).scalar_one()
        # BLOCKER fix (banner 7a): a fully-inherited task must NOT be paused
        # by plan_task_sources' pinned/no_sha/no_speed gates.
        assert t2.status != "paused_external"
        assert t2.status == "downloading"
```
(The SP2 `factory` fixture already seeds Tenant/Project/User/StorageBackend id=1; `StorageObject.storage_id` is a plain `BigInteger` (no FK) so `storage_id=1` works regardless.)

- [ ] **Step 2: Run** `uv run pytest tests/services/test_source_scheduler.py::test_run_scheduling_tick_inherits_before_planning -v` → FAIL (diff not wired AND the task gets `paused_external` by the gates).

- [ ] **Step 3: Implement** — in `src/dlw/services/source_scheduler.py`:
  (a) `run_scheduling_tick`: immediately after `task.status = "scheduling"` and BEFORE the per-source probe/`plan_task_sources` call, add:
```python
        from dlw.services.incremental import diff_and_dedup
        await diff_and_dedup(session, task)
```
  (b) **`plan_task_sources` — relocate the pending-subs load + empty-guard to the TOP, BEFORE the pinned/no_sha256_authority/no_source_speed gates** (banner 7a — these 3 gates currently run before the subtask query at ~`source_scheduler.py:55-71` and would wrongly `paused_external` a fully-inherited task). At the very start of `plan_task_sources` (after the docstring, before the `_strategy_filter`/manifest/`pinned`/`hf_ok`/`candidates` logic), insert:
```python
    # SP3 (banner 7a): only `pending` subtasks need source planning;
    # `inherit` subs were materialised by diff_and_dedup. A fully-inherited
    # task has zero pending subs — return early so the pinned / no-sha /
    # no-speed pause gates below never fire (the task then flows
    # scheduling -> downloading and its inherit subs complete to succeeded).
    subs = (await session.execute(select(FileSubTask).where(
        FileSubTask.task_id == task.id,
        FileSubTask.status == "pending"))).scalars().all()
    if not subs:
        return
```
  Then DELETE the later, original `subs = (await session.execute(select(FileSubTask).where(FileSubTask.task_id == task.id))).scalars().all()` line (the one feeding `sizes`) so `subs` is the single pending-only list defined at the top; everything downstream (`sizes = {x.filename: ... for x in subs}`, the assign loop) now uses pending-only subs unchanged. (If `subs` is referenced before the original query in current code, just remove the duplicate query and keep the top one — there must be exactly one `subs` binding, the pending-only one at the top.)

- [ ] **Step 3b: Run** `uv run pytest tests/services/test_source_scheduler.py -v` → confirm the new test PASSES (`task.status == "downloading"`, sub `inherit`, refcount 2) AND every existing SP2 `test_source_scheduler` test still passes (the early-return only triggers when there are zero pending subs; SP2 download tasks always have pending subs so their path is unchanged).

- [ ] **Step 4: Run** `uv run pytest tests/services/test_source_scheduler.py -v` → all PASS (the new test + existing SP2 tests still green).

- [ ] **Step 5: Commit**
```bash
git add src/dlw/services/source_scheduler.py tests/services/test_source_scheduler.py
git commit -m "feat(sp3): run diff_and_dedup before plan_task_sources; planner skips inherit"
```

---

### Task 7: `record_object` on subtask success + claim includes `inherit`

**Files:** Modify `src/dlw/services/scheduler.py`; Test `tests/services/test_record_object_on_complete.py`

- [ ] **Step 1: Write the failing test** — `tests/services/test_record_object_on_complete.py`:
```python
"""complete_subtask records a storage_object; inherit doesn't double-count."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import dlw.db.models  # noqa: F401
from dlw.db.base import Base
from dlw.db.models.storage_object import StorageObject
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.services.scheduler import complete_subtask

pytestmark = pytest.mark.slow


@pytest.fixture
async def f(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
        await c.run_sync(Base.metadata.create_all)
    fac = async_sessionmaker(engine, expire_on_commit=False)
    async with fac() as s:
        s.add(Tenant(id=1, slug="t", display_name="T"))
        await s.flush()
        s.add_all([Project(id=1, tenant_id=1, name="d"),
                   User(id=1, tenant_id=1, oidc_subject="u", email="e",
                        role="tenant_operator"),
                   StorageBackend(id=1, tenant_id=1, name="s",
                                  backend_type="s3", config_encrypted=b"")])
        await s.commit()
    yield fac
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)


async def test_download_complete_records_object(f):
    async with f() as s:
        t = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                         repo_id="o/r", revision="abc", storage_id=1,
                         path_template="t", status="downloading")
        s.add(t)
        await s.flush()
        tok = uuid.uuid4()
        sub = FileSubTask(task_id=t.id, tenant_id=1, filename="m",
                          file_size=4, expected_sha256="c" * 64,
                          status="assigned", assignment_token=tok)
        s.add(sub)
        await s.flush()
        sid = sub.id
        await complete_subtask(s, sid, final_status="succeeded",
                               actual_sha256="c" * 64, bytes_downloaded=4,
                               error=None, assignment_token=tok,
                               s3_key="o/r/abc/m")
        await s.commit()
        obj = (await s.execute(select(StorageObject))).scalar_one()
        assert (obj.sha256, obj.storage_key, obj.refcount) == \
               ("c" * 64, "o/r/abc/m", 1)


async def test_inherit_complete_does_not_double_count(f):
    from dlw.services.storage_objects import record_ref_only
    async with f() as s:
        t = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                         repo_id="o/r", revision="new", storage_id=1,
                         path_template="t", status="downloading")
        s.add(t)
        await s.flush()
        tok = uuid.uuid4()
        sub = FileSubTask(task_id=t.id, tenant_id=1, filename="w",
                          file_size=10, expected_sha256="a" * 64,
                          status="inherit", inherit_from_key="old/k",
                          assignment_token=tok)
        s.add(sub)
        await s.flush()
        sid = sub.id
        await record_ref_only(s, tenant_id=1, storage_id=1,
                              storage_key="old/k", sha256="a" * 64,
                              size=10, subtask_id=sid)   # diff_and_dedup did this
        await s.commit()
        await complete_subtask(s, sid, final_status="succeeded",
                               actual_sha256="a" * 64, bytes_downloaded=10,
                               error=None, assignment_token=tok,
                               s3_key="o/r/new/w")
        await s.commit()
        obj = (await s.execute(select(StorageObject))).scalar_one()
        assert obj.refcount == 1          # NOT 2 — ref already existed


async def test_inherit_copy_failure_dereferences_and_repends(f):
    """banner 7f: a failed inherit copy must undo the diff-time refcount++
    and re-queue the file as pending (no permanent refcount leak)."""
    from dlw.db.models.storage_object import SubtaskObjectRef
    from dlw.services.storage_objects import record_ref_only
    async with f() as s:
        t = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                         repo_id="o/r", revision="new", storage_id=1,
                         path_template="t", status="downloading")
        s.add(t)
        await s.flush()
        tok = uuid.uuid4()
        sub = FileSubTask(task_id=t.id, tenant_id=1, filename="w",
                          file_size=10, expected_sha256="a" * 64,
                          status="assigned", inherit_from_key="old/k",
                          assignment_token=tok)
        s.add(sub)
        await s.flush()
        sid = sub.id
        await record_ref_only(s, tenant_id=1, storage_id=1,
                              storage_key="old/k", sha256="a" * 64,
                              size=10, subtask_id=sid)   # diff_and_dedup did this
        await s.commit()
        sub_done, _ = await complete_subtask(
            s, sid, final_status="failed", actual_sha256=None,
            bytes_downloaded=0, error="copy_object failed",
            assignment_token=tok)
        await s.commit()
        assert sub_done.status == "pending"          # re-queued
        assert sub_done.inherit_from_key is None
        obj = (await s.execute(select(StorageObject))).scalar_one()
        assert obj.refcount == 0                     # diff-time bump undone
        refs = (await s.execute(select(SubtaskObjectRef))).scalars().all()
        assert refs == []
```

- [ ] **Step 2: Run** `uv run pytest tests/services/test_record_object_on_complete.py -v` → FAIL.

- [ ] **Step 3: Implement** — in `src/dlw/services/scheduler.py`:
  - Add a module-top import `from dlw.services.storage_objects import deref_subtask, record_object` (no circular import: storage_objects imports only models).
  - In `complete_subtask`, the SP2 region is: `sub.status = final_status` (~line 186) … `parent = await session.get(DownloadTask, sub.task_id, with_for_update=True)` (~line 200-202) then the SP2 `blacklist_file` block (~203-209). Immediately AFTER the blacklist block and BEFORE the `siblings = ...` query (~line 210), add BOTH:
```python
    if (final_status == "succeeded" and sub.s3_key
            and sub.actual_sha256):
        await record_object(
            session, tenant_id=sub.tenant_id, storage_id=parent.storage_id,
            storage_key=sub.s3_key, sha256=sub.actual_sha256,
            size=sub.bytes_downloaded or 0, subtask_id=sub.id)
    elif final_status == "failed" and sub.inherit_from_key:
        # banner 7f: an inherit copy failed. diff_and_dedup already did
        # refcount++ + a SubtaskObjectRef — undo it and re-queue the file as
        # a normal download (next scheduling pass), so refcount can't leak.
        await deref_subtask(session, sub.id)
        sub.status = "pending"
        sub.inherit_from_key = None
        sub.last_error = error
        return sub, parent      # do NOT run sibling/parent terminal logic
```
  (The `return sub, parent` short-circuits the sibling/parent-transition tail so the parent task isn't marked terminal over a subtask we just re-queued. `complete_subtask`'s normal return is `(sub, parent)` — match its return type; verify the exact tuple shape in the function's other returns and mirror it.)
  - Change `claim_one_subtask`'s subtask-status filter (currently `.where(FileSubTask.status == "pending")`, ~line 75-76) to:
```python
        .where(FileSubTask.status.in_(("pending", "inherit")))
```
  (so `inherit` subtasks are claimable for the lightweight copy. `"inherit"` is in `VALID_SUBTASK_STATUS` from Task 1; this is a `.in_` comparison not a `status=` assignment so `tools/lint_invariants.py` doesn't flag it regardless — confirmed by the pre-review.)

- [ ] **Step 4: Run** `uv run pytest tests/services/test_record_object_on_complete.py -v` → 3 PASS. Then `uv run pytest tests/services/test_scheduler.py -v` (no regression — the failed-inherit branch is gated on `sub.inherit_from_key`, which existing scheduler tests never set) and `python tools/lint_invariants.py` (exit 0).

- [ ] **Step 5: Commit**
```bash
git add src/dlw/services/scheduler.py tests/services/test_record_object_on_complete.py
git commit -m "feat(sp3): record_object on success (idempotent) + claim includes inherit"
```

---

# Milestone M4 — executor inherit + DELETE + GC loop

### Task 8: Executor inherit materialization + runner dispatch + poll payload

**Files:** Create `src/dlw/executor/inherit.py`; Modify `src/dlw/executor/runner.py`, `src/dlw/api/executors.py` (poll), `src/dlw/schemas/subtask.py` (already has `inherit_from_key` from Task 1); Test `tests/executor/test_inherit.py`

- [ ] **Step 1: Write the failing test** — `tests/executor/test_inherit.py`:
```python
"""Executor inherit materialization: S3 copy / local hardlink (SP3)."""
from __future__ import annotations

import uuid

import pytest

from dlw.executor.inherit import materialize_inherit
from dlw.executor.types import DownloadResult


class _FakeS3:
    def __init__(self):
        self.calls = []

    def copy_object(self, **kw):
        self.calls.append(kw)
        return {}


async def test_s3_copy_object(monkeypatch):
    fake = _FakeS3()
    import dlw.executor.inherit as inh
    monkeypatch.setattr(inh, "_s3_client", lambda settings, cfg: fake)

    class _Cfg:
        bucket = "b"
        backend_type = "s3"
    r = await materialize_inherit(
        settings=object(), storage_config=_Cfg(),
        src_key="old/k", dst_key="new/k", sha256="a" * 64, size=42)
    assert isinstance(r, DownloadResult)
    assert r.actual_sha256 == "a" * 64 and r.s3_key == "new/k"
    assert fake.calls[0]["CopySource"] == {"Bucket": "b", "Key": "old/k"}
    assert fake.calls[0]["Key"] == "new/k"


async def test_local_hardlink(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"x" * 8)
    dst = tmp_path / "sub" / "dst.bin"

    class _Cfg:
        backend_type = "local"
        base_path = str(tmp_path)
    r = await materialize_inherit(
        settings=object(), storage_config=_Cfg(),
        src_key="src.bin", dst_key="sub/dst.bin",
        sha256="z" * 64, size=8)
    assert dst.read_bytes() == b"x" * 8
    assert r.s3_key == "sub/dst.bin" and r.actual_sha256 == "z" * 64
```

- [ ] **Step 2: Run** `uv run pytest tests/executor/test_inherit.py -v` → FAIL.

- [ ] **Step 3: Implement** — `src/dlw/executor/inherit.py`:
```python
"""Executor-side inherit materialization (Phase 3 SP3; doc §2 step 4).
S3 server-side copy_object (in-region, ≈ free) or local hardlink — no
HF/source bytes. The executor holds the storage creds (INVARIANT 3)."""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

from dlw.executor._io import make_s3_client
from dlw.executor.types import DownloadResult


def _s3_client(settings: Any, cfg: Any) -> Any:
    """Test seam — monkeypatched to inject a fake S3 client."""
    return make_s3_client(settings, cfg)


async def materialize_inherit(
    *, settings: Any, storage_config: Any, src_key: str, dst_key: str,
    sha256: str, size: int,
) -> DownloadResult:
    backend = getattr(storage_config, "backend_type", "s3")
    if backend == "s3":
        s3 = _s3_client(settings, storage_config)
        bucket = storage_config.bucket
        await asyncio.to_thread(
            lambda: s3.copy_object(
                Bucket=bucket,
                CopySource={"Bucket": bucket, "Key": src_key},
                Key=dst_key))
    else:
        base = Path(getattr(storage_config, "base_path", "."))
        src = base / src_key
        dst = base / dst_key
        dst.parent.mkdir(parents=True, exist_ok=True)

        def _link() -> None:
            try:
                os.link(src, dst)
            except OSError:                 # EXDEV / unsupported → copy
                shutil.copy2(src, dst)
        await asyncio.to_thread(_link)
    return DownloadResult(bytes_written=size, actual_sha256=sha256,
                          s3_key=dst_key)
```
In `src/dlw/executor/runner.py` the dispatch method is **`_execute_subtask`** (NOT `_run_one` — pre-review banner 7c; verified at `runner.py:193`). It builds `assignment = Assignment(...)` (`runner.py:203-213`) then `downloader = self._choose_downloader(assignment.file_size)` (`runner.py:214`). Insert the inherit branch BETWEEN those two (after the `Assignment(...)` build, before `_choose_downloader`):
```python
            inherit_key = subtask.get("inherit_from_key")
            if inherit_key:
                from dlw.executor._io import compose_key
                from dlw.executor.inherit import materialize_inherit
                result = await materialize_inherit(
                    settings=self._s,
                    storage_config=assignment.storage_config,
                    src_key=inherit_key,
                    dst_key=compose_key(assignment),
                    sha256=assignment.expected_sha256 or "",
                    size=assignment.file_size or 0)
                await self._client.report(
                    subtask_id=sub_id, status="succeeded",
                    assignment_token=assignment_token,
                    actual_sha256=result.actual_sha256,
                    bytes_downloaded=result.bytes_written,
                    s3_key=result.s3_key)
                return
            downloader = self._choose_downloader(assignment.file_size)
```
(**Verified anchors (banner 7c):** the runner's settings attribute is `self._s` (an `ExecutorSettings`; `runner.py:47`), NOT `self._settings`. `subtask` is the poll-payload dict in scope in `_execute_subtask`; `sub_id`/`assignment_token` are defined above the `Assignment(...)` build. `compose_key(assignment)` is correct (`_io.py:54`). `self._client.report(...)` accepts exactly those kwargs (`executor/client.py:184-194`).)

In `src/dlw/api/executors.py` the poll endpoint builds the assignment's storage-config dict (`cfg_dict`) from the `StorageBackend` (around `executors.py:160-167`, where it does `cfg_dict.setdefault("bucket", ...)` / `cfg_dict.setdefault("region", ...)`). **Add** (banner 7b — `materialize_inherit` needs to know S3 vs local; `StorageBackend.backend_type` exists in the DB but is never threaded):
```python
        cfg_dict.setdefault("backend_type", storage.backend_type)
```
(next to the existing `cfg_dict.setdefault(...)` calls, before `StorageConfig(**cfg_dict)` — so the executor's `assignment.storage_config.backend_type` reflects the real backend; `StorageConfig.backend_type` was added in Task 1 with default `"s3"`). `SubTaskRead` already has `inherit_from_key` (Task 1) so `model_validate(sub)` rides it along automatically — no other poll change.

- [ ] **Step 4: Run** `uv run pytest tests/executor/test_inherit.py tests/executor/test_runner.py -v` → PASS (runner tests still green; the inherit branch only triggers when `inherit_from_key` present).

- [ ] **Step 5: Commit**
```bash
git add src/dlw/executor/inherit.py src/dlw/executor/runner.py src/dlw/api/executors.py tests/executor/test_inherit.py
git commit -m "feat(sp3): executor inherit materialization (S3 copy / hardlink) + runner dispatch + poll backend_type"
```

---

### Task 9: `DELETE /api/v1/tasks/{id}`

**Files:** Modify `src/dlw/api/tasks.py`; Test `tests/api/test_delete_task.py`

- [ ] **Step 1: Write the failing test** — `tests/api/test_delete_task.py`:
```python
"""DELETE /api/v1/tasks/{id} — terminal-only, tenant-scoped, deref (SP3)."""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import dlw.db.models  # noqa: F401
from dlw.config import get_settings
from dlw.db.base import Base
from tests.conftest import make_app_with_state, principal_headers

pytestmark = pytest.mark.slow
SECRET = "unit-secret"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def app_client(ephemeral_ca, engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.storage_object import StorageObject, SubtaskObjectRef
    from dlw.db.models.task import DownloadTask, FileSubTask
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
        await c.run_sync(Base.metadata.create_all)
    fac = async_sessionmaker(engine, expire_on_commit=False)
    async with fac() as s:
        s.add_all([Tenant(id=1, slug="t1", display_name="T1"),
                   Tenant(id=2, slug="t2", display_name="T2")])
        await s.flush()
        s.add_all([Project(id=1, tenant_id=1, name="d"),
                   User(id=1, tenant_id=1, oidc_subject="u", email="e",
                        role="tenant_admin"),
                   StorageBackend(id=1, tenant_id=1, name="s",
                                  backend_type="s3", config_encrypted=b"")])
        obj = StorageObject(tenant_id=1, storage_id=1, storage_key="k",
                            sha256="a" * 64, size=10, refcount=1)
        s.add(obj)
        t = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                         repo_id="o/r", revision="abc", storage_id=1,
                         path_template="t", status="succeeded")
        s.add(t)
        await s.flush()
        sub = FileSubTask(task_id=t.id, tenant_id=1, filename="m",
                          file_size=10, status="succeeded")
        s.add(sub)
        await s.flush()
        s.add(SubtaskObjectRef(subtask_id=sub.id, object_id=obj.id))
        active = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                              repo_id="o/r2", revision="abc", storage_id=1,
                              path_template="t", status="downloading")
        s.add(active)
        await s.flush()
        ids = {"done": t.id, "active": active.id}
        await s.commit()
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c, fac, ids


def _h(tid=1):
    return principal_headers(secret=SECRET, user_id=1, tenant_id=tid,
                             role="tenant_admin")


async def test_delete_terminal_decrements_refcount(app_client):
    c, fac, ids = app_client
    r = await c.delete(f"/api/v1/tasks/{ids['done']}", headers=_h())
    assert r.status_code == 204, r.text
    async with fac() as s:
        from dlw.db.models.storage_object import StorageObject
        obj = (await s.execute(select(StorageObject))).scalar_one()
        assert obj.refcount == 0
        from dlw.db.models.task import DownloadTask
        assert await s.get(DownloadTask, ids["done"]) is None


async def test_delete_non_terminal_409(app_client):
    c, fac, ids = app_client
    r = await c.delete(f"/api/v1/tasks/{ids['active']}", headers=_h())
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "TASK_NOT_TERMINAL"


async def test_delete_cross_tenant_404(app_client):
    c, fac, ids = app_client
    r = await c.delete(f"/api/v1/tasks/{ids['done']}", headers=_h(tid=2))
    assert r.status_code == 404


async def test_delete_unauth_401(app_client):
    c, fac, ids = app_client
    assert (await c.delete(f"/api/v1/tasks/{ids['done']}")).status_code == 401
```

- [ ] **Step 2: Run** `uv run pytest tests/api/test_delete_task.py -v` → FAIL.

- [ ] **Step 3: Implement** — in `src/dlw/api/tasks.py`, add `from dlw.services.storage_objects import deref_subtask` (top imports) and `from sqlalchemy.orm import selectinload` is already imported (used by get_task); add the route after `post_cancel_task`:
```python
@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: uuid.UUID,
    principal: Principal = Depends(require_perm("/api/v1/tasks*", "DELETE")),
    session: AsyncSession = Depends(_session),
) -> None:
    row = (await session.execute(
        tenant_filtered(
            select(DownloadTask).where(DownloadTask.id == task_id),
            DownloadTask, principal)
        .options(selectinload(DownloadTask.subtasks))
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    if row.status not in ("succeeded", "failed", "cancelled"):
        raise HTTPException(
            status_code=409,
            detail={"code": "TASK_NOT_TERMINAL", "status": row.status})
    for sub in row.subtasks:
        await deref_subtask(session, sub.id)
    await session.delete(row)          # FK cascade → subtasks → object refs
    await session.commit()
```
(`selectinload(DownloadTask.subtasks)` + `tenant_filtered` mirror the existing `get_task`. `status` enum/`scheduling`/`inherit` are not introduced here as literals on a `.status =` assignment — the `not in (...)` tuple is a read comparison, not flagged by lint; `"succeeded"/"failed"/"cancelled"` are valid task statuses anyway.)

- [ ] **Step 4: Run** `uv run pytest tests/api/test_delete_task.py -v` → 4 PASS. Then `python tools/lint_invariants.py` (exit 0).

- [ ] **Step 5: Commit**
```bash
git add src/dlw/api/tasks.py tests/api/test_delete_task.py
git commit -m "feat(sp3): DELETE /api/v1/tasks/{id} (terminal-only, tenant-scoped, deref)"
```

---

### Task 10: Leader-gated GC loop

**Files:** Modify `src/dlw/main.py`; Test `tests/test_sp3_lifespan.py`

- [ ] **Step 1: Write the failing test** — `tests/test_sp3_lifespan.py`:
```python
"""Real lifespan starts the leader-gated GC loop (SP3; SP1/SP2 regression-class)."""
from __future__ import annotations

import pytest

import dlw.db.models  # noqa: F401
from dlw.db.base import Base

pytestmark = pytest.mark.slow


async def test_lifespan_gc_loop_present(engine, tmp_path, monkeypatch):
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    monkeypatch.setenv("DLW_AUTH_DEV_MODE", "true")
    monkeypatch.setenv("DLW_CA_DIR", str(tmp_path / "ca"))
    from dlw.config import get_settings
    get_settings.cache_clear()
    from dlw.main import create_app, lifespan
    app = create_app()
    async with lifespan(app):
        # registry/resolver from SP2 still bootstrapped (no regression)
        assert app.state.source_registry is not None
    get_settings.cache_clear()
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
```

- [ ] **Step 2: Run** `uv run pytest tests/test_sp3_lifespan.py -v` → expected PASS already (it asserts SP2 state survives; the GC-loop wiring is verified to not break lifespan). Confirm it PASSES after Step 3 too (regression guard that the new loop doesn't break startup/shutdown).

- [ ] **Step 3: Implement** — in `src/dlw/main.py` lifespan, mirror the SP2 `_rebalance_loop`/`rebalance_task_holder` pattern exactly. After the `rebalance_task_holder` definition add:
```python
    gc_task_holder: dict[str, asyncio.Task | None] = {"t": None}

    async def _gc_loop() -> None:
        from dlw.services.audit import write_audit
        from dlw.services.storage_objects import gc_orphans
        while True:
            try:
                await asyncio.sleep(_gs().gc_interval_seconds)
                async with factory() as session:
                    n = await gc_orphans(
                        session, grace_seconds=_gs().gc_grace_seconds)
                    if n:
                        # banner 7d: GC reclaim is an audited event.
                        await write_audit(
                            session, action="storage.gc",
                            resource_type="storage_objects",
                            resource_id=None, outcome="success",
                            tenant_id=None, actor_user_id=None,
                            payload={"reclaimed": n})
                        logger.info("gc reclaimed %d storage_objects", n)
                    await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("gc tick failed; retrying")
```
(`write_audit(session, *, action, resource_type, resource_id, outcome, tenant_id, actor_user_id, payload=None)` is the SP1 helper in `src/dlw/services/audit.py` — system-scope events use `tenant_id=None`/`actor_user_id=None`, like SP1's other system audits.)
(Use the `_gs` alias — `get_settings` is not in lifespan scope.) In `_on_active`, next to the existing `rebalance_task_holder["t"] = ...`, add:
```python
        gc_task_holder["t"] = asyncio.create_task(_gc_loop())
```
In `_on_step_down`, after the existing rebalance-holder cancel block, add the same cancel block for `gc_task_holder` (mirror it exactly: `gt = gc_task_holder["t"]; if gt is not None: gt.cancel(); try: await asyncio.wait_for(gt, timeout=2) except (asyncio.CancelledError, asyncio.TimeoutError): pass; gc_task_holder["t"] = None`).

- [ ] **Step 4: Run** `uv run pytest tests/test_sp3_lifespan.py tests/test_sp2_lifespan.py tests/test_lifespan_state.py -v` → PASS (no lifespan regression).

- [ ] **Step 5: Commit**
```bash
git add src/dlw/main.py tests/test_sp3_lifespan.py
git commit -m "feat(sp3): leader-gated GC loop (reclaims refcount=0 storage_objects)"
```

---

# Milestone M5 — E2E + Docs + PR

### Task 11: E2E incremental (≥90% inherit)

**Files:** Create `tests/e2e/test_incremental.py`

- [ ] **Step 1: Write the test** — `tests/e2e/test_incremental.py`:
```python
"""E2E-incremental: an upgrade with 1 changed file inherits the rest (SP3).

Planner-level end-to-end (no live mirrors): seed storage_objects as if
revision `old` completed; create the `new` task (upgrade_from_revision=old)
where all files but one keep their sha; assert ≥90% become inherit."""
from __future__ import annotations

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import async_sessionmaker

import dlw.db.models  # noqa: F401
from dlw.db.base import Base
from dlw.db.models.storage_object import StorageObject
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.services.incremental import diff_and_dedup

pytestmark = pytest.mark.slow


@pytest.fixture
async def f(engine):
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
        await c.run_sync(Base.metadata.create_all)
    fac = async_sessionmaker(engine, expire_on_commit=False)
    async with fac() as s:
        s.add(Tenant(id=1, slug="t", display_name="T"))
        await s.flush()
        s.add_all([Project(id=1, tenant_id=1, name="d"),
                   User(id=1, tenant_id=1, oidc_subject="u", email="e",
                        role="tenant_operator")])
        await s.commit()
    yield fac
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)


async def test_upgrade_inherits_at_least_90pct(f):
    N = 20
    async with f() as s:
        # revision `old` already produced N storage_objects (sha = f"{i}"*64)
        for i in range(N):
            s.add(StorageObject(tenant_id=1, storage_id=1,
                                storage_key=f"o/r/old/file{i}",
                                sha256=f"{i:02d}".ljust(64, "a"), size=100))
        t = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                         repo_id="o/r", revision="new", storage_id=1,
                         path_template="t", status="scheduling",
                         upgrade_from_revision="old")
        s.add(t)
        await s.flush()
        # new revision: file0 changed (new sha), file1..N-1 identical
        for i in range(N):
            sha = ("ff".ljust(64, "f") if i == 0
                   else f"{i:02d}".ljust(64, "a"))
            s.add(FileSubTask(task_id=t.id, tenant_id=1, filename=f"file{i}",
                              file_size=100, expected_sha256=sha,
                              status="pending"))
        await s.commit()
        await diff_and_dedup(s, t)
        await s.commit()
        inherited = await s.scalar(select(func.count()).select_from(
            FileSubTask).where(FileSubTask.task_id == t.id,
                               FileSubTask.status == "inherit"))
        pending = await s.scalar(select(func.count()).select_from(
            FileSubTask).where(FileSubTask.task_id == t.id,
                               FileSubTask.status == "pending"))
        assert inherited == N - 1 and pending == 1
        assert inherited / N >= 0.90       # roadmap §3.5 exit
```

- [ ] **Step 2: Run** `uv run pytest tests/e2e/test_incremental.py -v` → 1 PASS (planner already implemented; this is the acceptance gate — if it fails, fix the code, not the test).

- [ ] **Step 3: Commit**
```bash
git add tests/e2e/test_incremental.py
git commit -m "test(sp3): E2E-incremental — 1-file-changed upgrade inherits >=90%"
```

---

### Task 12: OpenAPI + operator doc + full CI gates + PR

**Files:** Modify `api/openapi.yaml`; Create `docs/operator/incremental-download.md`

- [ ] **Step 1: Update `api/openapi.yaml`** — add the `DELETE /api/v1/tasks/{taskId}` operation (tag `tasks`; path param `taskId`; responses 204, 404, 409 `TASK_NOT_TERMINAL`, 401, 403) mirroring the existing `POST /api/v1/tasks/{taskId}/cancel` operation's structure. Add `upgrade_from_revision` (string, nullable, maxLength 64) to the `TaskCreate` request schema. Add `inherit` to the subtask `status` enum if one is defined, and `inherit_from_key` (string, nullable) to the subtask/`SubTaskRead` schema. Every `$ref` must resolve.

- [ ] **Step 2: Run the exact OpenAPI CI commands** (both must pass — spectral 0 errors, warnings OK):
```
npx --yes @stoplight/spectral-cli@6 lint api/openapi.yaml --fail-severity=error
npx --yes @apidevtools/swagger-cli validate api/openapi.yaml
```
Keep 2-space indent / no trailing whitespace (yamllint scans `api/`).

- [ ] **Step 3: Create `docs/operator/incremental-download.md`** (~90 lines): how `upgrade_from_revision` works (sha-diff vs prior storage_objects, unified with cross-task dedup); the `storage_objects` refcount model + INVARIANT 14 (one physical copy per tenant+backend+content); inherit materialization (S3 server-side `copy_object` / local `os.link`, no re-download); `DELETE /api/v1/tasks/{id}` semantics (terminal-only → 409; derefs; does NOT delete bytes); the leader-gated GC (`DLW_GC_INTERVAL_SECONDS`/`DLW_GC_GRACE_SECONDS`; deletes refcount=0 DB rows past grace, audited `storage.gc`); and the deferred-to-Phase-4 items (physical S3/fs byte reclamation; quota/LRU eviction). **Include this explicit sentence (banner 6a):** "An inherited file's `storage_objects` row tracks the *original* (source) key; the executor's server-side copy creates new-revision-key bytes that are NOT tracked by any `storage_objects` row — these are orphaned bytes reclaimed in Phase 4 (physical GC); SP3's GC only frees refcount=0 DB rows." Also note the inherit-copy-failure self-heal (banner 7f: a failed inherit copy is dereferenced and re-queued as a normal download). Cross-ref `docs/v2.0/06-platform-and-ecosystem.md` §2/§3 and `INVARIANTS` 14.

- [ ] **Step 4: Full suite + all real CI gates**:
```
uv run pytest -q
uv run python -m pytest tools/test_lint_invariants.py -q
python tools/lint_invariants.py
python tools/lint_no_direct_status_write.py
uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head
```
All green. Confirm `python tools/lint_invariants.py` exits 0 (`"inherit"` added in Task 1; the only scanned-file change is `scheduler.py`'s `.in_(("pending","inherit"))` read filter + the `record_object` call — no invalid status assignment).

- [ ] **Step 5: Commit + push + PR**
```bash
git add api/openapi.yaml docs/operator/incremental-download.md
git commit -m "docs(sp3): OpenAPI DELETE/upgrade_from_revision + operator incremental guide"
git push -u origin feat/phase-3-sp3-incremental-download
gh pr create --title "Phase 3 SP3 — Incremental download + global dedup (refcount/GC)" --body "$(cat <<'EOF'
## Summary
- `upgrade_from_revision` + unified diff/dedup: a file whose HF sha already has a `storage_objects` row for (tenant,storage,sha) → `inherit` subtask (executor S3 copy_object / os.link, no download); else normal SP2 download.
- `storage_objects` refcount (INVARIANT 14 = UNIQUE(tenant,storage,sha)) + `subtask_object_refs`; `record_object` on success is inherit-idempotent (ref-exists ⇒ no double-count).
- `DELETE /api/v1/tasks/{id}` (terminal-only, tenant-scoped, RBAC) derefs; leader-gated GC loop reclaims refcount=0 rows past grace. Phase 3 sub-project 3 of 4 (SP1 #15, SP2 #16 merged).

## Test plan
- [ ] `uv run pytest` green incl. `tests/e2e/test_incremental.py` (≥90% inherit, roadmap §3.5)
- [ ] invariant_lint (+ `"inherit"` in VALID_SUBTASK_STATUS) / openapi(spectral+swagger-cli) / yamllint green
- [ ] alembic up/down/up clean (down_revision bb1dd2c45a12)

Spec: docs/superpowers/specs/2026-05-19-phase-3-sp3-incremental-download-design.md
Plan: docs/superpowers/plans/2026-05-19-phase-3-sp3-incremental-download.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review (completed during planning + revised after 2-reviewer pre-execution review)

**Pre-execution review applied (2026-05-19):** Two opus reviewers found 1 BLOCKER + 5 IMPORTANT, all fixed inline before implementation (spec banner §7 + plan tasks): (1 BLOCKER) `plan_task_sources` pinned/no-sha/no-speed gates strand a fully-inherited task → T6 relocates a pending-subs `if not subs: return` to the TOP of `plan_task_sources` before all gates + the T6 test now asserts `status=="downloading"`; (2) `StorageConfig` had no `backend_type` (local-hardlink branch dead) → T1 adds `backend_type`/`base_path` to `StorageConfig`, T8 threads `StorageBackend.backend_type` through the poll `cfg_dict`; (3) runner anchor was wrong → T8 uses `_execute_subtask`/`self._s` (verified `runner.py:47/193/214`); (4) spec-required `storage.gc` audit was missing → T10 `_gc_loop` calls `write_audit(action="storage.gc",...)`; (5) `create_task` never threaded `upgrade_from_revision` → T1 adds the ctor wire + a test; (6) inherit-then-fail refcount leak → T7 derefs + re-queues the sub to `pending` (banner 7f) + a test. Sound areas reviewers confirmed: record_object concurrency, the `_ref_exists` idempotency guard, claim→assigned→complete machine, DELETE FK-cascade, lint sufficiency.

**Spec coverage:** config/inherit-status/schema fields → T1; models+migration+INVARIANT-14 unique → T2; storage_objects svc + inherit idempotency → T3; deref/gc behavior → T4; diff_and_dedup → T5; scheduling-loop wiring + planner-skips-inherit → T6; record_object on complete + claim-includes-inherit → T7; executor inherit materialization + runner dispatch + poll payload → T8; DELETE route → T9; leader-gated GC loop → T10; E2E ≥90% → T11; OpenAPI+operator doc+CI+PR → T12. Deferred items (physical byte GC, quota/LRU, BLAKE3, CLI) are out per spec banner/§1.3.

**Placeholder scan:** every code step is complete; the previously-hand-waved T6 guard and T8 runner anchor are now concrete (pre-review fixes above). `<rev>` = alembic-generated hash.

**Type/name consistency:** `record_object`/`record_ref_only`/`deref_subtask`/`gc_orphans` signatures identical T3↔T4↔T5↔T7(now imports `deref_subtask` + `record_object`)↔T10. `StorageObject`/`SubtaskObjectRef` columns consistent T2↔T3↔T5↔T9↔T11. `diff_and_dedup(session, task)` consistent T5↔T6. `materialize_inherit(*, settings, storage_config, src_key, dst_key, sha256, size)` consistent T8↔(runner call). `DownloadResult(bytes_written, actual_sha256, s3_key)` matches existing `executor/types.py`. `inherit_from_key` consistent across schema (T1), model (T2), diff (T5), runner (T8), DELETE-unaffected. `"inherit"` status: added to lint set T1, set by T5, claimed by T7, materialized by T8. The inherit double-count guard (`_ref_exists` ⇒ early return) is defined in T3 and exercised by T3+T7 tests — the load-bearing correctness point.

## References
- Spec: `docs/superpowers/specs/2026-05-19-phase-3-sp3-incremental-download-design.md`
- Design doc: `docs/v2.0/06-platform-and-ecosystem.md` §2/§3.1/§3.3; INVARIANT 14.
- Code anchors: `services/task_service.py` `create_task`; `services/scheduler.py` `complete_subtask` (SP2 hook ~L200-209) + `claim_one_subtask` (`.status=="pending"` ~L76); `services/source_scheduler.py` `run_scheduling_tick`/`plan_task_sources`; `executor/runner.py` `_run_one` (~L200-251); `executor/_io.py` `compose_key`; `executor/types.py` `Assignment`/`DownloadResult`; `schemas/subtask.py` `SubTaskRead`; `tools/lint_invariants.py` `VALID_SUBTASK_STATUS` (L88-91); alembic head `bb1dd2c45a12`.
- Branch `feat/phase-3-sp3-incremental-download` off `main` (`454ac41`), spec `23a85f1`.
