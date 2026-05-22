# FU2 — Phase 4 Quota-Trigger LRU Ordering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make the Phase-4 physical-reclamation GC reclaim space-pressured tenants' dereferenced physical keys first (under the per-tick cap), wiring the deferred §3.2 quota-trigger.

**Architecture:** A `pressured_tenant_ids` query (QuotaSnapshot.storage_gb_used ≥ threshold × Tenant.quota_storage_gb) + a `priority_tenant_ids` `order_by` (`case`) in `reclaim_physical_orphans`; the leader-gated `_physical_gc_loop` computes + passes it.

**Spec:** `docs/superpowers/specs/2026-05-22-fu2-quota-trigger-lru-design.md` (read fully — the §0 "priority heuristic, doesn't reduce storage_gb_used" + the `created_at`-proxies-LRU + the empty-pressure-is-byte-identical notes).

**Locked constraints:**
- Pure addition: when `priority_tenant_ids` is empty, the candidate query MUST be byte-identical to today (guard the `case`). Existing `test_physical_reclaim.py` tests stay green unchanged.
- refcount=0-only eligibility, grace, S3-only, default-off `gc_delete_physical_bytes`, delete-bytes-before-row, audit, max_objects_per_tick — all UNCHANGED.
- No migration, no new dep, no API/openapi/frontend change. CI gate = pytest + `lint_invariants`; `ruff --select I001 --fix` new files only.

---

## File Structure
- **Modify** `src/dlw/services/storage_objects.py` — `pressured_tenant_ids` + `reclaim_physical_orphans` priority param + `case` import.
- **Modify** `src/dlw/config.py` — `gc_quota_pressure_threshold`.
- **Modify** `src/dlw/main.py` — `_physical_gc_loop` computes + passes priority.
- **Create** `tests/services/test_quota_pressure.py`; **Modify** `tests/services/test_physical_reclaim.py`.
- **Modify** `docs/operator/storage-reclamation.md`.

---

## Milestone M1 — detection + priority ordering

### Task 1: `pressured_tenant_ids` + reclaim priority param

**Files:** Modify `src/dlw/services/storage_objects.py`; Create `tests/services/test_quota_pressure.py`; Modify `tests/services/test_physical_reclaim.py`.

