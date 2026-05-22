# FU3 — Local-FS Physical Reclamation (executor-side) (Design)

> Third + final queued follow-on. Phase 4's physical-reclamation GC deletes
> orphaned bytes for **S3** backends directly from the controller, but SKIPS
> **local-filesystem** backends (the controller has no access to an executor's
> disk). FU3 adds an executor-side reclaim path: the controller dispatches
> orphaned local keys to the executor that WROTE them (over the existing 10 s
> heartbeat), the executor `unlink`s them, and confirms — so local-fs orphans
> are finally reclaimable. Safe-by-default (same `gc_delete_physical_bytes` gate).
> Status: self-approved per Rule #1. Branch: `feat/fu3-localfs-reclamation`.

## 0. The routing problem + the chosen design

The hard part: there is no durable executor↔key map, and the controller can't
reach an executor's disk. But for local (non-NFS) backends the v2.0 design
mandates **single-executor mode** (doc 13 §13: `switch_loss_factor=1.0`) — so a
given local key was written by exactly ONE executor, on exactly ONE host. FU3
**records that writer `executor_id` on the physical-key ledger** at completion,
then dispatches each orphaned local key back to its writer (if currently
healthy) for deletion. The dispatch rides the existing heartbeat (fires every
10 s, reaches idle executors, existing mTLS+JWT+HMAC+epoch auth); confirmation
rides the next heartbeat. Idempotent throughout (re-dispatch within the 10 s
gap → executor re-`unlink`s an absent file = success = re-confirm = controller
re-deletes an absent row = no-op).

**Honest scope:** correct for single-executor-local (the documented model). An
NFS-shared backend where the writer executor is down while a sibling shares the
mount is NOT handled — the key waits until the writer host returns (or stays
orphaned). Documented; a base-path-capability advertisement (any-mount-holder
deletes) is a larger follow-on. As with Phase 4, this is OFF by default
(`gc_delete_physical_bytes=false`).

## 1. Scope

**In scope (one migration — a nullable column; no new table, no new dep):**

1. **Migration**: add `storage_physical_keys.executor_id` (`String(64)`,
   nullable) — the writer executor. down_revision `c4d5e6f7a8b9`. Nullable, no
   server_default → no `compare_server_default` drift. `EXPECTED_TABLES`
   unchanged (column add, not table). Register nothing new.
2. **`record_physical_key`** gains `executor_id: str | None = None`; stored on
   the row. `complete_subtask` passes `sub.executor_id` (already set to the
   writer at `scheduler.py:95`).
3. **Heartbeat protocol** (additive, backward-compatible):
   - `ExecutorHeartbeat` (request): `reclaimed_key_ids: list[int] = []` — ledger
     row ids the executor deleted (or found already absent) since last beat.
   - `ExecutorRead` (response): `reclaim: list[ReclaimItem] = []` where
     `ReclaimItem = {id: int, base_path: str, storage_key: str}` — local keys for
     THIS executor to delete now.
   - New schema `ReclaimItem`.
4. **Heartbeat handler / `record_heartbeat`**: (a) CONFIRM — delete
   `storage_physical_keys` rows in `reclaimed_key_ids` scoped to
   `executor_id == this executor` (idempotent; audit `storage.gc.physical.local`);
   (b) DISPATCH — if `gc_delete_physical_bytes`, select ≤ `gc_max_objects_per_tick`
   local-fs orphan keys for this executor (join `StorageBackend` on `storage_id`
   where `backend_type=="local"`, `~live`, `created_at < grace cutoff`,
   `executor_id == this executor`, `with_for_update(skip_locked=True)`), resolve
   each backend's `base_path` (decrypt `config_encrypted`), return as `reclaim`.
   `grace = max(gc_grace_seconds, gc_archive_after_days*86400)` (same as the S3 loop).
