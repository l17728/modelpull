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

- [ ] **Step 1 (failing tests):** extend `tests/services/test_local_reclaim.py` per spec §3: seed a local backend (base_path `/srv/dlw`) + an orphan key written by `ex-writer`; assert (a) `dispatch_local_reclaim(session, "ex-sibling", grace_seconds=3600, limit=10, accessible_base_paths=frozenset({"/srv/dlw"}))` returns that key; (b) with `accessible_base_paths=frozenset()` it does NOT (writer-only preserved); (c) a sibling advertising a different path → not matched; (d) the writer gets its own keys regardless. Extend `tests/api/test_executors.py`: a heartbeat with `accessible_base_paths=["/srv/dlw"]` stores it on `ex.capabilities["base_paths"]`; a heartbeat omitting the field leaves capabilities unchanged.
- [ ] **Step 2: verify FAIL.**
- [ ] **Step 3: schema** — `ExecutorHeartbeat`: add `accessible_base_paths: list[str] | None = None`.
- [ ] **Step 4: record_heartbeat** — after the existing field updates, `if body.accessible_base_paths is not None: ex.capabilities = {**(ex.capabilities or {}), "base_paths": list(body.accessible_base_paths)}` (REASSIGN — JSONB change detection).
- [ ] **Step 5: dispatch widening** — `dispatch_local_reclaim(..., accessible_base_paths: frozenset[str] = frozenset())`: build `sibling_ids` (local backends whose decrypted `base_path` ∈ the set; `storage_config_from_backend(b).base_path`); candidate `where` uses `(executor_id == executor_id) | storage_id.in_(sibling_ids)` when `sibling_ids` else just `executor_id ==` (byte-identical FU3 path when empty). Per spec §2.
- [ ] **Step 6: handler** — `post_heartbeat` passes `accessible_base_paths=frozenset((ex.capabilities or {}).get("base_paths") or [])` into `dispatch_local_reclaim`.
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

- [ ] **Step 1 (failing runner test):** in `tests/executor/test_runner.py`, configure the fake settings with `local_base_paths=[<existing tmp dir>, "/nonexistent/bogus"]` → assert the heartbeat call's `accessible_base_paths` kwarg == `[<existing tmp dir>]` (bogus filtered by `is_dir`). (Inspect `mock.heartbeat.call_args`.)
- [ ] **Step 2: verify FAIL.**
- [ ] **Step 3: config** — `executor/config.py`: `local_base_paths: list[str] = Field(default_factory=list, description=...)`. (Confirm pydantic-settings parses `DLW_EXECUTOR_LOCAL_BASE_PATHS` as a JSON list — a quick `uv run python -c "from dlw.executor.config import ExecutorSettings; import os; os.environ['DLW_EXECUTOR_LOCAL_BASE_PATHS']='[\"/a\",\"/b\"]'; os.environ['DLW_EXECUTOR_ID']='x'; os.environ['DLW_EXECUTOR_BEARER_TOKEN']='t'; print(ExecutorSettings().local_base_paths)"` — adjust the env form if needed.)
- [ ] **Step 4: runner** — `_heartbeat_loop`: `accessible = [p for p in self._s.local_base_paths if Path(p).is_dir()]`; pass `accessible_base_paths=accessible` to `heartbeat(...)`. (`Path` already imported per FU3.)
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
- [ ] **Step 1:** `docs/operator/storage-reclamation.md`: local-fs reclaim now also runs via ANY healthy executor that advertises the backend's `base_path` (operator config `DLW_EXECUTOR_LOCAL_BASE_PATHS`, existence-verified at heartbeat) — covering NFS-shared backends whose writer is offline; the writer-only path remains; SAME `gc_delete_physical_bytes` gate + path-traversal guard; the existence-verification is the safety hinge (an executor only reclaims paths it actually has mounted). Note false-advertise (operator misconfig: same-named different dir) is the residual edge. Prose.
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