- [ ] **Step 1: failing tests.** Create `tests/services/test_quota_pressure.py` (mirror the DB-fixture style of `test_physical_reclaim.py` — `engine`/module-bootstrap/`session`):
```python
"""FU2 quota-pressure detection."""
from __future__ import annotations
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from dlw.db.base import Base
from dlw.services.storage_objects import pressured_tenant_ids


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.tenant import Tenant
    from dlw.db.models.usage import QuotaSnapshot
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
        await c.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=1, slug="t1", display_name="T1", quota_storage_gb=10))
        s.add(Tenant(id=2, slug="t2", display_name="T2", quota_storage_gb=10))
        s.add(Tenant(id=3, slug="t3", display_name="T3", quota_storage_gb=0))
        await s.flush()
        s.add(QuotaSnapshot(tenant_id=1, storage_gb_used=10))   # 100% → pressured
        s.add(QuotaSnapshot(tenant_id=2, storage_gb_used=5))    # 50%  → not
        s.add(QuotaSnapshot(tenant_id=3, storage_gb_used=999))  # quota 0 → not
        await s.commit()
    yield
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def session(engine):
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        yield s


async def test_pressured_set(session):
    ids = await pressured_tenant_ids(session, threshold=0.9)
    assert ids == frozenset({1})
```
And extend `tests/services/test_physical_reclaim.py` with a priority-ordering test (reuse its `_FakeS3`/`session`/bootstrap; the bootstrap seeds StorageBackend id=1 s3):
```python
async def test_priority_tenant_reclaimed_first_under_cap(session):
    from dlw.db.models.storage_object import StoragePhysicalKey
    fake = _FakeS3()
    # two old, fully-dereferenced keys (no storage_objects rows): tenant 1 (priority) + tenant 1-other.
    # Use distinct tenants if the bootstrap seeds >1; else distinguish by key. The bootstrap seeds tenant 1.
    # Seed a second tenant + backend in THIS test if needed, OR key priority off tenant_id present.
    session.add(StoragePhysicalKey(tenant_id=1, storage_id=1, sha256="p"*64,
                                   storage_key="repo/prio/x", size=1, created_at=_old(5)))
    session.add(StoragePhysicalKey(tenant_id=1, storage_id=1, sha256="q"*64,
                                   storage_key="repo/other/y", size=1, created_at=_old(1)))
    await session.commit()
    res = await reclaim_physical_orphans(
        session, grace_seconds=3600, delete_enabled=True,
        make_client=lambda sid: (fake, "bkt", "s3"), audit=lambda **k: None,
        max_objects_per_tick=1, priority_tenant_ids=frozenset({1}))
    await session.commit()
    assert res["deleted"] == 1   # cap=1; a tenant-1 key reclaimed
```
(NOTE: the priority test needs TWO tenants to meaningfully prove "priority wins" — if `test_physical_reclaim.py`'s bootstrap only seeds tenant 1, either (a) add a second tenant + its backend in the bootstrap, or (b) write the priority assertion to seed a priority tenant key with an OLDER `created_at`-disadvantage so that without priority the OTHER key would be chosen — i.e. give the priority tenant the NEWER key, the non-priority the OLDER key, cap=1, and assert the PRIORITY (newer) key is the one deleted, proving priority overrode created_at. Implementer: read the bootstrap, pick the variant that cleanly proves priority overrides oldest-first. The assertion must distinguish priority-wins from oldest-first-wins.)

- [ ] **Step 2: verify FAIL** (`pressured_tenant_ids` missing; priority param missing).

- [ ] **Step 3: implement** in `src/dlw/services/storage_objects.py` per spec §2: add `from sqlalchemy import case` to imports; add `pressured_tenant_ids`; add `priority_tenant_ids=frozenset()` param to `reclaim_physical_orphans` + the guarded `case` order_by (empty → byte-identical single-order path).

- [ ] **Step 4: verify PASS** + full reclaim regression: `cd "D:/download_weights" && uv run pytest tests/services/test_quota_pressure.py tests/services/test_physical_reclaim.py -v` → all pass (the existing reclaim tests, which pass no `priority_tenant_ids`, stay green).

- [ ] **Step 5: config.** In `src/dlw/config.py`, after the other `gc_*` fields: `gc_quota_pressure_threshold: float = Field(default=0.9, gt=0.0, le=1.0)`.

- [ ] **Step 6: tidy + commit.**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/services/storage_objects.py src/dlw/config.py tests/services/test_quota_pressure.py tests/services/test_physical_reclaim.py
git add src/dlw/services/storage_objects.py src/dlw/config.py tests/services/test_quota_pressure.py tests/services/test_physical_reclaim.py && git commit -m "feat(fu2): pressured_tenant_ids + quota-priority reclaim ordering"
```

### Task 2: M1 backend gate
- [ ] `cd "D:/download_weights" && uv run pytest -q` → all pass (failover flake = Windows-local; isolate-confirm if it appears). `python tools/lint_invariants.py --strict` → OK. No commit.

---

## Milestone M2 — loop wiring + docs

### Task 3: `_physical_gc_loop` wiring + docs

**Files:** Modify `src/dlw/main.py`, `docs/operator/storage-reclamation.md`; maybe `tests/test_phase4_lifespan.py`.

- [ ] **Step 1: wire** in `src/dlw/main.py` `_physical_gc_loop` — inside the `async with factory() as session:` block, after loading backends and BEFORE the `reclaim_physical_orphans` call, add:
```python
                    from dlw.services.storage_objects import pressured_tenant_ids
                    priority = await pressured_tenant_ids(
                        session, threshold=_gs().gc_quota_pressure_threshold)
```
and pass `priority_tenant_ids=priority` into the existing `reclaim_physical_orphans(...)` call.

- [ ] **Step 2: smoke + lifespan.** `cd "D:/download_weights" && uv run python -c "import dlw.main; print('ok')"` → ok. Run `tests/test_phase4_lifespan.py` (extend it to assert the new config default `gc_quota_pressure_threshold == 0.9` if it already asserts other gc config defaults) → pass.

- [ ] **Step 3: docs.** In `docs/operator/storage-reclamation.md`, add a short note under the reclamation section: under the per-tick cap, tenants at/over `gc_quota_pressure_threshold` (default 0.9) of their storage quota have their dereferenced keys reclaimed first; ordering within a group is oldest-first (`created_at`, the available proxy for §3.2's `last_referenced_at`); note this is a priority heuristic that does not itself lower `storage_gb_used` (orphan bytes are already untracked). Prose.

- [ ] **Step 4: full backend gate + commit.**
```bash
cd "D:/download_weights" && uv run pytest -q   # all pass
cd "D:/download_weights" && python tools/lint_invariants.py --strict   # OK
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/main.py tests/test_phase4_lifespan.py
git add src/dlw/main.py docs/operator/storage-reclamation.md tests/test_phase4_lifespan.py && git commit -m "feat(fu2): wire quota-pressure priority into _physical_gc_loop + docs"
```

---

## Self-Review
- **Spec coverage:** §1.1 detection → Task 1 ✓; §1.2 priority order → Task 1 ✓; §1.3 loop wiring → Task 3 ✓; §1.4 config → Task 1 ✓; §3 tests → Tasks 1,3 ✓.
- **Placeholder scan:** Task 1 Step 1's priority-test note gives a concrete "make the assertion distinguish priority-wins from oldest-first-wins" outcome (newer-priority vs older-non-priority + cap=1) — an implementer-judgment point with a specified proof obligation, not a TODO.
- **Type consistency:** `pressured_tenant_ids(session, *, threshold) -> frozenset[int]`; `reclaim_physical_orphans(..., priority_tenant_ids=frozenset())`; `gc_quota_pressure_threshold: float`. Consistent.
- **Open risks for reviewers:** (a) does the existing `test_physical_reclaim.py` bootstrap seed a second tenant/backend, or must the priority test seed its own? (b) `case((col.in_(frozenset), 0), else_=1)` — does SQLAlchemy accept a `frozenset` in `in_` + compile the `case` order_by on PG? (c) is the empty-`priority_tenant_ids` guard truly byte-identical to the current order_by (so existing tests don't shift)? (d) does `QuotaSnapshot.storage_gb_used >= threshold * Tenant.quota_storage_gb` (float×int in SQL) compare correctly across the join?
