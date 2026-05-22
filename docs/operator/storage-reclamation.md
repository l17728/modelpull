# Storage Reclamation (Phase 4)

This page documents the storage-quota accounting and physical-byte
reclamation introduced in Phase 4. It closes three gaps left open by the
SP3 incremental-download work: storage quota was never computed or
enforced, incremental "inherit" copies left untracked physical objects,
and the garbage collector deleted only database rows, never the bytes on
the object store.

## Storage quota accounting and enforcement

Each tenant has a `quota_storage_gb` limit. The minute-interval quota
aggregator now computes `storage_gb_used` for every tenant by summing the
sizes of that tenant's deduplicated `storage_objects` rows (each unique
piece of content is counted once, consistent with the global-dedup
invariant) and dividing by one GiB using floor division — a tenant under
one GiB shows zero. When a tenant creates a new download task, the quota
check blocks the task if `storage_gb_used` is greater than or equal to
`quota_storage_gb`. A tenant whose `quota_storage_gb` is zero or unset is
treated as unlimited, matching the existing monthly-bytes behavior. The
`/quota/current` API already surfaced `storage_gb_used` and
`storage_gb_quota`; those values are now live rather than always zero.

## The physical-key ledger

Incremental upgrades reuse already-downloaded content: when a file is
unchanged between two revisions, the executor server-side-copies the
existing object to the new revision's key (an S3 `copy_object` or a local
hard link) instead of downloading it again. The deduplication table keys
on content hash and only records the first writer's key, so the
new-revision physical object was previously untracked and could never be
reclaimed.

Phase 4 adds a durable `storage_physical_keys` ledger that records every
physical object key actually written — for both fresh downloads and
inherit copies — at subtask completion. This is recorded controller-side
from the key the subtask reported, so no executor change was required. The
ledger is decoupled from the deduplication table: it is the authoritative
record of bytes on the store.

## Physical reclamation

A new leader-gated background loop reclaims physical bytes. A physical key
is a reclamation candidate when its content hash has no surviving
`storage_objects` row — meaning the content is fully dereferenced and the
deduplication row has already been removed by the existing row-level GC —
and the ledger row is older than the grace window. The grace window is the
larger of `gc_grace_seconds` and `gc_archive_after_days` (default 90 days),
so reclamation only ever runs after the row-level GC has deleted the
deduplication row, never while it is still inside its own grace.

Reclamation is destructive and is therefore safe by default:

It is disabled unless the operator sets `gc_delete_physical_bytes` to true.
While disabled, the loop still runs and logs how many candidates it would
reclaim each tick, so an operator can observe the prospective blast radius
before enabling deletion.

It runs only for S3-type backends. The controller builds an S3 client from
the backend configuration (the same pattern the recovery routine uses).
Local-filesystem backends are handled separately via the executor heartbeat
path described below.

Each tick is capped at `gc_max_objects_per_tick` (default 1000) so that
enabling the feature on a large backlog does not attempt to delete an
unbounded number of objects in a single pass.

Every physical deletion is audited as `storage.gc.physical` (the object key
is recorded in the audit payload). Deletion is ordered bytes-before-row: the
object-store delete happens first, and only then is the ledger row removed,
so a crash between the two leaves a re-discoverable ledger row that is
retried on the next tick (re-deleting an already-absent key is a no-op).

## What is intentionally not reclaimed

Content with a live reference (refcount greater than zero) is never
touched; only fully dereferenced content is eligible.

## Quota-pressure prioritization

The reclaim loop orders candidates so that tenants at or over a storage
pressure threshold (`gc_quota_pressure_threshold`, default 0.9 — i.e. ≥90%
of `quota_storage_gb`) have their dereferenced keys reclaimed first; within
a group, oldest-first by ledger `created_at`. Be aware of what this does and
does not do:

- It only changes behavior **when the per-tick cap binds** — a reclaim
  backlog larger than `gc_max_objects_per_tick`. In normal operation (backlog
  ≤ cap) every eligible key is reclaimed each tick regardless of order, so the
  prioritization has no observable effect.
- It prioritizes freeing a pressured tenant's already-**orphaned disk bytes**;
  it does NOT lower that tenant's `storage_gb_used`, because a key is only
  eligible once its content's `storage_objects` row is already gone (those
  bytes are no longer counted in the quota). It is a disk-relief priority
  heuristic, not a quota-number reducer.
- The platform design's eviction of *tracked* refcount-0 `storage_objects`
  rows remains **time-only** (the row-level GC); this prioritization does not
  make that quota-aware.
- The pressure check uses integer-GiB `storage_gb_used` vs `quota_storage_gb`,
  so it is coarse and effectively inert below roughly 10-GiB quotas.
