# FU4 — NFS-Shared Local Reclamation (any mount-holder) (Design)

> First of the six remaining named follow-ons (user: "从1顺序实现到6"). FU3
> reclaims local-fs orphan bytes by dispatching each key to its WRITER executor;
> if the writer is offline, the key waits. FU4 closes that gap for NFS-shared
> local backends: any healthy executor that ACTUALLY HAS the backend's
> `base_path` mounted can reclaim the key — not just the writer.
> Status: self-approved per Rule #1. Branch: `feat/fu4-nfs-shared-reclaim`.

## 0. Safety design (revised after pre-review — two BLOCKERs + a cross-tenant vector)

Pre-review found the naive "widen dispatch, leave confirm unchanged" design BROKEN
and unsafe. The safe design rests on FOUR controls:

1. **Confirm MUST be widened in lockstep with dispatch.** FU3's
   `confirm_local_reclaim` filters `executor_id == confirming_executor`. If a
   SIBLING (non-writer) reclaims a key (whose `executor_id` is the writer), its
   confirm matches ZERO rows → the file is deleted but the ledger row PERSISTS →
   re-dispatched forever. So confirm must authorize a key for executor E when
   `executor_id == E` **OR** the key's `storage_id` is in E's resolved-accessible
   set — the SAME authorization dispatch uses. A single helper
   `resolve_accessible_storage_ids(session, base_paths)` computes the set once;
   the handler passes it to BOTH confirm and dispatch.
2. **Bind to `storage_id`, not the raw base_path string.** Dispatch/confirm
   resolve E's advertised base_paths → the set of local `storage_id`s whose
   decrypted `base_path` ∈ the advertised set, and match keys on `storage_id`.
   Two backends sharing a base_path string would BOTH resolve — so the operator
   constraint **local backend `base_path` values MUST be unique** is documented
   + the dispatch is tenant-scoped (a key is only handed to E if the key's
   `tenant_id` matches E's `tenant_id`, when E has one — see control 4).
3. **File-size verification before delete (the real wrong-file guard).** The
   `is_dir`-verified advertisement proves E has *a* dir at that path, NOT that the
   file under it is the right backend's data (same-named-different-mount
   misconfig). So the executor, before unlinking, `os.stat`s the target: if it
   EXISTS and its size != the ledger `size` (now carried in `ReclaimItem`) →
   REFUSE + do NOT confirm (wrong file); if MISSING → confirm (already gone, the
   safe NFS-shared / already-reclaimed case); if PRESENT and size matches →
   unlink + confirm. Plus the FU3 path-traversal guard (under base_path) is kept.
4. **Tenant scoping.** Dispatch + confirm only authorize a key for E when the
   key's `tenant_id` == E's `tenant_id` if E has one set (`Executor.tenant_id` is
   nullable and currently NULL for all executors → a no-op today, but it
   future-proofs against the cross-tenant deletion vector when tenant-pinned
   executors arrive). Documented.

Double-dispatch (writer AND a sibling both get the key across 10 s beats) is then
harmless: idempotent `unlink` (size-checked) + `confirm` re-selects 0 the second
time. No writer-health gating needed.

**Dispatch→confirm window (re-review IMPORTANT, documented residual):** if an
executor's advertised set SHRINKS between the dispatch beat and the confirm beat
(an unmount/reconfig), the confirming `acc` no longer authorizes the key →
confirm matches 0 → the file is already unlinked but the ledger row persists.
This is NOT a regression of B1 and NOT data loss: the row stays past-grace +
`~live`, and the next beat from ANY executor still advertising that base_path
(the writer, or another mount-holder) re-dispatches it → the now-missing file →
`unlink(missing_ok=True)` → confirm succeeds → row cleared. Worst case is a
delayed re-clear; the stuck-orphan gauge (FU3) surfaces it if it lingers.

**Honest scope:** local-fs is a minor backend (inherit-only writes), and
NFS-shared-with-offline-writer is a narrow sub-case — FU4 is near-dead-code,
shipped for parity/completeness. The size-verify + storage_id binding + tenant
scope + path-guard + default-OFF gate are the envelope; the residual is an
operator who reuses a base_path string across backends OR mounts a same-named
different filesystem with same-sized colliding files (extremely unlikely).

## 1. Scope

**In scope (additive; no migration — reuse `Executor.capabilities` JSONB; no new dep):**

