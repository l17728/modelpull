# FU9 — Pre-migration Orphan Backfill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development / executing-plans. Checkbox steps.

**Goal:** Heartbeat-driven adoption of NULL-executor_id `storage_physical_keys` rows, making `count_stuck_local_orphans` accurate (only genuinely-unreachable keys stay NULL after any advertising executor heartbeats).

**Spec:** `docs/superpowers/specs/2026-05-23-fu9-orphan-backfill-design.md` (read fully).

**Locked constraints:**
- No migration. Rides FU3 `executor_id` column + FU4 `acc_ids` machinery.
- **Not gated on `gc_delete_physical_bytes`** — adoption is metadata repair, not deletion.
- Guard at call site: `if acc_ids and executor.tenant_id is not None:` — skip adoption entirely when tenant_id is NULL (Executor.tenant_id is nullable; without this guard adoption runs without tenant filter → cross-tenant adoption).
- Implementation follows `dispatch_local_reclaim` SELECT-then-UPDATE pattern (not UPDATE-via-subquery): `SELECT ids WITH FOR UPDATE SKIP LOCKED` → Python list → `UPDATE ... WHERE id IN ids`.
- Capped at `gc_max_objects_per_tick` per heartbeat.
- Audit fires only when `adopted > 0` (not every heartbeat).
- WHERE `executor_id IS NULL` — already-owned rows never touched (idempotent).
- Test fixtures: module-scoped `_bootstrap` + local `session` per `tests/services/test_physical_keys.py` pattern; hard-coded `tenant_id=1, storage_id=1`.
- Integration test uses `accessible_base_paths` (not `base_paths`) in heartbeat body; seeds a local StorageBackend at `id=9002` (avoids collision with id=9001 used by `test_heartbeat_dispatch_enabled_returns_reclaim_item`).
- Lint gate: `uv run python tools/lint_invariants.py --strict` (not the module form).
- Zero openapi / frontend / executor-protocol change.

---

## File Structure

- **Modify** `src/dlw/services/storage_objects.py` — add `adopt_orphan_local_keys`.
- **Modify** `src/dlw/api/executors.py` — call adoption in `post_heartbeat` (after `acc_ids`, before `if body.reclaimed_key_ids:`).
- **Create** `tests/services/test_storage_orphan_adopt.py` — service-layer unit tests.
- **Extend** `tests/api/test_executors.py` — heartbeat-triggers-adoption integration test.

---

## Milestone M1 — service function + tests

### Task 1: `adopt_orphan_local_keys` + unit tests

**Files:** `src/dlw/services/storage_objects.py`, new `tests/services/test_storage_orphan_adopt.py`.

