# FU3 — Local-FS Physical Reclamation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or executing-plans. Checkbox (`- [ ]`) steps.

**Goal:** Reclaim orphaned local-filesystem bytes by dispatching each orphaned key to the executor that WROTE it (over the existing 10 s heartbeat); the executor `unlink`s it and confirms. Closes Phase 4's local-fs deferral. Safe-by-default (same `gc_delete_physical_bytes` gate).

**Architecture:** Record the writer `executor_id` on `storage_physical_keys`; the heartbeat handler confirms reclaimed ids + dispatches this executor's local orphans (writer-targeted, grace, `~live`); the executor `unlink`s (missing=ok) + confirms via the next heartbeat. The S3 controller loop is unchanged.

**Spec:** `docs/superpowers/specs/2026-05-22-fu3-localfs-reclamation-design.md` (read fully — §0 routing/single-executor rationale, the honesty/deferral notes, the full code for every fn).

**Locked constraints:**
- Destructive — same safe-by-default rails as Phase 4: OFF unless `gc_delete_physical_bytes`; grace = `max(gc_grace_seconds, gc_archive_after_days*86400)`; `~live` (sha fully dereferenced, no surviving `storage_objects` row); **writer-targeted** (`executor_id == this executor`); `unlink(missing_ok=True)` idempotent; audited `storage.gc.physical.local`; capped at `gc_max_objects_per_tick`; confirm-then-delete-ledger ordering (crash → re-dispatch).
- Heartbeat protocol additions are backward-compatible (default-empty; old executor omits `reclaimed_key_ids` → handler defaults `[]`). The HMAC signs the new body bytes (executor builds + signs).
- Migration = nullable column add (`executor_id`) on `storage_physical_keys`; down_revision `c4d5e6f7a8b9`; no server_default (nullable → no drift); `EXPECTED_TABLES` unchanged; dev-DB upgrade + drift gate.
- The S3 `reclaim_physical_orphans` loop is UNCHANGED. No openapi/frontend change. CI gate = pytest + `lint_invariants`; `ruff --select I001 --fix` new files.

---

## File Structure
- **Create** migration `src/dlw/alembic/versions/<rev>_fu3_phys_key_executor.py`.
- **Modify** `src/dlw/db/models/storage_object.py` (`StoragePhysicalKey.executor_id`), `src/dlw/services/storage_objects.py` (`record_physical_key` executor_id + `dispatch_local_reclaim` + `confirm_local_reclaim`), `src/dlw/services/scheduler.py` (pass `sub.executor_id`).
- **Modify** `src/dlw/schemas/executor.py` (`ExecutorHeartbeat.reclaimed_key_ids`, `ExecutorRead.reclaim`, `ReclaimItem`), `src/dlw/api/executors.py` (heartbeat handler confirm/dispatch).
- **Modify** `src/dlw/executor/client.py` (`heartbeat` reclaimed_key_ids), `src/dlw/executor/runner.py` (`_heartbeat_loop` unlink+confirm).
- **Create** `tests/services/test_local_reclaim.py`; **Modify** `tests/api/test_executors.py`, `tests/executor/test_runner.py`, `tests/services/test_physical_keys.py`, `tests/db/test_alembic.py` (no-op verify).
- **Modify** `docs/operator/storage-reclamation.md`.

---

## Milestone M1 — ledger column + dispatch/confirm service

### Task 1: migration + model + record executor_id
**Files:** migration (create), `storage_object.py`, `storage_objects.py` (`record_physical_key`), `scheduler.py`, `tests/services/test_physical_keys.py`.

