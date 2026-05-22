# FU4 — NFS-Shared Local Reclamation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development / executing-plans. Checkbox steps.

**Goal:** Let any healthy executor that has a backend's `base_path` mounted (advertised + existence-verified) reclaim its local orphan keys — not just the writer — closing FU3's writer-offline gap for NFS-shared backends.

**Spec:** `docs/superpowers/specs/2026-05-22-fu4-nfs-shared-reclaim-design.md` (read fully — §0 safety hinge: advertise only `is_dir`-verified paths so confirm-on-missing stays sound; double-dispatch is idempotent-harmless).

**Locked constraints:**
- Safety: the executor advertises ONLY base_paths it has verified (`Path(p).is_dir()`). Confirm-on-missing + the FU3 path-traversal guard are UNCHANGED. Same `gc_delete_physical_bytes` gate, grace, `~live`, cap.
- No migration — the advertised set rides `Executor.capabilities` JSONB (reassign the dict, don't mutate in place, so SQLAlchemy flags the change).
- Additive: new heartbeat field `accessible_base_paths` defaults None (no update); `dispatch_local_reclaim`'s new `accessible_base_paths` param defaults `frozenset()` → FU3 writer-only behavior byte-preserved. Existing reclaim/heartbeat/runner tests unchanged.
- HMAC: the executor signs the body including the new field; old body omitting it → handler defaults None.
- No openapi/frontend change. CI gate = pytest + `lint_invariants`; `ruff --select I001 --fix` touched files.

---

## File Structure
- **Modify** `src/dlw/schemas/executor.py` (`ExecutorHeartbeat.accessible_base_paths`), `src/dlw/services/executor_service.py` (`record_heartbeat` store), `src/dlw/services/storage_objects.py` (`dispatch_local_reclaim` sibling widening), `src/dlw/api/executors.py` (handler passes the set).
- **Modify** `src/dlw/executor/config.py` (`local_base_paths`), `src/dlw/executor/runner.py` (advertise verified set), `src/dlw/executor/client.py` (heartbeat param).
- **Modify** `tests/services/test_local_reclaim.py`, `tests/api/test_executors.py`, `tests/executor/test_runner.py`.
- **Modify** `docs/operator/storage-reclamation.md`.

---

## Milestone M1 — controller: store advertised paths + widen dispatch

### Task 1: schema + record_heartbeat store + dispatch widening
**Files:** `schemas/executor.py`, `services/executor_service.py`, `services/storage_objects.py`, `api/executors.py`; `tests/services/test_local_reclaim.py`, `tests/api/test_executors.py`.

- [ ] **Step 1 (failing tests):** extend `tests/services/test_local_reclaim.py` per spec §3 (REVISED). Seed a local backend (base_path `/srv/dlw`, storage_id S) + an orphan key written by `ex-writer`. `acc = await resolve_accessible_storage_ids(session, frozenset({"/srv/dlw"}))` → `{S}`. Assert: (a) `dispatch_local_reclaim(session, "ex-sibling", grace_seconds=3600, limit=10, accessible_storage_ids=acc)` returns the key; (b) `accessible_storage_ids=frozenset()` → does NOT (writer-only preserved); (c) a different base_path → `resolve_...` empty → not matched; (d) writer gets its own regardless; **(e) CONFIRM widening (the BLOCKER): `confirm_local_reclaim(session, "ex-sibling", [key.id], audit=_a, accessible_storage_ids=acc)` returns 1 + deletes the row even though `executor_id` is `ex-writer`; with `accessible_storage_ids=frozenset()` returns 0**; (f) tenant scope: key tenant_id=2, `tenant_id=1` passed → not authorized. Extend `tests/api/test_executors.py`: heartbeat with `accessible_base_paths=["/srv/dlw"]` stores it on `ex.capabilities["base_paths"]`; omitting the field leaves capabilities unchanged; a sibling heartbeat (gc enabled) returns the writer's key in `reclaim` (with `size`) then a follow-up heartbeat with `reclaimed_key_ids=[that id]` deletes the row (end-to-end sibling confirm).
- [ ] **Step 2: verify FAIL.**
- [ ] **Step 3: schema** — `ExecutorHeartbeat`: add `accessible_base_paths: list[str] | None = None`. `ReclaimItem`: add `size: int` (required) — update any FU3 test/code constructing a `ReclaimItem` to pass `size=`.
- [ ] **Step 4: record_heartbeat** — after the existing field updates, `if body.accessible_base_paths is not None: ex.capabilities = {**(ex.capabilities or {}), "base_paths": list(body.accessible_base_paths)}` (REASSIGN — plain JSONB needs reassignment to flag dirty).
- [ ] **Step 5: resolve + dispatch + confirm widening** (per spec §2 — read it):
  - add `resolve_accessible_storage_ids(session, base_paths) -> set[int]` (local backends whose decrypted base_path ∈ base_paths).
  - `dispatch_local_reclaim(..., accessible_storage_ids: frozenset[int] = frozenset(), tenant_id: int | None = None)`: candidate auth = `(executor_id == executor_id) | storage_id.in_(accessible_storage_ids)` when the set is non-empty (else just `executor_id ==` — byte-identical FU3); add `tenant_id ==` to the where when `tenant_id is not None`; return `ReclaimItem(id, base_path, storage_key, size=row.size)`.
  - `confirm_local_reclaim(..., accessible_storage_ids: frozenset[int] = frozenset(), tenant_id: int | None = None)`: SAME auth widening (the row's `executor_id == E` OR `storage_id ∈ accessible_storage_ids`) + tenant scope. (This is the BLOCKER fix — confirm must authorize siblings or the ledger row never clears.)
- [ ] **Step 6: handler** — `post_heartbeat` computes the resolved set ONCE: `acc = frozenset(await resolve_accessible_storage_ids(session, frozenset((ex.capabilities or {}).get("base_paths") or [])))`; pass `accessible_storage_ids=acc, tenant_id=executor.tenant_id` to BOTH `confirm_local_reclaim` and `dispatch_local_reclaim`.
- [ ] **Step 7: verify PASS** + reclaim/executor regression: `cd "D:/download_weights" && uv run pytest tests/services/test_local_reclaim.py tests/services/test_physical_reclaim.py tests/api/test_executors.py -v` → all pass.
- [ ] **Step 8: tidy + commit.**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/schemas/executor.py src/dlw/services/executor_service.py src/dlw/services/storage_objects.py src/dlw/api/executors.py tests/services/test_local_reclaim.py tests/api/test_executors.py
git add src/dlw/schemas/executor.py src/dlw/services/executor_service.py src/dlw/services/storage_objects.py src/dlw/api/executors.py tests/services/test_local_reclaim.py tests/api/test_executors.py && git commit -m "feat(fu4): advertise+store base_paths; widen local reclaim to mount-holders"
```

### Task 2: M1 backend gate
- [ ] `uv run pytest -q` all pass (failover flake = Windows-local; isolate-confirm if it appears); `lint_invariants --strict` OK. No commit.

---

## Milestone M2 — executor: configure + advertise verified paths

### Task 3: ExecutorSettings + runner advertise + client param
**Files:** `executor/config.py`, `executor/runner.py`, `executor/client.py`; `tests/executor/test_runner.py`.

- [ ] **Step 1 (failing runner tests):** in `tests/executor/test_runner.py`: (a) configure fake settings `local_base_paths=[<existing tmp dir>, "/nonexistent/bogus"]` → assert the heartbeat call's `accessible_base_paths` kwarg == `[<existing tmp dir>]` (bogus filtered by `is_dir`); (b) **size-verify**: a `reclaim` item `{id, base_path:<tmp>, storage_key:"k", size:5}` where `<tmp>/k` exists with size 5 → unlinked + id confirmed; the same with the file at size 9 (≠5) → NOT unlinked AND id NOT confirmed (wrong-file refusal); a MISSING file → confirmed. (Inspect `mock.heartbeat.call_args_list`.)
- [ ] **Step 2: verify FAIL.**
- [ ] **Step 3: config** — `executor/config.py`: `local_base_paths: list[str] = Field(default_factory=list, description=...)`. pydantic-settings 2.6 parses the env var as **JSON** (`DLW_EXECUTOR_LOCAL_BASE_PATHS='["/a","/b"]'`, NOT comma) — verify: `uv run python -c "import os; os.environ.update(DLW_EXECUTOR_ID='x', DLW_EXECUTOR_BEARER_TOKEN='t', DLW_EXECUTOR_LOCAL_BASE_PATHS='[\"/a\"]'); from dlw.executor.config import ExecutorSettings; print(ExecutorSettings().local_base_paths)"` → `['/a']`. Unset → `[]`.
- [ ] **Step 4: runner** — `_heartbeat_loop`: `accessible = [p for p in self._s.local_base_paths if Path(p).is_dir()]`; pass `accessible_base_paths=accessible` to `heartbeat(...)`. In the reclaim handling, ADD the **size-verify** before unlink (per spec §2): after `_safe_target` passes, `expected = item.get("size")`; `if target.exists() and expected is not None and target.stat().st_size != expected:` → `logger.warning(...); continue` (do NOT confirm); else `unlink(missing_ok=True)` + confirm. (`Path` imported per FU3.)
- [ ] **Step 5: client** — `heartbeat(..., accessible_base_paths: list[str] | None = None)`: add to `body_dict` when not None (before signing).
- [ ] **Step 6: verify PASS** + executor regression: `cd "D:/download_weights" && uv run pytest tests/executor/ -v` → all pass.
- [ ] **Step 7: tidy + commit.**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/executor/config.py src/dlw/executor/runner.py src/dlw/executor/client.py tests/executor/test_runner.py
git add src/dlw/executor/config.py src/dlw/executor/runner.py src/dlw/executor/client.py tests/executor/test_runner.py && git commit -m "feat(fu4): executor advertises existence-verified local_base_paths"
```

### Task 4: M2 backend gate
- [ ] `uv run pytest -q` all pass; `lint_invariants --strict` OK. No commit.

---

## Milestone M3 — docs

### Task 5: docs
- [ ] **Step 1:** `docs/operator/storage-reclamation.md`: local-fs reclaim now also runs via ANY healthy executor that advertises the backend's `base_path` (operator config `DLW_EXECUTOR_LOCAL_BASE_PATHS` — a **JSON list**, existence-verified at heartbeat) — covering NFS-shared backends whose writer is offline; the writer-only path remains; SAME `gc_delete_physical_bytes` gate. Document the safety envelope: (1) the executor advertises only `is_dir`-verified paths; (2) before deleting, it verifies the file SIZE matches the ledger (refuses a wrong-sized file — guards same-named-different-mount misconfig); (3) the FU3 path-traversal guard bounds deletes under base_path; (4) dispatch/confirm are tenant-scoped. **Hard operator constraint: local backend `base_path` values MUST be unique** (two backends sharing a base_path string would be conflated). Honest note: local-fs is a minor backend and this NFS-shared-offline-writer path is a narrow case — shipped for parity. Prose.
- [ ] **Step 2: commit.**
```bash
cd "D:/download_weights" && git add docs/operator/storage-reclamation.md && git commit -m "docs(fu4): NFS-shared local reclamation via advertised base_paths"
```

---

## Self-Review
- **Spec coverage:** §1.1 config → Task 3 ✓; §1.2 advertise → Task 3 ✓; §1.3 store → Task 1 ✓; §1.4 dispatch widen → Task 1 ✓; §3 tests → Tasks 1,3 ✓; §4 milestones → M1/M2/M3 ✓.
- **Placeholder scan:** Task 3 Step 3's env-parse verification is a real check with a concrete command, not a TODO. The pydantic list-env form is the one implementer-judgment point (adjust to the installed parser).
- **Type consistency:** `accessible_base_paths: list[str] | None` (heartbeat) / `frozenset[str]` (dispatch param); `ex.capabilities["base_paths"]`; `local_base_paths: list[str]`. Consistent.
- **Open risks for reviewers:** (a) does pydantic-settings parse `DLW_EXECUTOR_LOCAL_BASE_PATHS` as a list (JSON? comma?) — the impl verifies; (b) `ex.capabilities` reassignment vs in-place — confirm SQLAlchemy flags the JSONB change (MutableDict not used → reassign required); (c) the `(executor_id==) | storage_id.in_(sibling_ids)` OR with the existing join + `FOR UPDATE OF` — compiles on PG; (d) confirm-on-missing safety under the advertise-only-verified hinge — is `is_dir` at heartbeat a strong enough proxy for "has the actual file's mount" (yes for a real NFS mount; the residual false-advertise edge is documented).