- [ ] **Step 1 (failing tests):** create `tests/services/test_storage_orphan_adopt.py`.

  Fixture pattern: identical to `tests/services/test_physical_keys.py` — module-scoped `_bootstrap(engine)` + per-test `session(engine)`. The `engine` fixture comes from `tests/conftest.py`. Use `tenant_id=1, storage_id=1` (already seeded by `_bootstrap`). The `storage_id=1` backend is `backend_type="s3"` — that's fine; `adopt_orphan_local_keys` does not filter by backend_type (the caller's `acc_ids` already restricts to local, but the service function itself is backend-agnostic so it can be unit-tested against any storage_id in acc_ids).

  ```python
  """FU9 — adopt_orphan_local_keys (heartbeat-driven executor_id backfill)."""
  from __future__ import annotations

  import pytest
  from sqlalchemy import select
  from sqlalchemy.ext.asyncio import async_sessionmaker

  from dlw.db.base import Base
  from dlw.db.models.storage_object import StoragePhysicalKey
  from dlw.services.storage_objects import adopt_orphan_local_keys


  @pytest.fixture(scope="module", autouse=True)
  async def _bootstrap(engine):
      from dlw.db.models.storage import StorageBackend
      from dlw.db.models.tenant import Project, Tenant, User
      async with engine.begin() as conn:
          await conn.run_sync(Base.metadata.drop_all)
          await conn.run_sync(Base.metadata.create_all)
      f = async_sessionmaker(engine, expire_on_commit=False)
      async with f() as s:
          s.add(Tenant(id=1, slug="t1", display_name="T1"))
          await s.flush()
          s.add(Project(id=1, tenant_id=1, name="proj1"))
          s.add(User(id=1, tenant_id=1, oidc_subject="u1", email="u1@t",
                     role="tenant_operator"))
          s.add(StorageBackend(id=1, tenant_id=1, name="bkt",
                               backend_type="s3", config_encrypted=b"{}"))
          await s.commit()
      yield
      async with engine.begin() as conn:
          await conn.run_sync(Base.metadata.drop_all)


  @pytest.fixture
  async def session(engine):
      f = async_sessionmaker(engine, expire_on_commit=False)
      async with f() as s:
          yield s


  @pytest.mark.asyncio
  async def test_adopt_null_key_on_accessible_storage(session):
      """NULL-executor_id key on accessible storage_id → adopted, returns 1."""
      key = StoragePhysicalKey(
          tenant_id=1, storage_id=1, sha256="a" * 64,
          storage_key="fu9/a.bin", size=100, executor_id=None)
      session.add(key)
      await session.flush()

      n = await adopt_orphan_local_keys(
          session, "exec-1",
          accessible_storage_ids=frozenset([1]),
          limit=10, tenant_id=1)

      assert n == 1
      await session.refresh(key)
      assert key.executor_id == "exec-1"
      await session.rollback()


  @pytest.mark.asyncio
  async def test_adopt_skips_already_owned(session):
      """Key already has executor_id → unchanged, returns 0."""
      key = StoragePhysicalKey(
          tenant_id=1, storage_id=1, sha256="b" * 64,
          storage_key="fu9/b.bin", size=100, executor_id="old-exec")
      session.add(key)
      await session.flush()

      n = await adopt_orphan_local_keys(
          session, "exec-1",
          accessible_storage_ids=frozenset([1]),
          limit=10, tenant_id=1)

      assert n == 0
      await session.refresh(key)
      assert key.executor_id == "old-exec"
      await session.rollback()


  @pytest.mark.asyncio
  async def test_adopt_skips_other_tenant(session):
      """NULL key on storage_id=1 tenant=1; adoption called with tenant=2 → not adopted."""
      key = StoragePhysicalKey(
          tenant_id=1, storage_id=1, sha256="c" * 64,
          storage_key="fu9/c.bin", size=100, executor_id=None)
      session.add(key)
      await session.flush()

      # Executor of tenant=2 should NOT adopt tenant=1 key
      n = await adopt_orphan_local_keys(
          session, "exec-1",
          accessible_storage_ids=frozenset([1]),
          limit=10, tenant_id=2)

      assert n == 0
      await session.refresh(key)
      assert key.executor_id is None
      await session.rollback()


  @pytest.mark.asyncio
  async def test_adopt_empty_acc_ids(session):
      """Empty accessible_storage_ids → returns 0 immediately, no DB access."""
      key = StoragePhysicalKey(
          tenant_id=1, storage_id=1, sha256="d" * 64,
          storage_key="fu9/d.bin", size=100, executor_id=None)
      session.add(key)
      await session.flush()

      n = await adopt_orphan_local_keys(
          session, "exec-1",
          accessible_storage_ids=frozenset(),
          limit=10, tenant_id=1)

      assert n == 0
      await session.refresh(key)
      assert key.executor_id is None
      await session.rollback()


  @pytest.mark.asyncio
  async def test_adopt_limit(session):
      """limit=2 with 5 NULL keys → exactly 2 adopted, 3 remain NULL."""
      # Use distinct sha256/keys to avoid collisions with earlier tests
      keys = [
          StoragePhysicalKey(
              tenant_id=1, storage_id=1,
              sha256=f"{'e' * 63}{i}", storage_key=f"fu9/lim{i}.bin",
              size=100, executor_id=None)
          for i in range(5)
      ]
      session.add_all(keys)
      await session.flush()

      n = await adopt_orphan_local_keys(
          session, "exec-1",
          accessible_storage_ids=frozenset([1]),
          limit=2, tenant_id=1)

      assert n == 2
      remaining_null = (await session.execute(
          select(StoragePhysicalKey)
          .where(StoragePhysicalKey.executor_id.is_(None),
                 StoragePhysicalKey.storage_key.like("fu9/lim%.bin"))
      )).scalars().all()
      assert len(remaining_null) == 3
      await session.rollback()
  ```

- [ ] **Step 2: verify FAIL** — `uv run pytest tests/services/test_storage_orphan_adopt.py -v` — expect `ImportError: cannot import name 'adopt_orphan_local_keys'`.