- [ ] **Step 1 (failing test):** in `tests/services/test_physical_keys.py`, add a test that `record_physical_key(..., executor_id="ex-9")` stores `executor_id` (query the row back). Verify FAIL (param/column missing). ALSO confirm the `complete_subtask`→`record_physical_key` wiring records the writer: `tests/services/test_record_object_on_complete.py` already drives `complete_subtask(session, executor_id=..., ...)` success — extend (or add) an assertion that the resulting `storage_physical_keys` row's `executor_id` equals the completing executor (covers both the download and the inherit success path, since both flow through the same `if final_status=="succeeded" and sub.s3_key and sub.actual_sha256:` block where `record_physical_key` is called).
- [ ] **Step 2: model** — `storage_object.py` `StoragePhysicalKey`: add `executor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)`.
- [ ] **Step 3: migration** — create `..._fu3_phys_key_executor.py` (revision e.g. `d5e6f7a8b9c0`, down_revision `c4d5e6f7a8b9`): `op.add_column("storage_physical_keys", sa.Column("executor_id", sa.String(64), nullable=True))` + `downgrade` drops it.
- [ ] **Step 4: record + wire** — `record_physical_key(..., executor_id: str | None = None)` includes `executor_id` in the `pg_insert(...).values(...)`. In `scheduler.py::complete_subtask`, the `record_physical_key(...)` call adds `executor_id=sub.executor_id`.
- [ ] **Step 5: apply + verify** — `uv run alembic -c alembic.ini upgrade head`; `uv run pytest tests/db/test_alembic.py tests/services/test_physical_keys.py -v` → pass (EXPECTED_TABLES unchanged; new col present).
- [ ] **Step 6: BLOCKING drift gate** — `uv run alembic -c alembic.ini revision --autogenerate -m _drift` → generated `upgrade()` empty w.r.t. `storage_physical_keys.executor_id`; delete the drift file; if drift, reconcile (nullable, no server_default).
- [ ] **Step 7: commit** (migration + model + service + scheduler + test):
```bash
cd "D:/download_weights" && git add src/dlw/alembic/versions/*_fu3_phys_key_executor.py src/dlw/db/models/storage_object.py src/dlw/services/storage_objects.py src/dlw/services/scheduler.py tests/services/test_physical_keys.py && git commit -m "feat(fu3): record writer executor_id on storage_physical_keys"
```

### Task 2: `dispatch_local_reclaim` + `confirm_local_reclaim`
**Files:** `storage_objects.py`; `tests/services/test_local_reclaim.py`.

- [ ] **Step 1 (failing tests):** create `tests/services/test_local_reclaim.py` (mirror `test_physical_reclaim.py` bootstrap — seed Tenant + a `backend_type="local"` StorageBackend whose `config_encrypted` JSON MUST carry `base_path` (e.g. `b'{"base_path": "/srv/dlw"}'`) — without it `dispatch_local_reclaim` skips the key and the test asserts nothing, + an s3 backend). `dispatch_local_reclaim` returns `list[ReclaimItem]` (assert `.base_path`/`.storage_key`/`.id` attrs, not dict keys). Tests:
  - `dispatch_local_reclaim(session, "ex-1", grace_seconds=3600, limit=10)` returns only ex-1's LOCAL orphan keys past grace, each `{id, base_path, storage_key}` with base_path from config; excludes: s3-backend keys, other executors' keys, non-orphan (a key whose sha still has a `storage_objects` row), within-grace (fresh `created_at`).
  - `confirm_local_reclaim(session, "ex-1", [ids], audit=rec)` deletes only rows scoped to ex-1 + returns count; a wrong-executor id is NOT deleted.
  (Seed `StoragePhysicalKey` rows with `executor_id`, `created_at=_old()`; reuse the `_old`/`session` helpers.)