1. **`ExecutorSettings.local_base_paths: list[str] = []`** — operator config: the
   local/NFS base_paths this executor can serve. Env `DLW_EXECUTOR_LOCAL_BASE_PATHS`
   is parsed as a **JSON list** (pydantic-settings 2.6 is JSON-only for complex
   types — `'["/srv/dlw"]'`, NOT comma-separated; verified pinned version).
2. **Executor advertises verified base_paths** in the heartbeat:
   `ExecutorHeartbeat.accessible_base_paths: list[str] | None = None` (None = no
   update, backward-compat). The runner sends
   `[p for p in settings.local_base_paths if Path(p).is_dir()]` (existence-verified
   — the §0 hinge). Empty list is sent as `[]` (explicit "I serve no local paths").
3. **Controller stores it** on `Executor.capabilities["base_paths"]`
   (`record_heartbeat`: when `body.accessible_base_paths is not None`, set
   `ex.capabilities = {**ex.capabilities, "base_paths": body.accessible_base_paths}`
   — reassign so SQLAlchemy detects the JSONB change).
4. **`dispatch_local_reclaim` AND `confirm_local_reclaim` widen in lockstep**
   (§0 control 1): both authorize a key for E when `executor_id == E.id` OR
   `storage_id ∈ resolve_accessible_storage_ids(E's advertised base_paths)`,
   tenant-scoped to E's `tenant_id` (control 4). The handler computes the
   resolved storage-id set ONCE and passes it to both. (Leaving confirm
   writer-only — the naive design — deletes the file but never the ledger row →
   infinite redispatch. This is the BLOCKER the pre-review caught.)
5. **`ReclaimItem.size`** + **executor size-verify** (§0 control 3): the executor
   refuses to unlink a present file whose size ≠ the ledger size (wrong-file /
   misconfig guard), confirms a missing file (already gone), unlinks+confirms a
   size-matching file. Keeps the FU3 path-traversal guard.

**Out of scope (named):**
- Auto-discovery of mounts (the executor advertises only operator-configured
  `local_base_paths`, existence-filtered) — no scanning `/proc/mounts`.
- A new column / migration — the advertised set rides `Executor.capabilities`
  JSONB (already present).
- The `executor_id`-NULL pre-migration keys (FU9) — still need the backfill;
  FU4 only widens dispatch for keys that DO have a backend whose base_path a live
  executor advertises (NULL-writer keys still match via the base_path branch IF a
  live executor advertises that backend's path — so FU4 actually helps some NULL
  cases too, a bonus, but full backfill is FU9).

## 2. Implementation

`executor/config.py`:
```python
    local_base_paths: list[str] = Field(default_factory=list,
        description="Local/NFS base_paths this executor can access (reclaim).")
```
(pydantic-settings parses `DLW_EXECUTOR_LOCAL_BASE_PATHS='["/srv/dlw"]'` as a JSON
list; confirm the env-parse form during impl.)

`schemas/executor.py` `ExecutorHeartbeat`: add
`accessible_base_paths: list[str] | None = None`.

`executor/runner.py` `_heartbeat_loop`: compute + send the verified set:
```python
    from pathlib import Path
    accessible = [p for p in self._s.local_base_paths if Path(p).is_dir()]
    await self._client.heartbeat(..., accessible_base_paths=accessible)
```
(Send it every beat; cheap. `accessible=[]` when none configured/mounted.)

`executor/client.py` `heartbeat(..., accessible_base_paths: list[str] | None = None)`:
add to `body_dict` when not None (before HMAC signing).

`services/executor_service.py` `record_heartbeat`: after the existing field
updates:
```python
    if body.accessible_base_paths is not None:
        ex.capabilities = {**(ex.capabilities or {}),
                           "base_paths": list(body.accessible_base_paths)}
```

`schemas/executor.py` `ReclaimItem`: add `size: int` (the executor size-verifies
against it). So `ReclaimItem = {id, base_path, storage_key, size}`.

