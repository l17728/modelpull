# FU9 — Pre-migration Orphan Backfill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development / executing-plans. Checkbox steps.

**Goal:** Heartbeat-driven adoption of NULL-executor_id `storage_physical_keys` rows, making `count_stuck_local_orphans` accurate (only genuinely-unreachable keys stay NULL after any advertising executor heartbeats).

**Spec:** `docs/superpowers/specs/2026-05-23-fu9-orphan-backfill-design.md` (read fully — §0 design/guard, §1 correctness, §2 tests, §3 files).

**Locked constraints:**
- No migration. Rides FU3 `executor_id` column + FU4 `acc_ids` machinery.
- **Not gated on `gc_delete_physical_bytes`** — adoption is metadata repair, not deletion.
- Tenant-scoped: `tenant_id` filter in UPDATE (executor only adopts its own tenant's keys).
- Capped at `gc_max_objects_per_tick` per heartbeat.
- Audit fires only when `adopted > 0` (not every heartbeat).
- WHERE clause is `executor_id IS NULL` — already-owned rows never touched (idempotent).
- `except` clauses: never catch bare `Exception` without `# noqa: BLE001`.
- Zero openapi / frontend / executor-protocol change.

---

## File Structure

- **Modify** `src/dlw/services/storage_objects.py` — add `adopt_orphan_local_keys`.
- **Modify** `src/dlw/api/executors.py` — call adoption in `post_heartbeat` (after `acc_ids`, before `dispatch_local_reclaim`).
- **Create** `tests/services/test_storage_orphan_adopt.py` — service-layer unit tests.
- **Extend** `tests/api/test_executors.py` — heartbeat-triggers-adoption integration test.

---

## Milestone M1 — service function + tests

### Task 1: `adopt_orphan_local_keys` + tests

**Files:** `src/dlw/services/storage_objects.py`, new `tests/services/test_storage_orphan_adopt.py`.

- [ ] **Step 1 (failing tests):** create `tests/services/test_storage_orphan_adopt.py`.

  Read `tests/services/test_physical_keys.py` first to understand the DB fixture pattern
  (Tenant → StorageBackend → DownloadTask → FileSubTask → StoragePhysicalKey seed chain).
  Also read `tests/conftest.py` to understand `db_session` and `bootstrap_db` fixtures.

  ```python
  """FU9 — adopt_orphan_local_keys (heartbeat-driven executor_id backfill)."""
  from __future__ import annotations

  import pytest
  from sqlalchemy import select

  from dlw.db.models.storage_object import StoragePhysicalKey
  from dlw.services.storage_objects import adopt_orphan_local_keys, count_stuck_local_orphans


  @pytest.mark.asyncio
  async def test_adopt_null_key_on_accessible_storage(db_session, bootstrap_db):
      """NULL-executor_id key on an accessible storage → adopted, returns 1."""
      tenant_id = bootstrap_db["tenant_id"]
      storage_id = bootstrap_db["storage_id"]
      key = StoragePhysicalKey(
          tenant_id=tenant_id, storage_id=storage_id, sha256="a" * 64,
          storage_key="m/a.bin", size=100, executor_id=None)
      db_session.add(key)
      await db_session.flush()

      n = await adopt_orphan_local_keys(
          db_session, "exec-1",
          accessible_storage_ids=frozenset([storage_id]),
          limit=10, tenant_id=tenant_id)

      assert n == 1
      await db_session.refresh(key)
      assert key.executor_id == "exec-1"


  @pytest.mark.asyncio
  async def test_adopt_skips_already_owned(db_session, bootstrap_db):
      """Key already has executor_id → unchanged, returns 0."""
      tenant_id = bootstrap_db["tenant_id"]
      storage_id = bootstrap_db["storage_id"]
      key = StoragePhysicalKey(
          tenant_id=tenant_id, storage_id=storage_id, sha256="b" * 64,
          storage_key="m/b.bin", size=100, executor_id="old-exec")
      db_session.add(key)
      await db_session.flush()

      n = await adopt_orphan_local_keys(
          db_session, "exec-1",
          accessible_storage_ids=frozenset([storage_id]),
          limit=10, tenant_id=tenant_id)

      assert n == 0
      await db_session.refresh(key)
      assert key.executor_id == "old-exec"


  @pytest.mark.asyncio
  async def test_adopt_skips_other_tenant(db_session, bootstrap_db):
      """NULL key belongs to a different tenant → not adopted."""
      tenant_id = bootstrap_db["tenant_id"]
      storage_id = bootstrap_db["storage_id"]
      other_tenant_id = tenant_id + 999  # not in DB, but only used for filter check
      key = StoragePhysicalKey(
          tenant_id=tenant_id, storage_id=storage_id, sha256="c" * 64,
          storage_key="m/c.bin", size=100, executor_id=None)
      db_session.add(key)
      await db_session.flush()

      n = await adopt_orphan_local_keys(
          db_session, "exec-1",
          accessible_storage_ids=frozenset([storage_id]),
          limit=10, tenant_id=other_tenant_id)

      assert n == 0
      await db_session.refresh(key)
      assert key.executor_id is None


  @pytest.mark.asyncio
  async def test_adopt_empty_acc_ids(db_session, bootstrap_db):
      """Empty accessible_storage_ids → immediate 0, no DB hit."""
      tenant_id = bootstrap_db["tenant_id"]
      storage_id = bootstrap_db["storage_id"]
      key = StoragePhysicalKey(
          tenant_id=tenant_id, storage_id=storage_id, sha256="d" * 64,
          storage_key="m/d.bin", size=100, executor_id=None)
      db_session.add(key)
      await db_session.flush()

      n = await adopt_orphan_local_keys(
          db_session, "exec-1",
          accessible_storage_ids=frozenset(),
          limit=10, tenant_id=tenant_id)

      assert n == 0
      await db_session.refresh(key)
      assert key.executor_id is None


  @pytest.mark.asyncio
  async def test_adopt_limit(db_session, bootstrap_db):
      """limit=2 with 5 NULL keys → exactly 2 adopted, 3 remain NULL."""
      tenant_id = bootstrap_db["tenant_id"]
      storage_id = bootstrap_db["storage_id"]
      keys = [
          StoragePhysicalKey(
              tenant_id=tenant_id, storage_id=storage_id,
              sha256=f"{'e' * 63}{i}", storage_key=f"m/e{i}.bin",
              size=100, executor_id=None)
          for i in range(5)
      ]
      db_session.add_all(keys)
      await db_session.flush()

      n = await adopt_orphan_local_keys(
          db_session, "exec-1",
          accessible_storage_ids=frozenset([storage_id]),
          limit=2, tenant_id=tenant_id)

      assert n == 2
      remaining = (await db_session.execute(
          select(StoragePhysicalKey)
          .where(StoragePhysicalKey.executor_id.is_(None),
                 StoragePhysicalKey.storage_id == storage_id,
                 StoragePhysicalKey.tenant_id == tenant_id)
      )).scalars().all()
      assert len(remaining) == 3
  ```

- [ ] **Step 2: verify FAIL** — `cd "D:/download_weights" && uv run pytest tests/services/test_storage_orphan_adopt.py -v` — expect `ImportError` (function doesn't exist yet).

- [ ] **Step 3 (implement `adopt_orphan_local_keys`):** add to `src/dlw/services/storage_objects.py` (after `record_physical_key`, before `pressured_tenant_ids`):

  ```python
  async def adopt_orphan_local_keys(
      session: AsyncSession,
      executor_id: str,
      *,
      accessible_storage_ids: frozenset[int],
      limit: int,
      tenant_id: int | None = None,
  ) -> int:
      """Heartbeat-driven backfill: claim NULL-executor_id physical keys on backends
      this executor provably mounts. Makes count_stuck_local_orphans accurate.
      Caller commits. Returns count of rows adopted."""
      if not accessible_storage_ids:
          return 0
      stmt = (
          update(StoragePhysicalKey)
          .where(
              StoragePhysicalKey.executor_id.is_(None),
              StoragePhysicalKey.storage_id.in_(accessible_storage_ids),
              *([StoragePhysicalKey.tenant_id == tenant_id] if tenant_id is not None else []),
          )
          .values(executor_id=executor_id)
          .returning(StoragePhysicalKey.id)
      )
      # LIMIT on UPDATE is not portable; use a subquery to cap rows.
      ids_to_adopt = (
          select(StoragePhysicalKey.id)
          .where(
              StoragePhysicalKey.executor_id.is_(None),
              StoragePhysicalKey.storage_id.in_(accessible_storage_ids),
              *([StoragePhysicalKey.tenant_id == tenant_id] if tenant_id is not None else []),
          )
          .limit(max(1, limit))
          .with_for_update(skip_locked=True)
      )
      result = await session.execute(
          update(StoragePhysicalKey)
          .where(StoragePhysicalKey.id.in_(ids_to_adopt))
          .values(executor_id=executor_id)
          .returning(StoragePhysicalKey.id)
      )
      return len(result.fetchall())
  ```

  **Note on LIMIT**: PostgreSQL supports `UPDATE ... WHERE id IN (SELECT id ... LIMIT n)` but not `UPDATE ... LIMIT n` directly. The subquery with `with_for_update(skip_locked=True)` is the correct pattern — consistent with `dispatch_local_reclaim`.

- [ ] **Step 4: verify PASS** — `cd "D:/download_weights" && uv run pytest tests/services/test_storage_orphan_adopt.py -v` — all 5 tests pass.

- [ ] **Step 5: tidy + commit:**
  ```bash
  cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/services/storage_objects.py tests/services/test_storage_orphan_adopt.py
  git add src/dlw/services/storage_objects.py tests/services/test_storage_orphan_adopt.py
  git commit -m "feat(fu9): adopt_orphan_local_keys service fn + tests"
  ```

### Task 2: M1 gate
- [ ] `cd "D:/download_weights" && uv run pytest tests/services/ -q` — all pass. No commit.

---

## Milestone M2 — wire into heartbeat + full gate

### Task 3: `post_heartbeat` wiring + integration test

**Files:** `src/dlw/api/executors.py`, `tests/api/test_executors.py`.

- [ ] **Step 1 (failing integration test):** add to `tests/api/test_executors.py` after existing heartbeat tests:

  Read the existing heartbeat test in `test_executors.py` to understand the client/auth/executor fixture pattern before writing. The key helper is `service_headers(token)` from conftest, and `bootstrap_db` gives `storage_id` etc.

  ```python
  @pytest.mark.asyncio
  async def test_heartbeat_adopts_orphan_local_keys(
      client, executor_headers, bootstrap_db, db_session,
  ):
      """Heartbeat with base_paths → NULL-executor_id keys on that storage adopted."""
      from sqlalchemy import select as sa_select

      from dlw.db.models.storage_object import StoragePhysicalKey

      tenant_id = bootstrap_db["tenant_id"]
      storage_id = bootstrap_db["storage_id"]
      base_path = bootstrap_db.get("base_path", "/mnt/nfs")

      # Seed a NULL-executor_id key on this storage
      key = StoragePhysicalKey(
          tenant_id=tenant_id, storage_id=storage_id,
          sha256="f" * 64, storage_key="m/f.bin",
          size=200, executor_id=None)
      db_session.add(key)
      await db_session.commit()

      # Heartbeat with base_paths = [base_path]
      resp = client.post(
          f"/api/v1/executors/{executor_headers['executor_id']}/heartbeat",
          headers=executor_headers,
          json={"reclaimed_key_ids": [], "base_paths": [base_path]},
      )
      assert resp.status_code == 200

      # Key should now have executor_id set
      await db_session.refresh(key)
      assert key.executor_id is not None
  ```

  **Note:** Check how `executor_headers` and `bootstrap_db["base_path"]` are set up in the existing test file — adapt if the fixture doesn't include `base_path`. The key insight is that `resolve_accessible_storage_ids` maps base_paths to storage_ids; the test may need to ensure the storage backend has `backend_type="local"` and the right `base_path` in its config. Read the relevant fixture code before finalizing.

- [ ] **Step 2: verify FAIL** — `cd "D:/download_weights" && uv run pytest tests/api/test_executors.py::test_heartbeat_adopts_orphan_local_keys -v` — expect FAIL (adoption not wired yet).

- [ ] **Step 3 (wire into `post_heartbeat`):** in `src/dlw/api/executors.py`, in `post_heartbeat`, after `acc_ids` is computed (line ~166) and before `if body.reclaimed_key_ids:` (line ~168), add:

  ```python
  if acc_ids:
      from dlw.services.storage_objects import adopt_orphan_local_keys
      adopted = await adopt_orphan_local_keys(
          session, executor.id,
          accessible_storage_ids=acc_ids,
          limit=s.gc_max_objects_per_tick,
          tenant_id=executor.tenant_id,
      )
      if adopted:
          await write_audit(
              session, action="storage.local.adopt",
              resource_type="storage_physical_keys",
              resource_id=executor.id, outcome="success",
              tenant_id=executor.tenant_id, actor_user_id=None,
              payload={"adopted": adopted},
          )
  ```

  The import can be lazy (inside the if-block) to match the existing `dispatch_local_reclaim` style, or moved to the top of the function — either is fine. The audit import `write_audit` is already at module level.

- [ ] **Step 4: verify PASS** — `cd "D:/download_weights" && uv run pytest tests/api/test_executors.py -v` — all pass including the new integration test.

- [ ] **Step 5: tidy + commit:**
  ```bash
  cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/api/executors.py tests/api/test_executors.py
  git add src/dlw/api/executors.py tests/api/test_executors.py
  git commit -m "feat(fu9): wire adopt_orphan_local_keys into post_heartbeat"
  ```

### Task 4: M2 full gate
- [ ] `cd "D:/download_weights" && uv run pytest -q` — ALL pass (test_failover_drill = Windows-local flake; isolate-confirm). `uv run python -m dlw.tools.lint_invariants --strict` OK. No commit.

---

## Self-Review

- **Spec coverage:** §0 guard (empty acc_ids) → Task 1 Step 3 ✓; tenant_id scoped → Step 3 WHERE ✓; cap LIMIT → Step 3 subquery ✓; not gated on gc_delete_physical_bytes → absent from wiring ✓; audit fires only on adopted>0 → Step 3 wiring `if adopted:` ✓; §2 tests → Task 1 5 unit tests + Task 3 integration ✓.
- **Placeholder scan:** all code blocks are concrete. No TBDs.
- **Type consistency:** `adopt_orphan_local_keys(session, str, *, frozenset[int], int, int|None) -> int`; `adopted: int` in wiring; `write_audit(..., resource_id=executor.id)` where `executor.id: str` (executor IDs are strings — FU3/FU4 pattern confirmed).
- **Open risks for reviewers:** (a) subquery-with-LIMIT UPDATE pattern — is `with_for_update(skip_locked=True)` on the subquery correct in SQLAlchemy async? (Answer: yes — same pattern as `dispatch_local_reclaim` in storage_objects.py which uses this exact idiom.) (b) no cross-tenant race: since `tenant_id` is in the WHERE of both subquery and update, two executors from different tenants can't steal each other's keys. (c) two executors of the same tenant racing on the same key: `executor_id IS NULL` in subquery + `SKIP LOCKED` ensures they don't double-adopt the same row (one gets the lock, updates it; the other skips it). (d) audit `resource_id=executor.id` (string ID, not the key ID) — consistent with FU3/FU4 style for bulk operations; adopted count in payload.
