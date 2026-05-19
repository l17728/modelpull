# Incremental Download + Global Dedup — Operator Guide (SP3)

> **Cross-references**: `docs/v2.0/06-platform-and-ecosystem.md` §2 (incremental /
> diff design) and §3.1–§3.3 (global dedup, refcount, delete dependency);
> `docs/v2.0/INVARIANTS.md` 14 (one physical copy per tenant + backend + content).

---

## 1. `upgrade_from_revision` — sha-diff against what already exists

When a task is created with `upgrade_from_revision` set (the prior git sha),
the scheduling phase runs `diff_and_dedup` **before** source planning. For each
still-`pending` subtask whose HuggingFace `expected_sha256` already has a
`storage_objects` row for `(tenant_id, storage_id, sha256)`, the subtask is
flipped to status `inherit` and a `subtask_object_refs` row + a refcount
increment are recorded immediately. Only files whose content actually changed
(new sha) stay `pending` and flow through the normal SP2 multi-source planner.

This is **unified with cross-task dedup**: the lookup is purely by content
sha, so a file identical to one already stored by *any* prior task or revision
(not just the named `upgrade_from_revision`) is inherited too. There is no
separate "dedup mode" — one code path covers both.

## 2. `storage_objects` refcount model (INVARIANT 14)

`storage_objects` has `UNIQUE(tenant_id, storage_id, sha256)` — there is at
most **one** physical copy of a given content blob per tenant per storage
backend. Every subtask that resolves to that content holds a
`subtask_object_refs` row; `refcount` is the number of live references.

- `record_object` (on download success) upserts the row and adds a ref —
  but it is a **no-op when a ref for that subtask already exists** (the
  inherit path already added one in `diff_and_dedup`; this prevents a
  double-count).
- `deref_subtask` removes a subtask's ref and decrements `refcount`.

## 3. Inherit materialization (no re-download)

An `inherit` subtask is claimed like any other, but the executor does **not**
fetch source bytes. Instead `materialize_inherit` performs:

- **S3 backend**: a server-side `copy_object` (in-region, ≈ free, no egress).
- **local backend**: `os.link` (hardlink), falling back to a copy on `EXDEV`.

It then reports success with the file's known sha — no HuggingFace or mirror
traffic is generated for inherited files.

## 4. `DELETE /api/v1/tasks/{id}`

- **Terminal-only**: the task must be `succeeded`, `failed`, or `cancelled`.
  A non-terminal task returns **409** `{"code": "TASK_NOT_TERMINAL"}`.
- **Tenant-scoped + RBAC-gated**: a cross-tenant id returns 404 (existence is
  not leaked); unauthenticated returns 401.
- On success (**204**) every subtask of the task is dereferenced
  (`refcount--`) and the task row is deleted (FK cascade removes subtasks and
  their object refs).
- It does **not** delete physical bytes — only DB references. Reclamation is
  the GC's job (§5).

## 5. Leader-gated GC

The active controller runs a background GC loop (standby controllers do not):

- `DLW_GC_INTERVAL_SECONDS` (default 60) — how often a GC tick runs.
- `DLW_GC_GRACE_SECONDS` (default 3600) — a `storage_objects` row is only
  reclaimed once it has been at `refcount = 0` for at least this long.

Each reclaiming tick emits an audited `storage.gc` event (system-scope:
`tenant_id=null`, `actor_user_id=null`) with `{"reclaimed": <n>}`.

### Inherit-copy-failure self-heal

If an inherit copy fails on the executor, `complete_subtask` undoes the
diff-time `refcount++` (via `deref_subtask`), clears `inherit_from_key`, and
re-queues the subtask as `pending` so it is downloaded normally on the next
scheduling pass. A failed inherit therefore never leaks refcount and never
strands a file.

## 6. Scope / deferred to Phase 4

SP3's GC only frees `refcount = 0` **database rows** past the grace window.

> An inherited file's `storage_objects` row tracks the *original* (source)
> key; the executor's server-side copy creates new-revision-key bytes that
> are NOT tracked by any `storage_objects` row — these are orphaned bytes
> reclaimed in Phase 4 (physical GC); SP3's GC only frees refcount=0 DB rows.

Also deferred to Phase 4: physical S3 / filesystem byte reclamation, and
quota- or LRU-driven eviction of cold content.
