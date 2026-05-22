# FU5 — True `last_referenced_at` LRU Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development / executing-plans. Checkbox steps.

**Goal:** Add a real `last_referenced_at` column to `storage_physical_keys`, bump it whenever the key's content is (re)referenced, and use it (instead of the `created_at` proxy) as the LRU key for physical-reclamation ordering and grace.

**Spec:** `docs/superpowers/specs/2026-05-22-fu5-last-referenced-lru-design.md` (read fully — §0 column/bump-sites/ordering, §1 honest limits, §2 tests).

**Locked constraints:**
- Mirror the existing `StorageObject.last_referenced_at` exactly: `DateTime(timezone=True), server_default=func.now(), nullable=False`.
- ALTER existing table + NOT NULL → migration `server_default=sa.func.now()` matching the model (no drift) + `UPDATE ... SET last_referenced_at = created_at` backfill.
- Grace switch is strictly safer (`last_referenced_at >= created_at` → deletes later, never sooner).
- Additive only: no openapi/frontend/executor-protocol change. Same `gc_delete_physical_bytes` gate, cap, `~live`, FU4 widening, audit.
- `EXPECTED_TABLES` unchanged (column add). CI gate = pytest + `lint_invariants`; `ruff --select I001 --fix` touched files.

---

## File Structure
- **Modify** `src/dlw/db/models/storage_object.py` (`StoragePhysicalKey.last_referenced_at`).
- **Create** `src/dlw/alembic/versions/e6f7a8b9c0d1_fu5_phys_key_last_referenced.py`.
- **Modify** `src/dlw/services/storage_objects.py` (`record_physical_key` upsert, `record_ref_only` touch, `reclaim_physical_orphans`/`dispatch_local_reclaim`/`count_stuck_local_orphans` ordering+grace).
- **Modify** `tests/services/test_physical_reclaim.py`, `tests/services/test_physical_keys.py`, `tests/services/test_local_reclaim.py`, `tests/db/test_alembic.py`.
- **Modify** `docs/operator/storage-reclamation.md`.

---

## Milestone M1 — migration + model column

### Task 1: model column + migration + alembic test
**Files:** `src/dlw/db/models/storage_object.py`, new `src/dlw/alembic/versions/e6f7a8b9c0d1_fu5_phys_key_last_referenced.py`, `tests/db/test_alembic.py`.