- [ ] **Step 3 (implement `adopt_orphan_local_keys`):** add to `src/dlw/services/storage_objects.py` (after `record_physical_key`, before `pressured_tenant_ids`). Follow the SELECT-then-UPDATE pattern from `dispatch_local_reclaim` — NOT an `UPDATE ... WHERE id IN (subquery)`:

  ```python
  async def adopt_orphan_local_keys(
      session: AsyncSession,
      executor_id: str,
      *,
      accessible_storage_ids: frozenset[int],
      limit: int,
      tenant_id: int | None = None,
  ) -> int:
      """Heartbeat-driven backfill: set executor_id on NULL-owner physical keys
      for backends this executor provably mounts. Caller commits.
      Returns count of rows adopted."""
      if not accessible_storage_ids:
          return 0
      filters = [
          StoragePhysicalKey.executor_id.is_(None),
          StoragePhysicalKey.storage_id.in_(accessible_storage_ids),
      ]
      if tenant_id is not None:
          filters.append(StoragePhysicalKey.tenant_id == tenant_id)
      ids: list[int] = (await session.execute(
          select(StoragePhysicalKey.id)
          .where(*filters)
          .limit(max(1, limit))
          .with_for_update(skip_locked=True)
      )).scalars().all()
      if not ids:
          return 0
      await session.execute(
          update(StoragePhysicalKey)
          .where(StoragePhysicalKey.id.in_(ids))
          .values(executor_id=executor_id)
      )
      return len(ids)
  ```

  `update` is already imported at line 8 of `storage_objects.py`.

- [ ] **Step 4: verify PASS** — `uv run pytest tests/services/test_storage_orphan_adopt.py -v` — all 5 tests pass.

- [ ] **Step 5: tidy + commit:**
  ```bash
  uv run ruff check --select I001 --fix src/dlw/services/storage_objects.py tests/services/test_storage_orphan_adopt.py
  git add src/dlw/services/storage_objects.py tests/services/test_storage_orphan_adopt.py
  git commit -m "feat(fu9): adopt_orphan_local_keys service fn + tests"
  ```

### Task 2: M1 gate
- [ ] `uv run pytest tests/services/ -q` — all pass. No commit.

---

## Milestone M2 — wire into heartbeat + integration test + full gate

### Task 3: `post_heartbeat` wiring + integration test

**Files:** `src/dlw/api/executors.py`, `tests/api/test_executors.py`.

- [ ] **Step 1 (failing integration test):** add to `tests/api/test_executors.py` after the existing FU4 base_paths test (after line 530):

  Pattern mirrors `test_heartbeat_dispatch_enabled_returns_reclaim_item` (lines 372–445): register executor, seed a local StorageBackend at `id=9002` (a different id from the existing 9001 used by that test), seed a NULL-executor_id key on it, send a heartbeat with `accessible_base_paths=[base_path]`.

  ```python
  # ── FU9: heartbeat adopts NULL-executor_id keys on accessible backends ────────

  @pytest.mark.slow
  async def test_heartbeat_adopts_orphan_local_keys(
      client: AsyncClient, engine, monkeypatch: pytest.MonkeyPatch,
  ) -> None:
      """Heartbeat with accessible_base_paths → NULL-executor_id keys on that
      backend get executor_id backfilled (FU9 adoption)."""
      from dlw.db.models.storage import StorageBackend
      from dlw.db.models.storage_object import StoragePhysicalKey

      reg = await register_test_executor(
          client, enrollment_token=_ENROLL,
          executor_id="ex-adopt-1", host_id="host-adopt",
      )
      ex_id = reg["executor_id"]

      base_path = "/tmp/fu9-adopt-store"
      cfg_bytes = json.dumps({
          "bucket": "local-bucket", "base_path": base_path,
          "backend_type": "local",
      }).encode()
      factory = async_sessionmaker(engine, expire_on_commit=False)
      local_storage_id = 9002  # avoids collision with 9001 used by dispatch test
      async with factory() as s:
          s.add(StorageBackend(
              id=local_storage_id, tenant_id=1, name=f"local-adopt-{ex_id}",
              backend_type="local", config_encrypted=cfg_bytes,
          ))
          # Seed a NULL-executor_id key on this local backend
          s.add(StoragePhysicalKey(
              tenant_id=1, storage_id=local_storage_id,
              sha256="fa" * 32, storage_key="models/adopt/file.bin",
              size=256, executor_id=None,
          ))
          await s.commit()

      hb_body = json.dumps({
          "health_score": 100, "parts_dir_bytes": 0,
          "accessible_base_paths": [base_path],
      }).encode()
      r = await client.post(
          f"/api/v1/executors/{ex_id}/heartbeat",
          content=hb_body,
          headers=signed_heartbeat_headers(reg, hb_body),
      )
      assert r.status_code == 200, r.text

      # Verify the NULL key now has executor_id = ex_id
      async with factory() as s:
          row = (await s.execute(
              select(StoragePhysicalKey)
              .where(StoragePhysicalKey.storage_key == "models/adopt/file.bin")
          )).scalar_one()
          assert row.executor_id == ex_id

      # Cleanup
      async with factory() as s:
          row = (await s.execute(
              select(StoragePhysicalKey)
              .where(StoragePhysicalKey.storage_key == "models/adopt/file.bin")
          )).scalar_one_or_none()
          if row:
              await s.delete(row)
          backend = await s.get(StorageBackend, local_storage_id)
          if backend:
              await s.delete(backend)
          await s.commit()
  ```

  **Note:** The integration test does NOT use `monkeypatch.setenv("DLW_GC_DELETE_PHYSICAL_BYTES", "true")` — adoption runs regardless of GC being enabled.