5. **Executor heartbeat loop**: after each heartbeat, for each `reclaim` item
   `os.unlink(Path(base_path) / storage_key)` (best-effort; `FileNotFoundError`
   = already gone = success; other `OSError` = skip, retry next beat); buffer the
   succeeded ids; send them as `reclaimed_key_ids` in the NEXT heartbeat.
6. **`client.heartbeat`** gains `reclaimed_key_ids` → body dict (HMAC covers the
   new body; an old executor omitting it → handler defaults `[]`).

**Out of scope (named):**
- NFS-shared multi-executor where the writer is down — deferred (writer-targeted
  only). Base-path-capability advertisement is the follow-on.
- The controller-side S3 `reclaim_physical_orphans` loop is UNCHANGED (still
  skips local — now the heartbeat path handles local). The two are complementary:
  S3 via the leader loop, local via per-executor heartbeats.
- Backfilling `executor_id` for local keys recorded before this migration (they
  have `executor_id=NULL` → never dispatched). A reconcile is a follow-on; new
  keys carry the writer.

## 2. Data model + recording

Migration `..._fu3_phys_key_executor.py`:
```python
def upgrade():
    op.add_column("storage_physical_keys",
                  sa.Column("executor_id", sa.String(64), nullable=True))
def downgrade():
    op.drop_column("storage_physical_keys", "executor_id")
```
Model `StoragePhysicalKey`: `executor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)`.
`record_physical_key(..., executor_id: str | None = None)` → include in the
`pg_insert(...).values(...)`. `complete_subtask` call: add
`executor_id=sub.executor_id`.

## 3. Heartbeat handler (`api/executors.py` + `executor_service.record_heartbeat`)