- [ ] **Step 1 (model):** in `StoragePhysicalKey`, add immediately before `created_at`:
```python
    last_referenced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
```
- [ ] **Step 2 (migration):** create `src/dlw/alembic/versions/e6f7a8b9c0d1_fu5_phys_key_last_referenced.py`:
```python
"""fu5 phys key last_referenced_at

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-05-22
"""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "e6f7a8b9c0d1"
down_revision: str | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("storage_physical_keys", sa.Column(
        "last_referenced_at", sa.DateTime(timezone=True),
        server_default=sa.func.now(), nullable=False))
    op.execute(
        "UPDATE storage_physical_keys SET last_referenced_at = created_at")


def downgrade() -> None:
    op.drop_column("storage_physical_keys", "last_referenced_at")
```
- [ ] **Step 3 (alembic test):** in `tests/db/test_alembic.py`, add a focused test after `test_upgrade_head_creates_all_tables` (use the same engine/inspect idiom already in the file):
```python
@pytest.mark.asyncio
async def test_phys_key_has_last_referenced_at(...):
    # after upgrade head, inspect storage_physical_keys columns
    cols = {c["name"] for c in inspector.get_columns("storage_physical_keys")}
    assert "last_referenced_at" in cols
```
Match the file's existing async-inspect helper exactly (read it first; reuse its engine fixture / `run_sync(inspect)` pattern — do NOT invent a new connection).
- [ ] **Step 4: dev-DB upgrade** — `cd "D:/download_weights" && uv run alembic -c alembic.ini upgrade head` against PG `:5433`; expect "Running upgrade d5e6f7a8b9c0 -> e6f7a8b9c0d1". Then `uv run alembic -c alembic.ini check` (or autogenerate) → **no drift**.
- [ ] **Step 5: verify** — `cd "D:/download_weights" && uv run pytest tests/db -q` → all pass (esp. `test_alembic`).
- [ ] **Step 6: tidy + commit.**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/db/models/storage_object.py src/dlw/alembic/versions/e6f7a8b9c0d1_fu5_phys_key_last_referenced.py tests/db/test_alembic.py
git add src/dlw/db/models/storage_object.py src/dlw/alembic/versions/e6f7a8b9c0d1_fu5_phys_key_last_referenced.py tests/db/test_alembic.py && git commit -m "feat(fu5): add last_referenced_at column to storage_physical_keys"
```

### Task 2: M1 backend gate
- [ ] `cd "D:/download_weights" && uv run pytest -q` all pass (failover flake = Windows-local, isolate-confirm if it appears); `uv run python -m dlw.tools.lint_invariants --strict` OK. No commit.

---

## Milestone M2 — bump sites + ordering/grace switch

### Task 3: bump on (re)reference + LRU ordering/grace
**Files:** `src/dlw/services/storage_objects.py`; `tests/services/test_physical_keys.py`, `tests/services/test_physical_reclaim.py`, `tests/services/test_local_reclaim.py`.

- [ ] **Step 1 (failing tests — write path):** in `tests/services/test_physical_keys.py` add per spec §2:
  - `test_record_physical_key_bumps_last_referenced_on_conflict`: insert a `StoragePhysicalKey` with `last_referenced_at=_old(...)`, `executor_id="ex1"`, `size=7`; `await record_physical_key(session, tenant_id=, storage_id=, sha256=, storage_key=<same>, size=99, executor_id="ex2")`; commit; reload → `last_referenced_at` advanced (> the old value), `executor_id == "ex1"` and `size == 7` UNCHANGED (conflict refreshes only the timestamp).
  - `test_record_ref_only_touches_physical_keys_for_sha`: seed a `StoragePhysicalKey` (`last_referenced_at=_old(...)`, sha=S) + a `StorageObject` (sha=S, refcount=1) + a `FileSubTask` row to satisfy the `subtask_object_refs` FK; `await record_ref_only(session, tenant_id=, storage_id=, storage_key=, sha256=S, size=, subtask_id=<that subtask>)`; commit; reload the physical key → `last_referenced_at` advanced. (Reuse the file's existing seed helpers / fixtures for tenant/storage/subtask — read the file first.)
- [ ] **Step 2 (failing tests — reclaim ordering/grace):** in `tests/services/test_physical_reclaim.py`:
  - First, **update every existing `StoragePhysicalKey(...)` construction** in the file to also pass `last_referenced_at=` equal to its `created_at` value (so `test_grace_respected`, `test_per_tick_cap`, `test_priority_tenant_reclaimed_first_under_cap` stay candidates / keep their relative order).
  - Add `test_orders_by_last_referenced_not_created`: key A `created_at=_old(10*86400)`, `last_referenced_at=_old(3600)`; key B `created_at=_old(2*86400)`, `last_referenced_at=_old(9*86400)`; same tenant, no priority, `delete_enabled=True`, `max_objects_per_tick=1`, both `~live`. Assert the single reclaimed key is **B** (older last reference). (A fake `make_client` returning an s3 client whose delete succeeds; mirror the existing test's client/audit doubles.)
  - Add `test_grace_uses_last_referenced_at`: key `created_at=_old(30*86400)`, `last_referenced_at=datetime.now(UTC)`, `~live`, `grace_seconds=3600`. Assert `candidates == 0`.
- [ ] **Step 3 (failing tests — local dispatch):** in `tests/services/test_local_reclaim.py`: update every `StoragePhysicalKey(...)` construction to also set `last_referenced_at=_old(...)` (matching its `created_at`). Add `test_dispatch_orders_by_last_referenced`: two local orphan keys (writer-scoped to the same executor, both `~live`, backend local with base_path), one `last_referenced_at=_old(9d)` the other `_old(1d)`; `dispatch_local_reclaim(session, "ex", grace_seconds=3600, limit=1)` returns the older-last-referenced key.
- [ ] **Step 4: verify FAIL** — `cd "D:/download_weights" && uv run pytest tests/services/test_physical_keys.py tests/services/test_physical_reclaim.py tests/services/test_local_reclaim.py -q` → new tests fail (column-bump / ordering not implemented), and the *updated* existing tests may already pass (they just gained an explicit `last_referenced_at`).
- [ ] **Step 5 (record_physical_key upsert):** in `src/dlw/services/storage_objects.py`, change `record_physical_key` from `on_conflict_do_nothing` to:
```python
    await session.execute(pg_insert(StoragePhysicalKey).values(
        tenant_id=tenant_id, storage_id=storage_id, sha256=sha256,
        storage_key=storage_key, size=size,
        executor_id=executor_id).on_conflict_do_update(
            index_elements=["tenant_id", "storage_id", "storage_key"],
            set_={"last_referenced_at": datetime.now(UTC)}))
```
(`datetime`/`UTC` already imported at module top.)
- [ ] **Step 6 (record_ref_only touch):** at the END of `record_ref_only` (after the `SubtaskObjectRef` insert, before `return oid`), add:
```python
    await session.execute(
        update(StoragePhysicalKey)
        .where(StoragePhysicalKey.tenant_id == tenant_id,
               StoragePhysicalKey.storage_id == storage_id,
               StoragePhysicalKey.sha256 == sha256)
        .values(last_referenced_at=datetime.now(UTC)))
