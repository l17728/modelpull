# FU4 — NFS-Shared Local Reclamation (any mount-holder) (Design)

> First of the six remaining named follow-ons (user: "从1顺序实现到6"). FU3
> reclaims local-fs orphan bytes by dispatching each key to its WRITER executor;
> if the writer is offline, the key waits. FU4 closes that gap for NFS-shared
> local backends: any healthy executor that ACTUALLY HAS the backend's
> `base_path` mounted can reclaim the key — not just the writer.
> Status: self-approved per Rule #1. Branch: `feat/fu4-nfs-shared-reclaim`.

## 0. The safety hinge

FU3's confirm-on-missing is safe only because the WRITER wrote the file to its
own disk. Extending to siblings risks a false confirm: a sibling that does NOT
have the mount would `unlink(missing_ok=True)` → no-op → confirm → controller
deletes the ledger row while the bytes persist on the real writer. FU4's hinge:
**an executor advertises only base_paths it has VERIFIED exist as directories on
its host** (`os.path.isdir`). So "executor E advertises base_path P" ⟹ "E has P
mounted" ⟹ on a shared NFS mount the file is the SAME file the writer wrote ⟹
`unlink` is a real delete (or genuinely-already-gone by a sibling) = safe. A
false advertisement is only possible via operator misconfig of
`local_base_paths` pointing at a same-named-but-different dir — documented; the
path-traversal guard (FU3) still bounds every delete under `base_path`.

Double-dispatch (writer AND a sibling both get the key) is HARMLESS: idempotent
`unlink(missing_ok=True)` + the controller's confirm-delete is a no-op on an
already-deleted row. So FU4 needs NO "writer-not-healthy" gating — it simply
widens the candidate executors.

## 1. Scope

**In scope (additive; no migration — reuse `Executor.capabilities` JSONB; no new dep):**

1. **`ExecutorSettings.local_base_paths: list[str] = []`** — operator config: the
   local/NFS base_paths this executor can serve (env `DLW_EXECUTOR_LOCAL_BASE_PATHS`,
   pydantic parses a JSON list / comma list per its env conventions).
2. **Executor advertises verified base_paths** in the heartbeat:
   `ExecutorHeartbeat.accessible_base_paths: list[str] | None = None` (None = no
   update, backward-compat). The runner sends
   `[p for p in settings.local_base_paths if Path(p).is_dir()]` (existence-verified
   — the §0 hinge). Empty list is sent as `[]` (explicit "I serve no local paths").
3. **Controller stores it** on `Executor.capabilities["base_paths"]`
   (`record_heartbeat`: when `body.accessible_base_paths is not None`, set
   `ex.capabilities = {**ex.capabilities, "base_paths": body.accessible_base_paths}`
   — reassign so SQLAlchemy detects the JSONB change).
4. **`dispatch_local_reclaim` widens candidacy**: for the heartbeating executor E,
   return local orphan keys (past grace, `~live`) where `executor_id == E.id`
   (FU3 writer path) **OR** the key's backend `base_path` ∈ E's advertised
   `base_paths`. The handler passes E's advertised set (from `ex.capabilities`)
   into dispatch. Implementation: resolve the set of local `storage_id`s whose
   decrypted `base_path` ∈ E's advertised set (decrypt the few local backends in
   Python), then the candidate query is
   `(executor_id == E.id) OR (storage_id IN those_ids)`, still local-only +
   `~live` + grace + cap + `FOR UPDATE OF storage_physical_keys SKIP LOCKED`.

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

`services/storage_objects.py` `dispatch_local_reclaim` — add an optional
`accessible_base_paths: frozenset[str] = frozenset()` param; the handler passes
`frozenset(ex.capabilities.get("base_paths") or [])`. Build the sibling
`storage_id` set:
```python
    sibling_ids: set[int] = set()
    if accessible_base_paths:
        locals_ = (await session.execute(
            select(StorageBackend).where(StorageBackend.backend_type == "local"))
        ).scalars().all()
        for b in locals_:
            bp = storage_config_from_backend(b).base_path
            if bp and bp in accessible_base_paths:
                sibling_ids.add(b.id)
    writer_or_sibling = (StoragePhysicalKey.executor_id == executor_id)
    if sibling_ids:
        writer_or_sibling = writer_or_sibling | StoragePhysicalKey.storage_id.in_(sibling_ids)
    # candidate query: .where(writer_or_sibling, created_at<cutoff, ~live,
    #                         StorageBackend.backend_type=="local") ...
```
(The existing query already joins `StorageBackend` for `backend_type=="local"`;
add `writer_or_sibling` to its `.where(...)`. Keep the dedup `out` keyed by row
id so a key matched by both branches isn't emitted twice — `select` returns
distinct rows anyway since it's one row per key.)

The handler (`api/executors.py::post_heartbeat`) passes
`accessible_base_paths=frozenset((ex.capabilities or {}).get("base_paths") or [])`
into `dispatch_local_reclaim`. Confirm/unlink/path-guard are UNCHANGED (FU3).

## 3. Tests

- **`tests/services/test_local_reclaim.py`** (extend): seed a local backend
  (base_path `/srv/dlw`) + an orphan key written by `ex-writer`. (a) executor
  `ex-sibling` with `accessible_base_paths={"/srv/dlw"}` → `dispatch_local_reclaim(
  session, "ex-sibling", ..., accessible_base_paths=frozenset({"/srv/dlw"}))`
  returns the key (sibling reclaims a non-writer key). (b) `ex-sibling` with
  `accessible_base_paths=frozenset()` → does NOT get it (writer-only, FU3
  behavior preserved). (c) a sibling advertising a DIFFERENT base_path → not
  matched. (d) the writer still gets its own keys regardless of advertised set.
- **`tests/api/test_executors.py`** (extend): a heartbeat with
  `accessible_base_paths=["/srv/dlw"]` stores it on `ex.capabilities["base_paths"]`;
  a subsequent heartbeat (gc enabled) returns a sibling-reclaimable key in
  `reclaim`. A heartbeat omitting the field leaves `capabilities` unchanged
  (None = no update).
- **`tests/executor/test_runner.py`** (extend): the runner sends
  `accessible_base_paths` = its `local_base_paths` filtered by `is_dir` (configure
  one existing tmp dir + one bogus path → only the existing one advertised).
- Backward-compat: existing reclaim/heartbeat tests unchanged (new params default
  empty/None).

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