- [ ] **Step 2: verify FAIL** — `uv run pytest tests/api/test_executors.py::test_heartbeat_adopts_orphan_local_keys -v` — expect FAIL (adoption not wired in heartbeat yet).

- [ ] **Step 3 (wire into `post_heartbeat`):** in `src/dlw/api/executors.py`, in `post_heartbeat`, after the `acc_ids` line (~166) and before `if body.reclaimed_key_ids:` (~168):

  ```python
  # FU9: adopt NULL-executor_id local keys on backends this executor mounts.
  # Gated on both acc_ids being non-empty AND executor.tenant_id being set
  # (prevents cross-tenant adoption when tenant_id is None).
  if acc_ids and executor.tenant_id is not None:
      from dlw.services.storage_objects import adopt_orphan_local_keys
      _adopted = await adopt_orphan_local_keys(
          session, executor.id,
          accessible_storage_ids=acc_ids,
          limit=s.gc_max_objects_per_tick,
          tenant_id=executor.tenant_id,
      )
      if _adopted:
          await write_audit(
              session, action="storage.local.adopt",
              resource_type="storage_physical_keys",
              resource_id=executor.id, outcome="success",
              tenant_id=executor.tenant_id, actor_user_id=None,
              payload={"adopted": _adopted},
          )
  ```

  The `write_audit` import is already at module level in `executors.py`.

- [ ] **Step 4: verify PASS** — `uv run pytest tests/api/test_executors.py -v` — all existing + new test pass.

- [ ] **Step 5: tidy + commit:**
  ```bash
  uv run ruff check --select I001 --fix src/dlw/api/executors.py tests/api/test_executors.py
  git add src/dlw/api/executors.py tests/api/test_executors.py
  git commit -m "feat(fu9): wire adopt_orphan_local_keys into post_heartbeat"
  ```

### Task 4: M2 full gate
- [ ] `uv run pytest -q` — ALL pass (test_failover_drill = Windows-local flake; isolate-confirm). `uv run python tools/lint_invariants.py --strict` OK. No commit.

---

## Self-Review

- **Spec coverage:** §0 guard empty acc_ids → Step 3 `if not accessible_storage_ids: return 0` ✓; tenant_id guard → Step 3 `if tenant_id is not None` + M2 Step 3 call site guard `executor.tenant_id is not None` ✓; SELECT-then-UPDATE with SKIP LOCKED → Step 3 pattern (consistent with `dispatch_local_reclaim`) ✓; not gated on gc_delete_physical_bytes → M2 Step 3 has no GC gate ✓; audit only on adopted>0 → Step 3 wiring `if _adopted:` ✓; capped → `limit=s.gc_max_objects_per_tick` ✓; §2 tests 5 unit + 1 integration ✓.
- **Placeholder scan:** no TBDs. Fixture patterns are concrete (module `_bootstrap` + `session`, hard-coded tenant_id=1/storage_id=1). Integration test uses `accessible_base_paths`, `id=9002`, `async_sessionmaker` pattern — all confirmed from real test file.
- **Type consistency:** `adopt_orphan_local_keys(AsyncSession, str, *, frozenset[int], int, int|None) -> int`; returns `len(ids)` (int); `_adopted: int`; `executor.id: str`; `executor.tenant_id: int | None`.
- **Open risks:** (a) Two executors of same tenant racing on same NULL key: SKIP LOCKED on the SELECT means one gets the lock and the other skips that row → no double-adopt. (b) Audit `resource_id=executor.id` is per-bulk-batch (count in payload) — consistent with the established style for batch audit events. (c) `adopt_orphan_local_keys` has no `backend_type="local"` filter — it adopts any NULL key whose `storage_id` is in `acc_ids`; since `resolve_accessible_storage_ids` already restricts to local backends, `acc_ids` is already local-only → correct without redundant filter in the service. (d) `count_stuck_local_orphans` is only called inside `if s.gc_delete_physical_bytes:` — FU9 adoption improves gauge accuracy but gauge only emits when GC is enabled; this is pre-existing behavior, not a new gap.