```
(`update` already imported from sqlalchemy at module top.)
- [ ] **Step 7 (ordering/grace switch):** replace `created_at` with `last_referenced_at` ONLY where used as ordering key / grace cutoff:
  - `reclaim_physical_orphans`: lines ~139-144 — both `.where(StoragePhysicalKey.last_referenced_at < cutoff, ~live)` and `.order_by(prio, StoragePhysicalKey.last_referenced_at)` / `.order_by(StoragePhysicalKey.last_referenced_at)`.
  - `dispatch_local_reclaim`: in `where`, `StoragePhysicalKey.last_referenced_at < cutoff`; `.order_by(StoragePhysicalKey.last_referenced_at)`.
  - `count_stuck_local_orphans`: `StoragePhysicalKey.last_referenced_at < cutoff`.
  - Do NOT touch `gc_orphans` (operates on `StorageObject`, out of scope).
- [ ] **Step 8: verify PASS** — `cd "D:/download_weights" && uv run pytest tests/services/test_physical_keys.py tests/services/test_physical_reclaim.py tests/services/test_local_reclaim.py -v` → all pass.
- [ ] **Step 9: tidy + commit.**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/services/storage_objects.py tests/services/test_physical_keys.py tests/services/test_physical_reclaim.py tests/services/test_local_reclaim.py
git add src/dlw/services/storage_objects.py tests/services/test_physical_keys.py tests/services/test_physical_reclaim.py tests/services/test_local_reclaim.py && git commit -m "feat(fu5): bump last_referenced_at on reference + use it as LRU ordering/grace key"
```

### Task 4: M2 backend gate
- [ ] `cd "D:/download_weights" && uv run pytest -q` all pass; `lint_invariants --strict` OK. No commit.

---

## Milestone M3 — docs

### Task 5: docs
**File:** `docs/operator/storage-reclamation.md`.

- [ ] **Step 1:** in the "Quota-pressure prioritization" section, replace the proxy bullet (currently: "`created_at` (ledger-write time) is the available proxy for the design's `last_referenced_at`; a true per-key last-access column is a follow-on.") with prose stating: physical keys now carry a real `last_referenced_at`, bumped whenever the content is (re)referenced (a re-download to the same key, or a later revision inheriting the same content via dedup); reclamation now orders orphans **least-recently-referenced first** and measures the grace window **from the last reference, not the ledger write**. Note the grace change is strictly more conservative (`last_referenced_at >= created_at`, so bytes are deleted no sooner than before). Keep the honest caveats: the signal only diverges from write-time for content re-referenced during its live lifetime; ordering only has observable effect when the per-tick cap binds; the *tracked* refcount-0 `storage_objects` eviction (`gc_orphans`) remains time-only (FU5 sharpens only physical-orphan ordering/grace). Prose, not a bullet dump.
- [ ] **Step 2: commit.**
```bash
cd "D:/download_weights" && git add docs/operator/storage-reclamation.md && git commit -m "docs(fu5): physical reclamation uses true last_referenced_at LRU"
```

---

## Self-Review
- **Spec coverage:** §0 column → Task 1 ✓; §0 bump sites → Task 3 Steps 5-6 ✓; §0 ordering/grace → Task 3 Step 7 ✓; §2 tests → Tasks 1,3 ✓; §3 milestones → M1/M2/M3 ✓.
- **Placeholder scan:** Task 1 Step 3's alembic test references "the file's existing async-inspect helper" — implementer must read `tests/db/test_alembic.py` and reuse its real fixture (the one judgment point; the column-presence assertion itself is concrete).
- **Type consistency:** `last_referenced_at: Mapped[datetime]` (model) / `sa.DateTime(timezone=True)` (migration) / `last_referenced_at < cutoff` & `.order_by(... last_referenced_at)` (service). Consistent with the existing `StorageObject.last_referenced_at`.
- **Open risks for reviewers:** (a) grace switch is strictly safer but a behavior change — is the doc framing honest? (b) `record_ref_only` touch adds one UPDATE on the inherit hot path (per-sha, hits `idx_phys_key_gc`) — acceptable? (c) existing reclaim/local tests must gain explicit `last_referenced_at` or they fall inside the new grace — Task 3 Steps 2-3 enumerate this; (d) `on_conflict_do_update` must NOT clobber `executor_id`/`size` (FU3/FU4 rely on writer `executor_id`) — set_ contains only `last_referenced_at`, asserted by the conflict test.