- [ ] **Step 2: verify FAIL.**
- [ ] **Step 3: implement** `dispatch_local_reclaim` + `confirm_local_reclaim` in `storage_objects.py` per spec §3 (join `StorageBackend` on storage_id where `backend_type=="local"`, `~live`, `created_at<cutoff`, `executor_id==`, `order_by(created_at).limit().with_for_update(skip_locked=True, of=StoragePhysicalKey)`; resolve base_path via `storage_config_from_backend`; confirm deletes scoped rows + audits).
- [ ] **Step 4: verify PASS** + reclaim regression: `uv run pytest tests/services/test_local_reclaim.py tests/services/test_physical_reclaim.py -v` → all pass (the S3 loop is untouched).
- [ ] **Step 5: tidy + commit:**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/services/storage_objects.py tests/services/test_local_reclaim.py
git add src/dlw/services/storage_objects.py tests/services/test_local_reclaim.py && git commit -m "feat(fu3): dispatch_local_reclaim + confirm_local_reclaim"
```

### Task 3: M1 backend gate
- [ ] `uv run pytest -q` all pass; `python tools/lint_invariants.py --strict` OK; `uv run alembic -c alembic.ini current` → new head. No commit.

---

## Milestone M2 — heartbeat protocol + handler

### Task 4: schema + heartbeat handler confirm/dispatch
**Files:** `schemas/executor.py`, `api/executors.py`; `tests/api/test_executors.py`.

- [ ] **Step 1 (failing API tests):** in `tests/api/test_executors.py` (reuse `register_test_executor`/`signed_heartbeat_headers`/`make_app_with_state`), add:
  - heartbeat with `reclaimed_key_ids=[id]` (a seeded local key owned by this executor) → the row is deleted; a row owned by ANOTHER executor in the list is NOT deleted.
  - with `gc_delete_physical_bytes` enabled (monkeypatch/env) + a seeded local orphan for this executor past grace → heartbeat response `reclaim` has one item `{id, base_path, storage_key}`; with it disabled → `reclaim == []`.
  (Set the gc config via the settings/env the other tests use; seed via `db_session`+commit.)
- [ ] **Step 2: verify FAIL.**
- [ ] **Step 3: schema** — `schemas/executor.py`: add `class ReclaimItem(BaseModel): id:int; base_path:str; storage_key:str`; `ExecutorHeartbeat.reclaimed_key_ids: list[int] = Field(default_factory=list)`; `ExecutorRead.reclaim: list[ReclaimItem] = Field(default_factory=list)`.
- [ ] **Step 4: handler** — `api/executors.py::post_heartbeat`: after `ex = await record_heartbeat(...)`, run confirm (if `body.reclaimed_key_ids`) + dispatch (if enabled), commit, return `ExecutorRead.model_validate(ex).model_copy(update={"reclaim": reclaim})`. Use the EXACT corrected code in spec §3 (pre-review): import `from dlw.config import get_settings` (NOT `_gs` — that alias is main.py-local) and use `get_settings().gc_*`; import `write_audit`; build an async `_audit(*, action, id, tenant_id, storage_key, size)` ADAPTER that calls `write_audit(session, action=, resource_type="storage_physical_keys", resource_id=str(id), outcome="success", tenant_id=, actor_user_id=None, payload={"storage_key":..., "size":...})` (the bare `audit(id=, storage_key=, size=)` kwargs do NOT match `write_audit`'s signature); pass `_audit` to `confirm_local_reclaim`. `dispatch_local_reclaim` returns `list[ReclaimItem]` (so `model_copy` serializes nested models — no pydantic dict-coercion warning).
  - **Observability (pre-review):** in the dispatch path (or right after), count local phys keys that are `~live` + past grace but NOT dispatchable (`executor_id IS NULL` OR points at a deactivated/absent executor) and `logger.info` the gauge so accruing un-reclaimable bytes are visible. Keep it a single cheap COUNT query; gate behind the same enabled-check or run it unconditionally at low frequency.
- [ ] **Step 5: verify PASS** + executor API regression: `uv run pytest tests/api/test_executors.py -v` → all pass (existing register/heartbeat/poll tests unaffected — new fields default-empty).
- [ ] **Step 6: tidy + commit:**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/schemas/executor.py src/dlw/api/executors.py tests/api/test_executors.py
git add src/dlw/schemas/executor.py src/dlw/api/executors.py tests/api/test_executors.py && git commit -m "feat(fu3): heartbeat confirm/dispatch local reclaim"
```

### Task 5: M2 backend gate
- [ ] `uv run pytest -q` all pass; `lint_invariants --strict` OK. No commit.

---

## Milestone M3 — executor side + docs

### Task 6: executor unlink + confirm + docs
**Files:** `executor/client.py`, `executor/runner.py`; `tests/executor/test_runner.py`; `docs/operator/storage-reclamation.md`.