Physical keys now carry a real `last_referenced_at` (FU5). It is bumped whenever
the content is referenced again — a re-download to the same key, or a later
revision inheriting the same content via the dedup path — so reclamation orders
orphans **least-recently-referenced first** and measures the grace window **from
the last reference, not the ledger write**. The grace change is strictly more
conservative: `last_referenced_at` is always at or after `created_at`, so bytes
are deleted no sooner than they were before FU5. Caveats remain: the signal only
diverges from write-time for content re-referenced during its live lifetime
(single-use content has `last_referenced_at == created_at`); the ordering only
has an observable effect when the per-tick cap binds; and the *tracked*
refcount-0 `storage_objects` eviction (`gc_orphans`) is still time-only — FU5
sharpens only the physical-orphan ordering and grace.

Cold-storage tiering and reclamation of physical objects written before the
ledger shipped (which have no ledger row) are also follow-ons.

## Local-filesystem reclamation (FU3)

The S3 reclamation loop runs entirely inside the controller process, which
has no access to an executor's local disk. Local-filesystem orphan reclamation
therefore runs via the executor that originally wrote each file — the WRITER
executor — over the existing heartbeat channel.

**How it works.** The controller's heartbeat handler checks for orphaned
local-fs physical keys assigned to the calling executor (same `~live`,
grace window, and `gc_delete_physical_bytes` gate as the S3 loop; capped
at `gc_max_objects_per_tick` per heartbeat). Eligible keys are returned in
the heartbeat response as `reclaim: [{id, base_path, storage_key}]`. The
executor calls `unlink` on each file (using `missing_ok=True` so an already
absent file is treated as success) and reports the completed ids back via
`reclaimed_key_ids` in the next heartbeat. The controller then removes the
confirmed ledger rows and audits them as `storage.gc.physical.local`. The
confirm-then-delete-ledger ordering means a crash between unlink and confirm
leaves the row intact and the key is re-dispatched on the next heartbeat
(re-unlinking an absent file is a no-op).

**Path-traversal guard.** Before unlinking, the executor resolves the full
path of `base_path / storage_key` and rejects it if the result escapes
`base_path` (for example a key containing `../../etc/passwd`), if
`base_path` is empty, or if `base_path` is not an absolute path. Refused
keys are logged as warnings and are never confirmed, so the ledger row
persists and surfaces in the un-reclaimable count rather than disappearing
silently.

**Scope and deferrals.** This path is correct for the documented
single-executor-local deployment model, where a given local key was written
by exactly one executor on exactly one host.

### NFS-shared backends (FU4)

For an NFS-shared local backend whose writing executor is offline, reclamation
is no longer blocked: **any healthy executor that advertises the backend's
`base_path` can reclaim its keys**, not just the writer. An executor advertises
its accessible base paths by setting `DLW_EXECUTOR_LOCAL_BASE_PATHS` (a JSON
list, e.g. `["/srv/dlw"]`); at each heartbeat it sends only the paths that
actually exist as directories on its host (`is_dir`-verified), and the
controller records them. Dispatch and confirmation both authorize a key for an
executor when it is the writer **or** the key's backend is among the executor's
advertised paths.

The safety envelope (this is destructive — it deletes bytes on executor disks):

- The executor advertises only `is_dir`-verified paths — it claims access only
  to mounts it actually has.
- Before deleting, the executor verifies the file's **size** matches the ledger;
  a present file of the wrong size is refused (and not confirmed), guarding the
  same-named-but-different-mount misconfiguration.
- The path-traversal guard still bounds every delete strictly under `base_path`.
- Dispatch and confirmation are tenant-scoped (a no-op today, since executors are
  system-scoped, but it future-proofs against tenant-pinned executors).
- **Hard operator constraint:** each local backend's `base_path` must be unique
  — two backends sharing a `base_path` string would be conflated.

Honest note: local-filesystem is a minor backend (written only by incremental
inherit copies today), so this NFS-shared, writer-offline path is a narrow case
shipped for parity. A residual: if an executor's advertised set shrinks between
the dispatch and confirm heartbeats, a row may linger one extra cycle before any
remaining mount-holder re-clears it (self-healing; surfaced by the gauge below).

### Still deferred

- *Keys recorded before FU3 shipped.* Physical keys written before the
  `executor_id` column was added carry `NULL` for the writer; they are dispatched
  only if a live executor advertises their backend's base path (FU4 helps some),
  otherwise a back-fill reconciliation is a named follow-on.

Un-reclaimable keys are reported by the periodic count logged by the heartbeat
handler so operators can observe accruing bytes rather than silent growth.