The handler currently: `ex = await record_heartbeat(session, executor.id, body)`
→ `ExecutorRead.model_validate(ex)`. FU3 wraps this:
```python
    from dlw.config import get_settings           # NOT _gs (that alias is main.py-local)
    from dlw.services.audit import write_audit
    s = get_settings()
    ex = await record_heartbeat(session, executor.id, body)   # unchanged liveness

    # write_audit's real signature is keyword-only: (session, *, action,
    # resource_type, resource_id, outcome, tenant_id, actor_user_id, payload).
    # confirm_local_reclaim calls audit(action=, id=, tenant_id=, storage_key=,
    # size=); adapt it here (the long key goes in payload, NOT resource_id which
    # is String(128) — same lesson as Phase 4 B1).
    async def _audit(*, action, id, tenant_id, storage_key, size):
        await write_audit(session, action=action,
                          resource_type="storage_physical_keys",
                          resource_id=str(id), outcome="success",
                          tenant_id=tenant_id, actor_user_id=None,
                          payload={"storage_key": storage_key, "size": size})

    if body.reclaimed_key_ids:               # FU3 confirm (delete scoped rows)
        await confirm_local_reclaim(
            session, executor.id, body.reclaimed_key_ids, audit=_audit)
    reclaim: list[ReclaimItem] = []          # FU3 dispatch
    if s.gc_delete_physical_bytes:
        reclaim = await dispatch_local_reclaim(
            session, executor.id,
            grace_seconds=max(s.gc_grace_seconds,
                              s.gc_archive_after_days * 86400),
            limit=s.gc_max_objects_per_tick)
    await session.commit()
    return ExecutorRead.model_validate(ex).model_copy(update={"reclaim": reclaim})
```
(`confirm_local_reclaim`'s `audit` param is now an async callable; make
`confirm_local_reclaim` `await audit(...)`. `dispatch_local_reclaim` returns
`list[ReclaimItem]` — see below — so `model_copy(update={"reclaim": reclaim})`
serializes the nested models cleanly, no pydantic dict-coercion warning.)
New service fns in `services/storage_objects.py` (near `reclaim_physical_orphans`):
```python
async def confirm_local_reclaim(session, executor_id, key_ids, *, audit) -> int:
    rows = (await session.execute(
        select(StoragePhysicalKey).where(
            StoragePhysicalKey.id.in_(key_ids),
            StoragePhysicalKey.executor_id == executor_id))).scalars().all()
    for r in rows:
        await audit(action="storage.gc.physical.local", id=r.id,
                    tenant_id=r.tenant_id, storage_key=r.storage_key, size=r.size)
        await session.delete(r)
    return len(rows)

async def dispatch_local_reclaim(session, executor_id, *, grace_seconds, limit):
    """Local-fs orphan keys written by this executor, past grace. Returns
    list[ReclaimItem] — base_path resolved from the backend config (a backend
    whose config lacks base_path is SKIPPED, not dispatched — local backends
    MUST carry base_path in config_encrypted). Import ReclaimItem from
    dlw.schemas.executor."""
    from dlw.db.models.storage import StorageBackend
    from dlw.services.storage_client import storage_config_from_backend
    cutoff = datetime.now(UTC) - timedelta(seconds=grace_seconds)
    live = exists().where(
        StorageObject.tenant_id == StoragePhysicalKey.tenant_id,
        StorageObject.storage_id == StoragePhysicalKey.storage_id,
        StorageObject.sha256 == StoragePhysicalKey.sha256)
    rows = (await session.execute(
        select(StoragePhysicalKey)
        .join(StorageBackend, StorageBackend.id == StoragePhysicalKey.storage_id)
        .where(StoragePhysicalKey.executor_id == executor_id,
               StoragePhysicalKey.created_at < cutoff, ~live,
               StorageBackend.backend_type == "local")
        .order_by(StoragePhysicalKey.created_at)
        .limit(max(1, limit))
        .with_for_update(skip_locked=True, of=StoragePhysicalKey))).scalars().all()
    out, cache = [], {}
    for r in rows:
        if r.storage_id not in cache:
            b = await session.get(StorageBackend, r.storage_id)
            cache[r.storage_id] = (storage_config_from_backend(b).base_path
                                   if b else None)
        base = cache[r.storage_id]
        if base:
            out.append(ReclaimItem(id=r.id, base_path=base,
                                   storage_key=r.storage_key))
    return out
```
(`audit` here writes via the handler's `write_audit` — same isolated best-effort
pattern as the S3 loop; the handler builds an `audit` closure that appends, then
writes after commit, OR writes inline before commit. Keep it simple: write the
audit rows in the same transaction as the deletes — local reclaim is low-volume.)
The `confirm` happens BEFORE the heartbeat commit; `dispatch` reads in the same
session. The dispatch `with_for_update(skip_locked, of=StoragePhysicalKey)` keeps
two controllers (active/standby both reachable) from handing the same key to two
executors — though they'd both target the SAME writer executor anyway, so it's
belt-and-suspenders.

## 4. Executor side (`runner.py` + `client.py`)

`client.heartbeat(..., reclaimed_key_ids: list[int] | None = None)` → add to
`body_dict` when non-empty. Returns the response dict (now possibly carrying
`reclaim`).

