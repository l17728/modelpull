# SP2 — Chunk-Range Multi-Source Alignment (Design)

> Wires the SP2-deferred gap: the executor must download **one HTTP Range per
> `subtask_chunks` row** (using the row's `byte_start/byte_end`), so each Range
> maps to exactly one source and the source-proxy's per-chunk routing actually
> aligns. Today the executor splits by its own local `chunk_size_bytes` (16 MiB),
> which slices across the controller's 64-MiB per-source chunk boundaries → some
> Ranges cross source boundaries → wrong source for part of the range →
> whole-file SHA256 mismatch → blacklist + HF-refetch (safe, but multi-source
> chunk-level acceleration never lands).
> Authoritative design: SP2 spec ruling 6b + doc 02 §protocol `chunk_plan` +
> doc 03 §6.2 (multi-source = post-merge whole-file rehash, "mode B").
> Status: self-approved per Rule #1. Branch: `feat/sp2-chunk-range-alignment`.

## 1. Scope

**In scope (additive; no migration — `subtask_chunks` already exists; no new dep):**

1. **Carry chunk boundaries in the poll payload.** Add `chunks:
   list[ChunkAssignment]` to `SubTaskRead` (`schemas/subtask.py`), where
   `ChunkAssignment = {chunk_index:int, byte_start:int, byte_end:int,
   source_id:str}`. Empty for non-chunked subtasks.
2. **`post_poll`** (`api/executors.py`): when the claimed `sub.is_chunked`, query
   its `subtask_chunks` rows ordered by `chunk_index`, serialize into
   `SubTaskRead.chunks`. (`SubTaskRead.model_validate(sub)` won't auto-include
   them — they're a separate table; fetch + set explicitly.)
3. **Executor `Assignment`** (`executor/types.py`): add `chunks:
   tuple[ChunkAssignment, ...] = ()` (frozen dataclass → tuple). The runner
   builds it from `subtask.get("chunks")`.
4. **Runner downloader choice** (`runner.py::_choose_downloader`): if the
   assignment has chunks, force the `DirectOffsetDownloader` (chunked subtasks
   must use Range pulls regardless of the local file-size threshold).
5. **`DirectOffsetDownloader.download`** (`chunk_downloader.py`): when
   `assignment.chunks` is non-empty, build the `ChunkPlan` list from the rows
   (`offset = byte_start`, `length = byte_end - byte_start + 1`,
   `index = chunk_index`) instead of `plan_chunks(file_size, chunk_size_bytes)`.
   Everything downstream is unchanged: `_download_one_chunk` already builds
   `bytes={offset}-{offset+length-1}`, so each Range now equals one chunk row →
   the proxy (`source_proxy.py`) routes it to that chunk's source; `_pass2_upload`
   concatenates parts in index (offset) order → correct whole-file SHA256 →
   existing W4 gate verifies it.

**Defensive fallback:** if `assignment.chunks` is present but the rows do NOT
contiguously tile `[0, file_size-1]` (gap/overlap/wrong total — should never
happen, the controller's `_split_chunks` covers the full range), log a warning
and fall back to `plan_chunks`. The whole-file SHA gate is the backstop either
way; the fallback just avoids a guaranteed-failed multi-source attempt on
malformed input.

**Out of scope (named, deferred):**
- **Per-chunk SHA verification** (`subtask_chunks.sha256_partial`) — doc 03 §6.2
  mandates whole-file rehash for multi-source (mode B), which the existing
  sequential `_pass2_upload` SHA256 already provides. BLAKE3 streaming per-chunk
  hash is a v2.2 item (doc 08). `sha256_partial` stays unwritten.
- **Executor per-chunk status reporting** (`subtask_chunks.status`
  pending→downloading→done from the executor): the executor reports
  subtask-level success/failure as today; chunk `status` stays controller-managed
  (the rebalance loop reassigns `pending` chunks of a blacklisted source). A
  mid-flight reassignment doesn't affect an already-claimed subtask (its
  chunk→source map is baked into the Assignment at poll time). Wiring per-chunk
  progress is a follow-on.
- **per-executor source probing / 5xx-health blacklist transitions** (the other
  SP2 v2.1 deferral) — unrelated to Range alignment.

## 2. Schema (`schemas/subtask.py`)

```python
class ChunkAssignment(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    chunk_index: int
    byte_start: int
    byte_end: int          # inclusive
    source_id: str

class SubTaskRead(BaseModel):
    ...                    # existing fields unchanged
    chunks: list[ChunkAssignment] = Field(default_factory=list)
```
`AssignmentResponse` (`schemas/executor.py`) is unchanged — `chunks` rides inside
its `subtask: SubTaskRead`. No openapi.yaml change is required for the runtime
schema, but `SubTaskRead` appears in the static `api/openapi.yaml` — **verify**:
if the static spec defines `SubTaskRead`, add the optional `chunks` array there
too (no literal `null` examples; an empty-array default). If it's not referenced,
no openapi change.

## 3. `post_poll` (`api/executors.py`)

After `sub_read = SubTaskRead.model_validate(sub)` and before the commit:
```python
    if sub.is_chunked:
        rows = (await session.execute(
            select(SubtaskChunk).where(SubtaskChunk.subtask_id == sub.id)
            .order_by(SubtaskChunk.chunk_index))).scalars().all()
        sub_read = sub_read.model_copy(update={"chunks": [
            ChunkAssignment.model_validate(c) for c in rows]})
```
(Pydantic v2 `model_copy(update=...)`. `SubtaskChunk` import from
`dlw.db.models.source`.)

## 4. Executor `Assignment` + runner

`executor/types.py`:
```python
@dataclass(frozen=True)
class ChunkAssignment:
    chunk_index: int
    byte_start: int
    byte_end: int
    source_id: str

@dataclass(frozen=True)
class Assignment:
    ...                                  # existing fields
    chunks: tuple[ChunkAssignment, ...] = ()
```
`runner.py` (build Assignment): 
```python
    raw_chunks = subtask.get("chunks") or []
    chunks = tuple(
        ChunkAssignment(chunk_index=c["chunk_index"], byte_start=c["byte_start"],
                        byte_end=c["byte_end"], source_id=c["source_id"])
        for c in raw_chunks)
    assignment = Assignment(..., chunks=chunks)
```
`_choose_downloader` becomes chunk-aware (called with the assignment, or a
`has_chunks` arg):
```python
    def _choose_downloader(self, file_size, *, has_chunks=False):
        if has_chunks:
            return self._chunk_downloader
        threshold = self._s.chunk_level_threshold_bytes
        if file_size is None or file_size >= threshold:
            return self._chunk_downloader
        return self._stream_downloader
```
Call site: `downloader = self._choose_downloader(assignment.file_size,
has_chunks=bool(assignment.chunks))`.

## 5. `DirectOffsetDownloader.download` (`chunk_downloader.py`)

```python
    async def download(self, *, assignment):
        if assignment.chunks:
            plans = _plans_from_chunks(assignment.chunks, assignment.file_size)
            if plans is None:                       # malformed → safe fallback
                logger.warning("subtask %s chunk rows don't tile file; "
                               "falling back to local split", assignment.subtask_id)
                if assignment.file_size is None:
                    assignment = await self._resolve_size(assignment)
                plans = plan_chunks(assignment.file_size, self._s.chunk_size_bytes)
        else:
            if assignment.file_size is None:
                assignment = await self._resolve_size(assignment)
            plans = plan_chunks(assignment.file_size, self._s.chunk_size_bytes)
        ...                                          # pass1/pass2 unchanged
```
```python
def _plans_from_chunks(chunks, file_size):
    """ChunkPlan per subtask_chunks row, ordered by chunk_index, RENUMBERED to
    dense positional index (pre-review B1: _pass2_upload uses ChunkPlan.index
    for last-chunk detection / S3 PartNumber / <idx>.bin name — must be dense
    0..n-1, not the raw chunk_index). None if rows don't tile [0,file_size-1]."""
    ordered = sorted(chunks, key=lambda c: c.chunk_index)
    plans, expect = [], 0
    for pos, c in enumerate(ordered):
        if c.byte_start != expect or c.byte_end < c.byte_start:
            return None
        plans.append(ChunkPlan(index=pos, offset=c.byte_start,
                               length=c.byte_end - c.byte_start + 1))
        expect = c.byte_end + 1
    if file_size is not None and expect != file_size:
        return None
    if not plans:
        return None
    return plans
```
(`file_size` from chunks when the assignment's is None: the last chunk's
`byte_end + 1`. The contiguity check tolerates `file_size is None` by skipping
the total check.)

## 6. Tests

- **`tests/api/test_poll_chunks.py`** (or extend an existing executor-poll test):
  seed a chunked subtask + 2 `subtask_chunks` rows (sources A/B) → `POST
  /{executor}/poll` → `subtask.chunks` has 2 items, ordered by index, with the
  right byte ranges + source_ids; a NON-chunked subtask → `chunks == []`.
- **`tests/executor/test_chunk_downloader.py`** extension: an `Assignment` with
  `chunks=(0-67108863 src=A, 67108864-end src=B)` + a fake `stream_source` that
  records every Range header → assert exactly 2 Range requests
  `bytes=0-67108863` and `bytes=67108864-<end>` (the chunk boundaries), NOT the
  16-MiB local split; assembled SHA matches. Plus: malformed chunks (gap) →
  falls back to `plan_chunks` (records the 16-MiB Ranges) + logs.
- **`tests/executor/test_runner_choose.py`** (or existing runner test): an
  assignment with chunks forces the chunk downloader even when `file_size <
  threshold`.
- Backward-compat: existing `test_chunk_downloader.py` cases (no chunks) stay
  green unchanged — proves the `plan_chunks` path is untouched.

## 7. Milestones

- **M1 — protocol**: `ChunkAssignment` schema + `SubTaskRead.chunks` + `post_poll`
  query + (if needed) openapi `SubTaskRead.chunks` + poll test + backend gate.
- **M2 — executor**: `Assignment.chunks` + runner build + `_choose_downloader` +
  `download` chunk-plan branch + `_plans_from_chunks` + executor tests + full
  backend gate (executor tests run under pytest).
- **M3 — integration + docs**: a focused test asserting Range alignment end-to-end
  (poll → executor Ranges per chunk row, mocked proxy/stream); update the SP2
  spec banner / a doc note that ruling 6b is now wired; backend gate.

## 8. Risks & Contingencies

- **SHA safety net (with one documented carve-out)**: when `expected_sha256` is
  present (the normal case — HF is the sha authority), even wrong alignment is
  caught by the whole-file SHA256 W4 gate → blacklist + HF-refetch, no silent
  corruption. **Carve-out (pre-review arch-I1)**: the W4 gate is SKIPPED when
  `expected_sha256 is None`. The planner refuses to chunk a no-sha file (pins it
  to HF) UNLESS the task set `trust_non_hf_sha256=True` (`source_scheduler.py`
  `no_hf_authority` branch). So the ONE residual unverified path is
  `trust_non_hf_sha256=True` + `expected_sha256=None` + multi-source chunked:
  bytes are assembled with no sha backstop. This is PRE-EXISTING (ruling 4: trust
  = the user's explicit opt-out of sha authority) but SP2-alignment *activates*
  multi-source chunk acceleration, converting "safe HF-refetch fallback" into
  "trusted multi-source assembly". **Accepted under ruling 4** — `trust=True` is
  an explicit, audited per-task opt-out; the alignment doesn't change *who*
  verifies, only *which source serves which bytes*. The "cannot introduce
  corruption" claim holds for all sha-bearing tasks; for the trust+no-sha path it
  is the documented, opted-into behavior. (An integration test SHOULD pin this:
  a `trust=True, expected_sha256=None` chunked sub assembles without a gate.)
- **Stale `subtask_chunks` rows on re-pend (pre-review arch-I2)**: a failed/
  reclaimed chunked subtask returns to `pending` WITHOUT clearing its chunk rows
  (`is_chunked` stays True); `post_poll` serializes whatever rows currently
  exist. This is correct because the rebalance loop maintains those rows —
  `UPDATE subtask_chunks SET source_id = <healthy>` for `pending` chunks of a
  blacklisted source — so a re-poll gets the current healthy map. The only
  transient: a chunk whose source was just blacklisted before the rebalance tick
  → executor pulls from it → W4 gate catches → re-fail → rebalanced next time.
  Safe. **Required controller-side invariant (named, not owned by SP2)**: chunk
  rows are created once (`_split_chunks` only adds; UNIQUE(subtask_id,
  chunk_index)) and rebalance-maintained; if a future change ever re-runs
  `_split_chunks` on an existing `is_chunked` subtask it must clear old rows
  first. Flagged as a known limitation, not introduced here.
- **Fallback on a chunked sub still mis-routes (but fails safe)**: if
  `_plans_from_chunks` returns None (malformed/stale) and we fall back to
  `plan_chunks`, the 16-MiB Ranges again cross source boundaries → proxy
  mis-routes → W4 SHA mismatch → blacklist + HF-refetch. Same safe degradation as
  today; the fallback only avoids crashing on malformed rows.
- **`file_size` None with chunks (harmless edge)**: in practice the controller
  only chunks files with KNOWN size ≥100 MiB, so `file_size` is never None when
  `chunks` is non-empty — the derivation branch is essentially unreachable.
  `_pass2_upload` uses `assignment.file_size` only for `DownloadResult.
  bytes_written` (the multipart size derives from the `.bin` parts), so the
  `dataclasses.replace` is a harmless correctness belt-and-suspenders, not a
  load-bearing path. `_plans_from_chunks` tolerates `file_size is None` (skips
  the total check).
- **Contiguity**: the controller `_split_chunks` tiles `[0, size-1]` exactly (last
  source gets the remainder); the defensive check + fallback covers any drift.
- **No migration, no new dep.** `subtask_chunks` + the source-proxy routing
  already exist. No openapi change unless `SubTaskRead` is in the static spec
  (verify; add an optional `chunks` array if so, no null examples).
- **CI doesn't gate ruff** — real gate is pytest + `lint_invariants`; `ruff
  --select I001 --fix` new files only.
- **Frontend**: none — purely executor/controller protocol.

## 9. Self-Review

- **Closes the gap**: executor Range == one `subtask_chunks` row → proxy routes
  each to its source → multi-source chunk acceleration lands; SP2 ruling 6b
  wired. ✓
- **Sha-safe by construction**: sequential offset-order assembly + whole-file
  rehash (mode B) unchanged; malformed input falls back, never corrupts. ✓
- **No per-chunk sha / no chunk-status reporting** — deferred per doc (mode B
  suffices) + named follow-on. ✓
- **Backward compat**: non-chunked subtasks use `plan_chunks` exactly as today;
  existing executor tests unchanged. ✓
- **Placeholder scan**: the defensive fallback + the deferrals are deliberate +
  documented, not TODOs. ✓
- **Consistency**: rides the existing poll payload + `DirectOffsetDownloader`
  pass1/pass2 + source-proxy routing; additive schema field.