- [ ] **Step 1 (failing runner test):** in `tests/executor/test_runner.py` (mirror the `MagicMock(spec=ControllerClient)` + `runner.run()`/`request_shutdown()` pattern), add: a mock `heartbeat` whose return carries `reclaim=[{id:7, base_path:<tmp abs dir>, storage_key:"k"}]` for one tmp file that exists → assert the runner `unlink`s it AND the NEXT heartbeat call passes `reclaimed_key_ids=[7]`. A missing-file variant → still confirmed. AND a **path-traversal-refused** test (pre-review BLOCKER): `reclaim=[{id:9, base_path:<tmp abs dir>, storage_key:"../../etc/x"}]` (or an absolute `/abs`) → the runner does NOT unlink anything outside base_path AND does NOT confirm id 9 (it's absent from the next `reclaimed_key_ids`), and a sentinel file outside base_path is untouched. Also an empty/relative `base_path` → refused (not confirmed). (Inspect `heartbeat`'s call kwargs across iterations.)
- [ ] **Step 2: verify FAIL.**
- [ ] **Step 3: client** — `executor/client.py::heartbeat(..., reclaimed_key_ids: list[int] | None = None)`: add to `body_dict` when non-empty (before signing). Return value unchanged (the dict already returned).
- [ ] **Step 4: runner** — `executor/runner.py`: add `self._pending_reclaim_confirms: list[int] = []` (init in `__init__`). In `_heartbeat_loop`, pass `reclaimed_key_ids=self._pending_reclaim_confirms` to `heartbeat(...)`; capture the response dict; for each `resp.get("reclaim") or []` item, apply the **`_safe_target(base_path, storage_key)` path-traversal guard from spec §4** (reject empty/relative base_path; reject any target that, after `.resolve()`, is not `.is_relative_to(base.resolve())` — `../..`/absolute escapes); if the guard returns None → `logger.warning` + `continue` (do NOT confirm, so the controller never deletes the ledger row for an unsafe key); else `target.unlink(missing_ok=True)` in try/except OSError (success/missing → collect id; other OSError → warn, skip); set `self._pending_reclaim_confirms = confirmed`. (Best-effort; never crash the loop. `Path` is already imported in runner.py.)
- [ ] **Step 5: verify PASS** + executor regression: `uv run pytest tests/executor/ -v` → all pass.
- [ ] **Step 6: docs** — `docs/operator/storage-reclamation.md`: add that local-fs orphan reclamation now happens via the WRITER executor over its heartbeat (the controller hands it the keys + base_path; it `unlink`s and confirms); it is gated by the SAME `gc_delete_physical_bytes` flag, grace, and refcount=0 eligibility as S3; it targets the single executor that wrote the file (single-executor-local mode); deferred: an NFS-shared backend whose writer executor is offline (waits for the writer's return), and keys recorded before this shipped (no `executor_id` → not dispatched). Update the "skipped for local" note to "local handled via executor heartbeat".
- [ ] **Step 7: full backend gate + commit:**
```bash
cd "D:/download_weights" && uv run pytest -q   # all pass
cd "D:/download_weights" && python tools/lint_invariants.py --strict   # OK
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/executor/client.py src/dlw/executor/runner.py tests/executor/test_runner.py
git add src/dlw/executor/client.py src/dlw/executor/runner.py tests/executor/test_runner.py docs/operator/storage-reclamation.md && git commit -m "feat(fu3): executor unlinks local reclaim keys + confirms; docs"
```

---

## Self-Review
- **Spec coverage:** §1 migration/record → Task 1 ✓; §3 dispatch/confirm → Task 2 ✓; heartbeat schema+handler → Task 4 ✓; executor → Task 6 ✓; §5 tests → Tasks 1,2,4,6 ✓; §6 milestones → M1/M2/M3 ✓.
- **Placeholder scan:** the audit-inline-vs-collect choice + the optional `to_thread` are implementer-judgment with specified intent, not TODOs. Migration revision id is illustrative (`d5e6f7a8b9c0`) — implementer picks any unused id chaining off `c4d5e6f7a8b9`.
- **Type consistency:** `record_physical_key(..., executor_id=None)`; `dispatch_local_reclaim(session, executor_id, *, grace_seconds, limit) -> list[dict]`; `confirm_local_reclaim(session, executor_id, key_ids, *, audit) -> int`; `ReclaimItem{id,base_path,storage_key}`; `ExecutorHeartbeat.reclaimed_key_ids`; `ExecutorRead.reclaim`. Consistent.
- **Open risks for reviewers:** (a) does adding a field to `ExecutorHeartbeat` (the HMAC-signed body) break existing heartbeat tests that build the body without it (the field defaults — but the SIGNED bytes must match; confirm the test signs whatever body it sends)? (b) `ExecutorRead.model_copy(update={"reclaim":...})` valid + serializes nested `ReclaimItem`? (c) does `post_heartbeat` have access to `_gs()`/`write_audit`/the session for the dispatch query within the existing handler structure? (d) `with_for_update(skip_locked, of=StoragePhysicalKey)` on a join — does PG accept `OF` targeting only the physical-keys table? (e) does the executor `runner` have `Path`/`asyncio` imported + an `__init__` to add `_pending_reclaim_confirms`?
