# Phase 3 SP3 — Incremental Download + Global Dedup (refcount/GC) Design

> **Status:** Draft (brainstormed 2026-05-19).
> **Companion plan:** `docs/superpowers/plans/2026-05-19-phase-3-sp3-incremental-download.md` (writing-plans, after spec approval).
> **Roadmap source:** `docs/v2.0/08-mvp-roadmap.md` §3 Phase 3 Week 3a ("增量下载（diff + hardlink/copy）"). §3.5 exit: "增量下载 verify 节省 ≥ 90% 流量（同 repo 仅 tokenizer 改动）".
> **Phase 3 decomposition:** SP1 multi-tenancy = merged (PR #15, `fa08e6d`). SP2 multi-source = merged (PR #16, `454ac41`). **This is SP3 (3rd of 4).** SP4 CLI/SDK follows.
> **Design source:** `docs/v2.0/06-platform-and-ecosystem.md` §2 (增量/差分下载), §3.1 (storage_objects/refcount), §3.3 (删除前依赖检查).
> **Invariant source:** `docs/v2.0/INVARIANTS.md` row 14 (`(tenant_id,repo_id,revision,filename,sha256)` 存储中只存一份 — realized as the `storage_objects` UNIQUE constraint). Reuses 8 (tenant scoping, SP1), 11 (HF sha256 authority, SP2).
> **Closes:** the v2.0 incremental-download + global-dedup baseline (06 §2 + §3.1/§3.3 IN-scope subset).

> **⚠️ Scope decisions (authoritative — supersede broader readings of doc 06 §2/§3):**
> 1. **Unified diff+dedup.** Incremental-inherit and cross-task global-dedup are ONE mechanism: "an existing `storage_objects` row for `(tenant_id, storage_id, sha256)` ⇒ inherit via copy/hardlink, no download." The `upgrade_from_revision` diff just seeds candidates; the same lookup also dedups across unrelated tasks.
> 2. **HF sha authority for inherit.** A file is inherit-eligible only when the new revision's **HF** `expected_sha256` is non-NULL and an existing object matches it. HF non-LFS files (`sha256=NULL`) are never inherited (identity unprovable) — they download normally. Consistent with SP2 INVARIANT 11.
> 3. **Executor materializes** the inherit (it holds STS/storage creds — INVARIANT 3; the controller must not). `backend_type=s3` → `copy_object(CopySource=old_key→new_key)` (server-side, in-region ≈ free); local/nfs → `os.link`. New `inherit` subtask kind; the poll/assignment payload carries `inherit_from_key`.
> 4. **GC reclaims the DB row only.** A leader-gated GC sweep deletes `storage_objects` rows with `refcount=0` older than `gc_grace_seconds` (frees the INVARIANT-14 slot; stops a stale object being reused as an inherit source) + audits `storage.gc`. **Physical S3/fs byte reclamation is deferred to Phase 4** (orphaned bytes = cost, not correctness; a new executor-creds GC path is out of SP3 scope).
> 5. **DELETE requires terminal task.** `DELETE /api/v1/tasks/{id}` is allowed only when the task is in a terminal/cancelled state (else 409) — avoids deref races with in-flight subtasks. It deletes task/subtask/ref rows + decrements refcount; never deletes objects inline.
> 6. **Out:** §3.2 quota/LRU + `archive_after_days` cold-storage eviction (Phase 4 / quota plane); §3.4 scheduled auto-probe tasks (Phase 4); mid-upload abort dedup micro-opt (the scheduling-phase pre-check captures the value); cross-tenant dedup (INVARIANT 14 is per-tenant by design); BLAKE3/content-addressed layout (v2.2 — keys stay revision-path-addressed, which is why the copy is required); UI delete-dependency view (frontend).
> 7. **Pre-execution-review correctness rulings (2-reviewer pass 2026-05-19, authoritative):**
>    a. **Fully-inherited task must not pause.** `plan_task_sources` has 3 early-return gates (`pinned`/`no_sha256_authority`/`no_source_speed`) that run *before* its subtask query. After `diff_and_dedup` flips every file to `inherit`, the planner must early-return BEFORE those gates when there are zero `pending` subtasks (leave status `scheduling` so `run_scheduling_tick` flips it to `downloading` → inherit subs claim → terminal `succeeded`). The pending-subs load + `if not pending: return` goes at the TOP of `plan_task_sources`.
>    b. **`StorageConfig` gains `backend_type`.** The real `StorageConfig` (bucket/region/endpoint_url/key_prefix only) has no backend discriminator, so the §3.6 local `os.link` branch is unreachable. Add `backend_type: str = "s3"` (and an optional local `base_path`) to `StorageConfig`; the poll endpoint (`api/executors.py`) threads `StorageBackend.backend_type` into the assignment's storage config so `materialize_inherit` can pick S3-copy vs hardlink.
>    c. **Runner anchor:** the executor method is `_execute_subtask` (NOT `_run_one`); the settings attribute is `self._s` (NOT `self._settings`). The inherit branch goes after `assignment = Assignment(...)` and before `downloader = self._choose_downloader(...)`.
>    d. **`storage.gc` audit is required** (this banner + §3.2): the GC loop calls the SP1 `write_audit(action="storage.gc", resource_type="storage_objects", outcome="success", payload={"reclaimed": n})` after a non-zero reclaim.
>    e. **`upgrade_from_revision` threading:** `create_task` must set `DownloadTask.upgrade_from_revision = body.upgrade_from_revision` (it currently does not; the column exists). Functionally the unified dedup works without it, but it is a stated acceptance criterion + an audit/observability hint.
>    f. **Inherit-then-fail self-corrects (supersedes §7's "reconciled by GC — acceptable"):** if an `inherit` subtask's executor copy fails (`final_status=="failed"`) and a `SubtaskObjectRef` exists for it (added by `diff_and_dedup`), `complete_subtask` calls `deref_subtask` AND resets the subtask to `pending` with `inherit_from_key=NULL` so it downloads normally next scheduling pass. (Otherwise the premature refcount++ would permanently pin a possibly-orphan object — GC only reclaims `refcount<=0`.)
> The companion plan embeds all of these; it is the execution source of truth.

---

## 1. Goal & Scope

### 1.1 Goal

A model version upgrade (e.g. only `tokenizer.json` changed in a 689 GB repo) must not re-download unchanged files: identical content (proven by HF sha256) is materialized into the new revision's storage location by a cheap server-side copy / hardlink, and identical content is physically stored once per tenant+backend (refcounted), reclaimed when no task references it.

**Mechanism.** `POST /api/v1/tasks` accepts an optional `upgrade_from_revision`. A new **scheduling-phase diff/dedup stage** (runs before SP2's `plan_task_sources`) checks, for each file the new HF manifest lists with a non-NULL sha256, whether a `storage_objects` row already exists for `(tenant_id, storage_id, sha256)` (seeded by completed subtasks of `upgrade_from_revision` AND by any prior task — unified dedup). If so the new subtask becomes `status="inherit"` with `inherit_from_key` set, a `subtask_object_refs` row is added and `storage_objects.refcount` incremented immediately — no download, no SP2 source planning. Otherwise the subtask stays `pending` and flows through SP2's LPT/source planner as a normal download. The executor, on claiming an `inherit` subtask, performs an S3 server-side `copy_object` (or local `os.link`) from `inherit_from_key` to the new revision's `compose_key`, then reports `succeeded` (no HF/source bytes). On any subtask's success the controller upserts the `storage_objects` row (`INSERT ... ON CONFLICT (tenant_id,storage_id,sha256) DO UPDATE refcount=refcount+1`) and records the ref. `DELETE /api/v1/tasks/{id}` (terminal tasks only) drops the task/subtask/ref rows and decrements refcount; a leader-gated GC sweep removes `refcount=0` `storage_objects` rows older than `gc_grace_seconds`.

After SP3, a non-upgrade task behaves exactly as before SP2/SP3 *plus* transparent global dedup: the first download of a model stores objects+refs; a second identical download (any task, same tenant/backend) inherits via copy instead of re-downloading.

### 1.2 In scope

| Item | Where |
|---|---|
| `upgrade_from_revision` on `TaskCreate` (model column already exists) | `src/dlw/schemas/task.py`, `src/dlw/services/task_service.py` |
| `StorageObject` + `SubtaskObjectRef` models + 1 migration (+ `FileSubTask.inherit_from_key`) | `src/dlw/db/models/storage_object.py` (new), `src/dlw/alembic/versions/<rev>_p3sp3_incremental.py` (new) |
| `"inherit"` added to `tools/lint_invariants.py` `VALID_SUBTASK_STATUS` | `tools/lint_invariants.py` |
| Scheduling-phase diff/dedup stage | `src/dlw/services/incremental.py` (new) |
| `storage_objects` upsert/ref/deref/GC service | `src/dlw/services/storage_objects.py` (new) |
| Wire diff/dedup into the SP2 scheduling loop **before** `plan_task_sources` | `src/dlw/services/source_scheduler.py` `run_scheduling_tick` |
| `record_object` on subtask success | `src/dlw/services/scheduler.py` `complete_subtask` |
| Executor inherit materialization (S3 copy / local hardlink) + runner dispatch | `src/dlw/executor/inherit.py` (new), `src/dlw/executor/runner.py` |
| `inherit_from_key`/kind in the poll/assignment payload | `src/dlw/api/executors.py`, `src/dlw/schemas/subtask.py` (or assignment schema) |
| `DELETE /api/v1/tasks/{id}` (terminal-only, tenant-scoped, RBAC) | `src/dlw/api/tasks.py` |
| Leader-gated GC sweep | `src/dlw/main.py`, config `gc_grace_seconds`/`gc_interval_seconds` |
| Operator note: incremental/dedup/GC ops | `docs/operator/incremental-download.md` (new) |

### 1.3 Non-goals (deferred — explicit)

| Item | Where |
|---|---|
| Quota/LRU + `archive_after_days` cold-storage eviction (§3.2) | Phase 4 / quota plane |
| Physical S3/fs byte reclamation in GC (executor-creds GC path) | Phase 4 (SP3 GC frees the DB row only — banner #4) |
| Scheduled auto-probe tasks (§3.4) | Phase 4 |
| Mid-upload abort dedup micro-opt | scheduling-phase pre-check covers the value |
| Cross-tenant dedup | INVARIANT 14 is per-tenant by design |
| BLAKE3 / content-addressed storage layout | v2.2 (keys stay revision-path-addressed → copy required) |
| UI delete-dependency view | frontend sub-project |
| CLI `dlw upgrade`/`dlw materialize` | **SP4** |

---

## 2. Tech Stack Additions

**None.** `boto3` (executor S3 incl. `copy_object`) already present; `os.link` is stdlib; SQLAlchemy async + asyncpg + alembic + the SP1/SP2 leader-gated-loop pattern already in use. **One alembic migration** (`down_revision = "bb1dd2c45a12"`, the SP2 head). No new CI jobs. Real CI gates unchanged (SP1/SP2-verified): `pytest`, `invariant_lint` (`tools/lint_invariants.py` — **`"inherit"` MUST be added to `VALID_SUBTASK_STATUS` since scheduler/task_service/tasks.py are scanned and will assign `status="inherit"`**), `openapi` (spectral+swagger-cli), `yamllint(deploy/ api/)`. `uv.lock` unchanged (no new deps).

---

## 3. Components

### 3.1 `src/dlw/db/models/storage_object.py`

```python
class StorageObject(Base):
    __tablename__ = "storage_objects"
    __table_args__ = (UniqueConstraint("tenant_id", "storage_id", "sha256"),)
    id: int (BigInteger pk)
    tenant_id: int (FK tenants.id, not null)
    storage_id: int (not null)            # the StorageBackend it lives in
    storage_key: str(1024) (not null)     # the physical object key/path
    sha256: str(64) (not null)
    size: int (BigInteger, not null)
    refcount: int (Integer, not null, default 1)
    last_referenced_at: datetime tz (server_default now)
    created_at: datetime tz (server_default now)

class SubtaskObjectRef(Base):
    __tablename__ = "subtask_object_refs"
    subtask_id: uuid (FK file_subtasks.id ondelete CASCADE, pk part)
    object_id: int (FK storage_objects.id, pk part)
    # composite PK (subtask_id, object_id)
```
INVARIANT 14 = the `UNIQUE(tenant_id, storage_id, sha256)` constraint (one physical copy per tenant+backend+content). Models registered in `src/dlw/db/models/__init__.py` (SP1 lesson — never `base.py`). `FileSubTask` gains `inherit_from_key: str|None` (nullable; the source object key for an `inherit` subtask). `DownloadTask.upgrade_from_revision` **already exists** (initial schema) — no migration column for it.

### 3.2 `src/dlw/services/storage_objects.py`

- `async def record_object(session, *, tenant_id, storage_id, storage_key, sha256, size, subtask_id) -> None` — `pg_insert(StorageObject).values(...refcount=1...).on_conflict_do_update(index_elements=[tenant_id,storage_id,sha256], set_={refcount: StorageObject.refcount+1, last_referenced_at: now})`; then fetch the (new or existing) object id and `pg_insert(SubtaskObjectRef).values(subtask_id, object_id).on_conflict_do_nothing()`. Caller commits. Strong-consistent: the ON CONFLICT serializes concurrent same-sha completions (no dup row, no lost ref).
- `async def deref_subtask(session, subtask_id) -> None` — for each `SubtaskObjectRef` of the subtask: `UPDATE storage_objects SET refcount = refcount - 1 WHERE id = :oid`; delete the ref rows. (FK `ondelete CASCADE` also removes refs when the subtask row is deleted, but explicit deref runs the refcount decrement first.)
- `async def gc_orphans(session, *, grace_seconds) -> int` — `DELETE FROM storage_objects WHERE id IN (SELECT id FROM storage_objects WHERE refcount <= 0 AND created_at < now() - grace FOR UPDATE SKIP LOCKED)`; returns count; caller commits. Audits `storage.gc` (count) via the SP1 `write_audit` helper. Dependency-safe: only `refcount<=0`.

### 3.3 `src/dlw/services/incremental.py`

`async def diff_and_dedup(session, task) -> None`, called by `run_scheduling_tick` **before** `plan_task_sources` (task already `scheduling`):

1. Load the task's `FileSubTask`s (created `pending` by `create_task` from the new HF manifest, carrying HF `expected_sha256`).
2. For each sub with `expected_sha256 IS NOT NULL`: look up an existing `StorageObject` for `(task.tenant_id, task.storage_id, sub.expected_sha256)`.
   - **Found** → `sub.status="inherit"`, `sub.inherit_from_key = obj.storage_key`; `record_ref_only(session, obj.id, sub.id)` (add `SubtaskObjectRef` + `refcount++` + `last_referenced_at=now`).
   - **Not found** → leave `pending` (flows to `plan_task_sources` → SP2 LPT/source).
3. `expected_sha256 IS NULL` (HF non-LFS) → always `pending` (never inherited; identity unprovable).
4. `upgrade_from_revision` is the *seed*: completed subtasks of `(tenant,repo,upgrade_from_revision)` already produced `storage_objects` rows at their completion, so step 2's generic lookup naturally finds them — no separate old-revision query needed (unified dedup, banner #1). Old-revision-only files simply don't appear in the new manifest → skipped.

(`record_ref_only` is the ref+refcount half of `record_object`, factored shared.)

### 3.4 `src/dlw/services/source_scheduler.py` (modified)

In `run_scheduling_tick`, after `task.status="scheduling"` and before `plan_task_sources(...)`: `await diff_and_dedup(session, task)`. Then `plan_task_sources` operates only on the still-`pending` (non-inherit) subtasks (it already selects all subtasks; it must skip `status != "pending"` — verify it filters, else add the filter). Inherit subtasks are immediately claimable (lightweight copy).

### 3.5 `src/dlw/services/scheduler.py` (modified)

In `complete_subtask`, on the success path (after `sub.status="succeeded"`, `sub.actual_sha256`/`s3_key` set, parent locked — same insertion locus as SP2's blacklist hook): if `final_status == "succeeded"` and `sub.actual_sha256` and `sub.s3_key`: `await record_object(session, tenant_id=sub.tenant_id, storage_id=parent.storage_id, storage_key=sub.s3_key, sha256=sub.actual_sha256, size=sub.bytes_downloaded or 0, subtask_id=sub.id)`. Module-top `from dlw.services.storage_objects import record_object` (no circular import: storage_objects imports only models). This covers BOTH normal downloads and inherit subtasks (an inherit subtask reports `succeeded` with the inherited sha + new key → it gets its own ref to the *new-key* object; the copy created a distinct physical key, so a new `storage_objects` row for the new key is correct — UNIQUE is on sha not key, so the ON CONFLICT increments the *existing* sha row's refcount and the ref points there). NOTE: for an inherit subtask the ref was already added in `diff_and_dedup` step 2; `record_object`'s `on_conflict_do_nothing` on the ref makes the completion-time call idempotent (no double-ref, no double-refcount because the unique sha row already exists and DO UPDATE would double-count — see §7 mitigation).

### 3.6 `src/dlw/executor/inherit.py` + `runner.py` (modified)

`async def materialize_inherit(*, settings, storage_config, src_key, dst_key, sha256, size) -> DownloadResult`: if `backend_type` indicates S3 → `s3.copy_object(Bucket, CopySource={'Bucket':bucket,'Key':src_key}, Key=dst_key)` (server-side, `asyncio.to_thread`); if local/nfs → `os.link(src_path, dst_path)` (hardlink; fall back to `shutil.copy2` cross-device). Return `DownloadResult(bytes_written=size, actual_sha256=sha256, s3_key=dst_key)` — no HF/source fetch. `runner.py`: when the poll payload's subtask carries an inherit marker (`inherit_from_key` present / a `kind=="inherit"`), dispatch to `materialize_inherit` instead of the downloader, then `report(status="succeeded", actual_sha256=<known>, bytes_downloaded=size, s3_key=dst_key, assignment_token=...)`.

### 3.7 `src/dlw/api/tasks.py` (modified) — `DELETE`

```python
@router.delete("/{task_id}", status_code=204)
async def delete_task(task_id, principal=Depends(require_perm("/api/v1/tasks*","DELETE")),
                      session=Depends(_session)):
    row = scalar(tenant_filtered(select(DownloadTask).where(id==task_id), DownloadTask, principal))
    if row is None: 404
    if row.status not in ("succeeded","failed","cancelled"): 409 {"code":"TASK_NOT_TERMINAL"}
    for sub in row.subtasks: await deref_subtask(session, sub.id)
    await session.delete(row)            # FK cascade removes subtasks + refs
    await session.commit()
```
Tenant-scoped (cross-tenant id → 404, no existence leak — SP1 pattern). RBAC via the existing `require_perm` (no new policy — `DELETE` verb already in the casbin policy for tenant_admin/operator on `/api/v1/tasks*`).

### 3.8 `src/dlw/main.py` (modified) — leader-gated GC

Reuse the SP2 `_rebalance_loop`/holder pattern exactly: add `gc_task_holder`, `_gc_loop` (`while True: sleep(_gs().gc_interval_seconds); async with factory() as s: await gc_orphans(s, grace_seconds=_gs().gc_grace_seconds); await s.commit()`), started in `_on_active`, cancelled in `_on_step_down` (mirror the rebalance-holder cancel block). Leader-gated (only the active controller GCs). Bootstrap of `app.state` unchanged (GC uses `factory`, no new app.state — so no `make_app_with_state`/`test_lifespan_state` change needed; a `test_sp3` lifespan assertion still added for the GC-loop wiring regression, SP1 lesson).

### 3.9 Config (`config.py`)

```python
    # Phase 3 SP3 — incremental / dedup GC
    gc_interval_seconds: float = Field(default=60.0, ge=5.0, le=3600.0)
    gc_grace_seconds: int = Field(default=3600, ge=0)
```

---

## 4. Approaches Considered

- **A — Unified scheduling-phase diff/dedup → inherit subtask + executor copy/hardlink + refcount on complete + leader-gated GC (chosen).** Smallest blast radius (reuses SP2's scheduling-loop seam, SP1/SP2 leader-loop pattern, the existing executor claim/report path with one dispatch branch); diff and global-dedup collapse to one lookup; INVARIANT 14 enforced by one DB constraint; every unit isolated-testable.
- **B — Content-addressed storage layout (key=sha → inherit is free, no copy).** Eliminates the copy but is a storage-layout migration breaking every revision-path consumer and SP2's `compose_key` contract. Rejected (huge, out of scope).
- **C — Controller-performed S3 copy/delete.** Simplest control flow but the controller holds no long-term storage creds (INVARIANT 3 — STS is per-subtask to executors). Rejected (security-invariant violation).

---

## 5. Schema Changes

One migration `<rev>_p3sp3_incremental`, `down_revision = "bb1dd2c45a12"`.

```sql
ALTER TABLE file_subtasks ADD COLUMN inherit_from_key VARCHAR(1024);   -- nullable
CREATE TABLE storage_objects (
  id BIGSERIAL PRIMARY KEY,
  tenant_id BIGINT NOT NULL REFERENCES tenants(id),
  storage_id BIGINT NOT NULL,
  storage_key VARCHAR(1024) NOT NULL,
  sha256 VARCHAR(64) NOT NULL,
  size BIGINT NOT NULL,
  refcount INTEGER NOT NULL DEFAULT 1,
  last_referenced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, storage_id, sha256)
);
CREATE INDEX idx_storage_obj_gc ON storage_objects (refcount, created_at);
CREATE TABLE subtask_object_refs (
  subtask_id UUID NOT NULL REFERENCES file_subtasks(id) ON DELETE CASCADE,
  object_id BIGINT NOT NULL REFERENCES storage_objects(id),
  PRIMARY KEY (subtask_id, object_id)
);
```
Additive; existing rows unaffected; clean downgrade (drop refs, drop storage_objects, drop column — reverse order). `DownloadTask.upgrade_from_revision` already exists (no DDL). `storage_objects`/`subtask_object_refs` carry `tenant_id`/FK-chain to `file_subtasks`→`download_tasks.tenant_id`; queries go through `tenant_filtered` on the parent task (Invariant 8, consistent with SP1/SP2). Models in `db/models/__init__.py`.

## 6. Wire Format Changes

- **Changed:** `POST /api/v1/tasks` `TaskCreate` gains optional `upgrade_from_revision: str|None` (max 64). Poll/assignment response (`AssignmentResponse`/subtask payload) gains `inherit_from_key: str|None` (present ⇒ executor materializes via copy/hardlink, no source fetch). Subtask `status` domain gains `inherit`.
- **New:** `DELETE /api/v1/tasks/{task_id}` → 204 / 404 (missing or cross-tenant) / 409 `TASK_NOT_TERMINAL` / 401 / 403.
- **Config:** `DLW_GC_INTERVAL_SECONDS`, `DLW_GC_GRACE_SECONDS`.
- **OpenAPI:** add the `DELETE /api/v1/tasks/{taskId}` operation + `upgrade_from_revision` on the create body + `inherit` status enum value + `inherit_from_key` on the assignment schema. Must pass spectral `--fail-severity=error` + swagger-cli + yamllint(`api/`).

## 7. Error Handling Matrix

| Situation | Behaviour |
|---|---|
| `upgrade_from_revision` set but old rev never completed / no matching object | files just download normally (graceful; no error) |
| HF lists file with `sha256=NULL` (non-LFS) | never inherited → normal download (identity unprovable) |
| Existing object for `(tenant,storage,sha)` found | subtask → `inherit`, ref+refcount++ now, executor copies — no download |
| Inherit `copy_object`/`os.link` fails (key gone, perms, ENOSPC) | subtask → `failed`; `complete_subtask` detects the failed-inherit (has a `SubtaskObjectRef`), calls `deref_subtask` (undo the premature refcount++/ref) AND resets the subtask to `pending` with `inherit_from_key=NULL` → next scheduling pass downloads it normally (banner 7f — NOT "left for GC") |
| Two subtasks complete same sha concurrently | `record_object` `ON CONFLICT (tenant,storage,sha) DO UPDATE refcount=refcount+1` serializes; ref insert `ON CONFLICT DO NOTHING` |
| **Double-count risk:** an `inherit` subtask got refcount++ in `diff_and_dedup`, then `complete_subtask`→`record_object` would refcount++ again | `record_object` skips the refcount bump + ref for a subtask that already has a `SubtaskObjectRef` (idempotent: "ref exists ⇒ no-op"). Implemented as: in `record_object`, if a `SubtaskObjectRef` for `subtask_id` already exists, return early. |
| `DELETE` on non-terminal task | 409 `TASK_NOT_TERMINAL` (banner #5) |
| `DELETE` cross-tenant id | 404 (tenant_filtered, no existence leak) |
| GC vs an object that just got a new ref (race) | `gc_orphans` `WHERE refcount<=0 ... FOR UPDATE SKIP LOCKED`; `record_object`/`record_ref_only` take the row in their txn — a concurrent ref bump moves refcount>0 before GC commits, GC's `WHERE refcount<=0` excludes it |
| Object refcount goes negative (buggy double-deref) | GC predicate `refcount<=0` still reclaims it; deref uses `GREATEST(refcount-1,0)`-style guard not required for correctness but `refcount-1` is fine since GC is `<=0` |
| Standby controller | `_gc_loop` not running (leader-gated); scheduling/diff happens only on active |
| Local backend hardlink across devices (`EXDEV`) | `materialize_inherit` falls back to `shutil.copy2` (still no source fetch) |

## 8. Testing Strategy

TDD throughout. DB fixtures use `drop_all→create_all` + teardown drop (SP2 session-DB lesson — baked into every dispatch prompt); new test dirs get `__init__.py`; `"inherit"` added to `lint_invariants` before any scanned file assigns it.

| Area | File | Cases |
|---|---|---|
| Models/migration | `tests/db/test_p3sp3_migration.py` | tables+column created / UNIQUE(tenant,storage,sha) enforced / down clean |
| storage_objects svc | `tests/services/test_storage_objects.py` | record_object insert(refcount=1)+ref / 2nd same-sha → refcount=2 same row / ON CONFLICT serialize / ref-exists ⇒ idempotent no-op / deref → refcount-- / gc deletes refcount=0+grace only, SKIP LOCKED, leaves refcount>0 |
| diff/dedup | `tests/services/test_incremental.py` | object exists → inherit+inherit_from_key+ref / sha differs → pending / sha=NULL → pending / unified dedup across an unrelated prior task / upgrade_from_revision seeds inherit |
| scheduler hook | `tests/services/test_record_object_on_complete.py` | succeeded download → storage_object+ref / inherit subtask completion does NOT double-count (ref-exists guard) |
| executor inherit | `tests/executor/test_inherit.py` | s3 copy_object (moto/mock) src→dst / local os.link via tmp_path / EXDEV→copy2 fallback / failure → exception |
| DELETE route | `tests/api/test_delete_task.py` | terminal → 204 + refcount-- / non-terminal → 409 / cross-tenant → 404 / unauth → 401 |
| GC loop | `tests/test_sp3_lifespan.py` | real lifespan starts `_gc_loop` only on active; cancelled on step-down (SP1/SP2 regression-class) |
| scheduling wiring | `tests/services/test_source_scheduler.py` (extend) | diff_and_dedup runs before plan_task_sources; inherit subs excluded from LPT |
| E2E incremental | `tests/e2e/test_incremental.py` | task A downloads repo (objects+refs created); task B = upgrade_from_revision of A with 1 file's sha changed → N-1 inherit (copy) + 1 download; assert ≥90% files inherited (roadmap §3.5 exit) |

CI gates (verified SP1/SP2): full `uv run pytest`; `python -m pytest tools/test_lint_invariants.py` + `python tools/lint_invariants.py` (after adding `"inherit"`) + `lint_no_direct_status_write.py`; spectral+swagger-cli on `api/openapi.yaml`; yamllint(`deploy/ api/`). No ruff/mypy CI.

## 9. Acceptance Criteria

- [ ] `upgrade_from_revision` accepted on `TaskCreate` and threaded into the task.
- [ ] `StorageObject`/`SubtaskObjectRef` + `FileSubTask.inherit_from_key` + migration (down_revision `bb1dd2c45a12`); `UNIQUE(tenant_id,storage_id,sha256)` enforces INVARIANT 14; clean up/down; models in `db/models/__init__.py`.
- [ ] `"inherit"` in `tools/lint_invariants.py` `VALID_SUBTASK_STATUS`; `python tools/lint_invariants.py` exits 0.
- [ ] `diff_and_dedup`: existing-object → `inherit`+`inherit_from_key`+ref+refcount++; sha differs/NULL → `pending`; runs before `plan_task_sources`; inherit subs excluded from SP2 source planning.
- [ ] `record_object` on success: upsert with `ON CONFLICT refcount+1`, ref `ON CONFLICT DO NOTHING`, **ref-exists ⇒ idempotent** (no inherit double-count).
- [ ] Executor materializes inherit via S3 `copy_object` / local `os.link` (EXDEV→copy2), reports `succeeded` with no source fetch.
- [ ] `DELETE /api/v1/tasks/{id}`: terminal-only (409 otherwise), tenant-scoped (404 cross-tenant), `require_perm`-gated, refcount-- via `deref_subtask`, FK-cascade rows.
- [ ] Leader-gated `_gc_loop` deletes refcount=0 objects older than `gc_grace_seconds` (`SKIP LOCKED`, audited `storage.gc`), only on the active controller; cancelled on step-down.
- [ ] Full suite green; invariant_lint/openapi(spectral+swagger-cli)/yamllint CI gates green.
- [ ] `E2E-incremental` proves ≥90% files inherited on a single-file-changed upgrade (roadmap §3.5 exit).

## 10. Implementation Phasing (preview for plan)

5 milestones, ~14–16 TDD tasks.

- **M1 — Schema + models + config + lint.** config fields; `storage_object.py` models + `__init__.py` reg + `FileSubTask.inherit_from_key`; migration; `"inherit"` → `VALID_SUBTASK_STATUS`; migration test.
- **M2 — storage_objects service.** `record_object`/`record_ref_only`/`deref_subtask`/`gc_orphans` + the ref-exists idempotency guard; unit tests.
- **M3 — diff/dedup + scheduler wiring.** `incremental.diff_and_dedup`; wire into `run_scheduling_tick` before `plan_task_sources` (+ ensure planner skips non-`pending`); `complete_subtask` `record_object` hook; tests.
- **M4 — executor inherit + DELETE + GC loop.** `executor/inherit.py` + runner dispatch + `inherit_from_key` in poll payload; `DELETE /api/v1/tasks/{id}`; leader-gated `_gc_loop` in `main.py` + `test_sp3_lifespan`; tests.
- **M5 — E2E + docs + PR.** `tests/e2e/test_incremental.py` (≥90% inherit), OpenAPI + `docs/operator/incremental-download.md`, full suite + all CI gates, final whole-impl review, PR, squash-merge.

Branch: `feat/phase-3-sp3-incremental-download` (off `main` @ `454ac41`).

## 11. References

- Design: `docs/v2.0/06-platform-and-ecosystem.md` §2 (增量), §3.1 (storage_objects/refcount), §3.3 (删除依赖); §2.4 (增量+多源叠加).
- Roadmap: `docs/v2.0/08-mvp-roadmap.md` §3 W3a + §3.5 exit ("节省 ≥ 90%").
- Invariants: `docs/v2.0/INVARIANTS.md` 14 (one copy per tenant+content); 8 (tenant scope, SP1); 11 (HF sha authority, SP2).
- Code anchors: `src/dlw/services/task_service.py` `create_task` (subtask gen; `upgrade_from_revision` model col exists, unused), `src/dlw/services/scheduler.py` `complete_subtask` (SP2 hook locus), `src/dlw/services/source_scheduler.py` `run_scheduling_tick` (SP2 scheduling loop seam), `src/dlw/executor/_io.py` `compose_key` (`{prefix}/{repo}/{rev}/{file}`), `src/dlw/executor/runner.py` (claim→dispatch→report), `src/dlw/db/models/storage.py` (`StorageBackend.backend_type`), alembic head `bb1dd2c45a12`.
- Predecessors: SP1 spec (tenant `Principal`/`require_perm`/`tenant_filtered`, leader-gated-loop pattern, lifespan-state lesson), SP2 spec (`run_scheduling_tick` seam, `complete_subtask` hook locus, session-DB test lesson, scope-banner pattern).
- Merged: SP1 PR #15 (`fa08e6d`), SP2 PR #16 (`454ac41`).