`services/storage_objects.py` — a shared resolver + widened dispatch + widened confirm:
```python
async def resolve_accessible_storage_ids(session, base_paths) -> set[int]:
    """Local storage_ids whose decrypted base_path is in `base_paths`.
    NOTE: base_path MUST be unique per local backend (operator constraint);
    a shared string resolves to MULTIPLE ids (tenant scope below bounds it)."""
    if not base_paths:
        return set()
    from dlw.db.models.storage import StorageBackend
    from dlw.services.storage_client import storage_config_from_backend
    out: set[int] = set()
    for b in (await session.execute(
            select(StorageBackend).where(StorageBackend.backend_type == "local"))
            ).scalars().all():
        bp = storage_config_from_backend(b).base_path
        if bp and bp in base_paths:
            out.add(b.id)
    return out

async def dispatch_local_reclaim(session, executor_id, *, grace_seconds, limit,
                                 accessible_storage_ids=frozenset(),
                                 tenant_id=None):
    ...
    auth = (StoragePhysicalKey.executor_id == executor_id)
    if accessible_storage_ids:
        auth = auth | StoragePhysicalKey.storage_id.in_(accessible_storage_ids)
    where = [auth, StoragePhysicalKey.created_at < cutoff, ~live,
             StorageBackend.backend_type == "local"]
    if tenant_id is not None:                       # control 4: tenant scope
        where.append(StoragePhysicalKey.tenant_id == tenant_id)
    rows = (await session.execute(
        select(StoragePhysicalKey).join(StorageBackend, ...)
        .where(*where).order_by(...).limit(...)
        .with_for_update(skip_locked=True, of=StoragePhysicalKey))).scalars().all()
    # ReclaimItem(id, base_path, storage_key, size=row.size) per row
    ...

async def confirm_local_reclaim(session, executor_id, key_ids, *, audit,
                                accessible_storage_ids=frozenset(),
                                tenant_id=None) -> int:
    auth = (StoragePhysicalKey.executor_id == executor_id)
    if accessible_storage_ids:
        auth = auth | StoragePhysicalKey.storage_id.in_(accessible_storage_ids)
    where = [StoragePhysicalKey.id.in_(key_ids), auth]
    if tenant_id is not None:
        where.append(StoragePhysicalKey.tenant_id == tenant_id)
    rows = (await session.execute(
        select(StoragePhysicalKey).where(*where))).scalars().all()
    for r in rows:
        await audit(action="storage.gc.physical.local", id=r.id,
                    tenant_id=r.tenant_id, storage_key=r.storage_key, size=r.size)
        await session.delete(r)
    return len(rows)
```
The handler (`api/executors.py::post_heartbeat`) computes the resolved set ONCE
and passes it to BOTH (tenant from the executor row):
```python
    paths = frozenset((ex.capabilities or {}).get("base_paths") or [])
    acc_ids = frozenset(await resolve_accessible_storage_ids(session, paths))
    if body.reclaimed_key_ids:
        await confirm_local_reclaim(session, executor.id, body.reclaimed_key_ids,
            audit=_audit, accessible_storage_ids=acc_ids, tenant_id=executor.tenant_id)
    if s.gc_delete_physical_bytes:
        reclaim = await dispatch_local_reclaim(session, executor.id,
            grace_seconds=..., limit=..., accessible_storage_ids=acc_ids,
            tenant_id=executor.tenant_id)
```

`executor/runner.py` `_heartbeat_loop` reclaim handling adds **size-verify**
(control 3) before the unlink (the path-traversal guard `_safe_target` from FU3
is kept):
```python
    target = _safe_target(item.get("base_path",""), item.get("storage_key",""))
    if target is None:
        logger.warning("local reclaim: refusing unsafe target ..."); continue
    expected = item.get("size")
    if target.exists() and expected is not None and target.stat().st_size != expected:
        logger.warning("local reclaim: size mismatch for %s (have %d want %s); "
                       "refusing", target, target.stat().st_size, expected)
        continue                                   # wrong file — do NOT confirm
    try:
        target.unlink(missing_ok=True)             # missing = already gone = ok
        confirmed.append(item["id"])
    except OSError as e:
        logger.warning("local reclaim unlink failed %s: %s", target, e)
```

## 3. Tests