`runner._heartbeat_loop`: keep `self._pending_reclaim_confirms: list[int] = []`.
Each iteration: pass `reclaimed_key_ids=self._pending_reclaim_confirms` to
`heartbeat(...)`, then clear it; read `resp.get("reclaim") or []`; for each item
`{id, base_path, storage_key}` apply a **path-traversal guard** before deleting
(pre-review BLOCKER — `storage_key` round-trips through the DB and becomes a
delete instruction under the executor's often-root FS creds; a corrupt/tampered
key like `../../etc/x` or an absolute `/abs` must NOT escape `base_path`):
```python
    from pathlib import Path

    def _safe_target(base_path: str, storage_key: str) -> Path | None:
        if not base_path:
            return None
        base = Path(base_path)
        if not base.is_absolute():        # reject relative/"." bases (inherit's default)
            return None
        resolved = (base / storage_key).resolve()
        try:
            if not resolved.is_relative_to(base.resolve()):   # py3.9+
                return None
        except ValueError:
            return None
        return resolved

    # ... in the loop, per item:
    target = _safe_target(item.get("base_path", ""), item.get("storage_key", ""))
    if target is None:
        logger.warning("local reclaim: refusing unsafe target base=%r key=%r",
                       item.get("base_path"), item.get("storage_key"))
        continue                          # do NOT confirm — the row stays
    try:
        target.unlink(missing_ok=True)    # absent = already reclaimed = success
        confirmed.append(item["id"])
    except OSError as e:
        logger.warning("local reclaim unlink failed %s: %s", target, e)  # retry next beat
    self._pending_reclaim_confirms = confirmed
```
(`unlink(missing_ok=True)` — a missing file is success: the goal is "ensure
gone". The guard refuses any target that escapes `base_path` or has an
empty/relative base (and does NOT confirm those, so the controller never deletes
the ledger row for an unsafe key — it surfaces as an un-reclaimable key the gauge
in §3 reports). Other OSError → don't confirm → re-dispatched. Never crashes the
loop.)

## 5. Tests

- **`tests/services/test_local_reclaim.py`**: `dispatch_local_reclaim` returns
  only this executor's local-fs orphan keys past grace (not S3 keys, not other
  executors', not non-orphan, not within grace), with the right `base_path` from
  the backend config; `confirm_local_reclaim` deletes only rows scoped to the
  executor + returns the count; cross-executor confirm (wrong executor_id) → no
  delete.
- **`tests/api/test_executors.py`** (extend): a heartbeat with
  `reclaimed_key_ids=[...]` deletes those rows (scoped); a heartbeat when
  `gc_delete_physical_bytes` is enabled + a seeded local orphan for this executor
  → response carries a `reclaim` item with the right base_path/key; disabled →
  empty `reclaim`. (Use the existing `register_test_executor`/
  `signed_heartbeat_headers` helpers; set the gc config via monkeypatch/env.)
- **`tests/executor/test_runner.py`** (extend): a mock heartbeat returning a
  `reclaim` item pointing at a tmp file → the runner `unlink`s it and sends the
  id as `reclaimed_key_ids` on the next heartbeat; a missing file → still
  confirmed (success); the heartbeat-loop unit test asserts the unlink + the
  next-beat confirm. (Mirror the `MagicMock(spec=ControllerClient)` pattern.)
- **`record_physical_key`** test (extend `test_physical_keys.py`): `executor_id`
  is stored.
- `tests/db/test_alembic.py` — EXPECTED_TABLES unchanged (column add); the
  upgrade/downgrade round-trip still passes.

## 6. Milestones

- **M1 — ledger + dispatch/confirm service**: migration + model + `record_physical_key`
  executor_id + `complete_subtask` wiring + `dispatch_local_reclaim`/
  `confirm_local_reclaim` + service tests + dev-DB upgrade + drift gate + backend gate.
- **M2 — heartbeat protocol + handler**: `ExecutorHeartbeat.reclaimed_key_ids` +
  `ExecutorRead.reclaim` + `ReclaimItem` + handler confirm/dispatch + API tests +
  backend gate.
- **M3 — executor side**: `client.heartbeat` reclaimed_key_ids +
  `runner._heartbeat_loop` unlink+confirm + runner tests + docs note in
  `docs/operator/storage-reclamation.md` (local-fs now reclaimed via the writer
  executor over heartbeat; single-executor-local only; NFS-down-writer deferred;
  same `gc_delete_physical_bytes` gate) + full backend gate.

## 7. Risks & Contingencies

