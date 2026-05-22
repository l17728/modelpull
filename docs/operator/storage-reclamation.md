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
Local-filesystem backends are skipped — the controller has no access to an
executor's disk in a distributed deployment — and their reclamation is a
named follow-on (an executor-side reclaim path).

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
- `created_at` (ledger-write time) is the available proxy for the design's
  `last_referenced_at`; a true per-key last-access column is a follow-on.

Cold-storage tiering and reclamation of physical objects written before the
ledger shipped (which have no ledger row) are also follow-ons.