- **`tests/services/test_local_reclaim.py`** (extend): seed a local backend
  (base_path `/srv/dlw`, storage_id S) + an orphan key written by `ex-writer`.
  `acc = await resolve_accessible_storage_ids(session, frozenset({"/srv/dlw"}))`
  → `{S}`. (a) `dispatch_local_reclaim(session, "ex-sibling", ...,
  accessible_storage_ids=acc)` returns the key (sibling). (b)
  `accessible_storage_ids=frozenset()` → does NOT (writer-only, FU3 preserved).
  (c) advertising a different base_path → `resolve_...` empty → not matched.
  (d) the writer gets its own keys regardless. **(e) confirm widening (the
  BLOCKER): `confirm_local_reclaim(session, "ex-sibling", [key.id], audit=...,
  accessible_storage_ids=acc)` DELETES the row (count 1) even though the row's
  `executor_id` is `ex-writer`; with `accessible_storage_ids=frozenset()` it does
  NOT (returns 0).** (f) tenant scope: a key with tenant_id 2, executor tenant_id
  1 → not authorized (when tenant_id passed).
- **`tests/api/test_executors.py`** (extend): a heartbeat with
  `accessible_base_paths=["/srv/dlw"]` stores it on `ex.capabilities["base_paths"]`;
  a subsequent heartbeat (gc enabled) returns a sibling-reclaimable key in
  `reclaim`. A heartbeat omitting the field leaves `capabilities` unchanged
  (None = no update).
- **`tests/executor/test_runner.py`** (extend): the runner sends
  `accessible_base_paths` = its `local_base_paths` filtered by `is_dir` (configure
  one existing tmp dir + one bogus path → only the existing one advertised). AND
  **size-verify**: a `reclaim` item whose file exists but with a size ≠ the item's
  `size` → the runner does NOT unlink it AND does NOT confirm its id (wrong-file
  refusal); a size-matching file → unlinked + confirmed; a missing file →
  confirmed.
- Backward-compat: existing reclaim/heartbeat tests unchanged (new params default
  empty/None; `ReclaimItem.size` — FU3 tests that construct ReclaimItem must add
  `size=` OR it defaults; make `size: int` required in the schema and update any
  FU3 test that built a ReclaimItem to pass `size`).

## 4. Milestones
- **M1 — controller**: `ExecutorHeartbeat.accessible_base_paths` + `record_heartbeat`
  store + `dispatch_local_reclaim` sibling widening + handler passes the set +
  service/API tests + backend gate.
- **M2 — executor**: `ExecutorSettings.local_base_paths` + runner advertises
  verified set + `client.heartbeat` param + runner test + backend gate.
- **M3 — docs**: update `docs/operator/storage-reclamation.md` — local reclaim now
  also via any executor advertising the backend's base_path (NFS-shared), via
  operator-configured `DLW_EXECUTOR_LOCAL_BASE_PATHS` (existence-verified); the
  writer-only path remains; same `gc_delete_physical_bytes` gate + path guard.

## 5. Risks & Contingencies
- **Safety hinge (§0)**: advertise only `is_dir`-verified paths → "advertises P"
  ⟹ "has P" ⟹ confirm-on-missing safe. False advertise = operator misconfig
  (same-named different dir); path-traversal guard still bounds deletes under
  base_path. Documented.
- **Double-dispatch harmless**: idempotent unlink + no-op confirm-delete. No
  writer-health gating needed.
- **No migration** (rides `capabilities` JSONB); reassign the dict so SQLAlchemy
  flags the change (don't mutate in place).
- **HMAC body**: new heartbeat field signed by the executor; old executor omits →
  `None` → no capability update. Backward-compat.
- **`base_path` decrypt per heartbeat**: only when the executor advertises a
  non-empty set AND `gc_delete_physical_bytes` is on; decrypts the few local
  backends (low volume). Acceptable.
- **pydantic-settings list env parse**: confirm `DLW_EXECUTOR_LOCAL_BASE_PATHS`
  accepts a JSON list (the impl verifies; default empty).
- No openapi/frontend change. CI gate = pytest + `lint_invariants`.

## 6. Self-Review
- **Closes FU3's writer-offline gap** for NFS-shared via advertised+verified
  base_paths. ✓ (refcount=0/grace/gate/path-guard all preserved.)
- **Safe**: existence-verified advertisement makes confirm-on-missing sound;
  double-dispatch idempotent. ✓
- **Additive**: new params default empty/None; FU3 writer path + existing tests
  unchanged. ✓
- **Honest**: false-advertise (operator misconfig) edge + no mount auto-discovery
  named. ✓
- **Consistency**: reuses `capabilities` JSONB, the heartbeat confirm/dispatch,
  the path guard, `storage_config_from_backend`.