- **Destructive (deletes executor-disk bytes)** — same safe-by-default rails as
  Phase 4: OFF unless `gc_delete_physical_bytes`; grace; `~live` (sha fully
  dereferenced); targeted to the WRITER executor (the host that has the file);
  `unlink(missing_ok=True)` (idempotent); audited (`storage.gc.physical.local`);
  capped per beat. The confirm-then-delete-ledger order means a crash leaves the
  ledger row → re-dispatched (re-unlink absent = success). No silent data loss.
- **Confirm-on-missing safety (honest framing, pre-review).** Dispatch is scoped
  to `executor_id == writer`; `executor_id` is the executor's config-supplied id,
  STABLE across re-register (register is ON CONFLICT on `id`, bumping only epoch),
  so the writer survives restarts and writer-targeting is durable. The writer
  wrote the file to its OWN disk under `base_path`, so `unlink(missing_ok=True)`
  is a real delete (or genuinely-already-gone). The ONE leak vector: if the
  writer's `base_path` is later REMAPPED to a different filesystem (config edit /
  remount), an `unlink(missing_ok=True)` no-ops while the real bytes persist
  elsewhere → confirmed gone → ledger row deleted → untracked orphan. This is
  accepted because local-fs is single-host and rarely reconfigured — but it is a
  real edge, NOT impossible. (Do NOT cite doc 13's "single-executor mode" as a
  placement *invariant* — it is a v2.1 scheduling COST factor; doc 04 §291 says
  local/NFS access is by path permission, so a path CAN be shared/remapped.) The
  path-traversal guard (§4) further ensures only files UNDER the current
  `base_path` are ever touched.
- **Observability for un-reclaimable keys.** Keys whose `executor_id` points at a
  GONE (deactivated) executor, keys with `executor_id=NULL` (pre-migration), and
  keys the guard refused are never reclaimed. The dispatch path logs a periodic
  gauge (a `logger.info` count, or an audit `storage.gc.physical.local.stuck`)
  of local orphan keys not dispatchable, so an operator sees accruing
  un-reclaimable bytes rather than silent growth. (Lightweight: the heartbeat
  handler or the existing leader GC loop counts `local phys keys, ~live, past
  grace, executor_id NULL or pointing at a deactivated executor` and logs it.)
- **HMAC body change**: adding `reclaimed_key_ids` to the heartbeat body — the
  executor signs the new body; the controller validates the same bytes. An old
  executor (no field) → handler defaults `[]`. Backward-compatible.
- **Heartbeat does more DB work now** — low volume (per-executor 10 s); the
  dispatch query is indexed-ish (filters executor_id + created_at; the existing
  `idx_phys_key_gc` is on tenant/storage/sha — a new index isn't required for the
  modest row counts, but note it as a future optimization if local backends grow).
- **No leader-gate on heartbeat dispatch** — acceptable: gated by
  `gc_delete_physical_bytes`, `with_for_update(skip_locked)`, idempotent confirm;
  even if both controllers serve heartbeats they target the same writer + don't
  double-delete.
- **Migration**: nullable column add, down_revision `c4d5e6f7a8b9`, drift gate
  clean (nullable, no server_default), dev-DB upgrade, EXPECTED_TABLES unchanged.
- No openapi change (executor endpoints aren't in the static spec — verify), no
  frontend. CI gate = pytest + `lint_invariants`.

## 8. Self-Review

- **Closes the local-fs gap**: orphaned local keys reclaimed by their writer
  executor over heartbeat; Phase 4's named follow-on delivered. ✓
- **Safe-by-default destructive**: OFF by default, writer-targeted, grace,
  ~live, idempotent unlink, audited, confirm-before-ledger-delete. ✓
- **Honest deferrals**: NFS-down-writer + pre-migration backfill named. ✓
- **Additive protocol**: heartbeat fields default-empty, backward-compatible;
  the S3 loop unchanged. ✓
- **Consistency**: reuses the `storage_physical_keys` ledger, `~live`/grace
  semantics, `gc_delete_physical_bytes`, `storage_config_from_backend`, the
  heartbeat auth + loop, the audit pattern.
