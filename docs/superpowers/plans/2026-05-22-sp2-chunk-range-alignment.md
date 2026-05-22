# SP2 — Chunk-Range Multi-Source Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the executor download one HTTP Range per `subtask_chunks` row so each Range maps to exactly one source — landing multi-source chunk-level acceleration that today falls back to whole-file via the SHA safety net.

**Architecture:** Carry the chunk rows (`chunk_index, byte_start, byte_end, source_id`) in the executor poll payload (`SubTaskRead.chunks`); the executor's `DirectOffsetDownloader` uses them verbatim as its `ChunkPlan` list instead of its local 16-MiB split. The source-proxy already routes each Range to the chunk's source; sequential offset-order assembly + whole-file SHA256 (mode B) verify the result.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, Pydantic v2; the executor package (httpx, moto for S3 in tests); pytest asyncio_mode=auto.

**Spec:** `docs/superpowers/specs/2026-05-22-sp2-chunk-range-alignment-design.md` (read fully — ruling 6b, the defensive fallback, the named deferrals: no per-chunk sha, no executor chunk-status reporting).

**Locked constraints (do NOT violate):**
- Additive only: NO migration (`subtask_chunks` exists). NO new runtime dep. NO openapi.yaml change (`SubTaskRead`/`AssignmentResponse` are NOT in the static spec — verify with grep; the openapi `chunks` is the unrelated UI-SP2 display schema).
- The SHA safety net is sacred: sequential offset-order assembly + whole-file SHA256 W4 gate must stay intact. This change only makes the happy multi-source path succeed; it must not be able to introduce silent corruption. Malformed chunk rows MUST fall back to `plan_chunks` (sha-safe), never crash.
- Backward compat: non-chunked subtasks (empty `chunks`) keep the exact current `plan_chunks(file_size, chunk_size_bytes)` path — existing `tests/executor/test_chunk_downloader.py` cases must pass UNCHANGED.
- No per-chunk sha (`sha256_partial` stays unwritten — mode B whole-file rehash suffices). No executor per-chunk status reporting (subtask-level report as today).
- CI doesn't gate ruff — real gate is `uv run pytest` + `python tools/lint_invariants.py [--strict]`; `ruff check --select I001 --fix` new files only.

---

## File Structure

- **Modify** `src/dlw/schemas/subtask.py` — add `ChunkAssignment` + `SubTaskRead.chunks`.
- **Modify** `src/dlw/api/executors.py` — `post_poll` queries `subtask_chunks`.
- **Modify** `src/dlw/executor/types.py` — `ChunkAssignment` + `Assignment.chunks`.
- **Modify** `src/dlw/executor/runner.py` — build `chunks`, chunk-aware `_choose_downloader`.
- **Modify** `src/dlw/executor/chunk_downloader.py` — `_plans_from_chunks` + `download` branch.
- **Test**: `tests/api/test_poll_chunks.py` (new), `tests/executor/test_chunk_downloader.py` (extend), optionally a runner-choice test.

---

## Milestone M1 — Protocol: carry chunks in the poll payload

### Task 1: `ChunkAssignment` schema + `SubTaskRead.chunks` + `post_poll`

**Files:**
- Modify: `src/dlw/schemas/subtask.py`, `src/dlw/api/executors.py`
- Test: `tests/api/test_poll_chunks.py`

- [ ] **Step 1: Confirm no openapi change.** `cd "D:/download_weights" && grep -n "SubTaskRead\|AssignmentResponse" api/openapi.yaml` → expect NO matches (the executor poll schemas aren't in the static spec). If they ARE present, add an optional `chunks` array (default `[]`, no `null` examples) to the `SubTaskRead` schema there. Record the result.

- [ ] **Step 2: Add the schema.** In `src/dlw/schemas/subtask.py`, add before `SubTaskRead`:
```python
class ChunkAssignment(BaseModel):
    """One subtask_chunks row in the poll payload (SP2 chunk-Range alignment)."""
    model_config = ConfigDict(from_attributes=True)
    chunk_index: int
    byte_start: int
    byte_end: int          # inclusive
    source_id: str
```
And add to `SubTaskRead` (after `inherit_from_key`):
```python
    chunks: list[ChunkAssignment] = Field(default_factory=list)
```
(`Field` and `ConfigDict` are already imported.)

- [ ] **Step 3: Write the failing poll test.** Create `tests/api/test_poll_chunks.py`. Read an existing executor-poll test (grep `tests/` for `/poll` or `claim_one_subtask` — e.g. `tests/api/test_executors.py` or `tests/api/test_subtasks.py`) to reuse the real app/executor-enrollment/mTLS-or-epoch fixtures and the exact way a subtask is seeded + an executor polls. The assertion targets:
```python
# after seeding a chunked subtask (is_chunked=True) + 2 SubtaskChunk rows
# (chunk_index 0: bytes 0..67108863 source "modelscope";
#  chunk_index 1: bytes 67108864..end source "hf_mirror") and polling:
assert resp_json["assigned"] is True
chunks = resp_json["subtask"]["chunks"]
assert [c["chunk_index"] for c in chunks] == [0, 1]
assert chunks[0]["byte_start"] == 0 and chunks[0]["source_id"] == "modelscope"
assert chunks[1]["byte_start"] == 67108864 and chunks[1]["source_id"] == "hf_mirror"
# a NON-chunked subtask poll → subtask.chunks == []
```
(Match the file's real fixtures/seeding — the executor poll needs an enrolled executor with the right epoch + a claimable `pending` subtask. Reuse the existing poll test's setup verbatim; only add the SubtaskChunk seeding + the chunks assertions. Seed `FileSubTask(..., is_chunked=True)` + `SubtaskChunk` rows from `dlw.db.models.source`.)

- [ ] **Step 4: Verify FAIL** (`cd "D:/download_weights" && uv run pytest tests/api/test_poll_chunks.py -v`) — `chunks` absent/empty.

- [ ] **Step 5: Implement in `post_poll`** (`src/dlw/api/executors.py`). Add imports (`from dlw.db.models.source import SubtaskChunk`, `from dlw.schemas.subtask import ChunkAssignment` — and `select` if not already imported). After `sub_read = SubTaskRead.model_validate(sub)` and BEFORE `await session.commit()`:
```python
    if sub.is_chunked:
        rows = (await session.execute(
            select(SubtaskChunk).where(SubtaskChunk.subtask_id == sub.id)
            .order_by(SubtaskChunk.chunk_index))).scalars().all()
        sub_read = sub_read.model_copy(update={
            "chunks": [ChunkAssignment.model_validate(c) for c in rows]})
```

- [ ] **Step 6: Verify PASS** + regression on the existing poll test(s): `cd "D:/download_weights" && uv run pytest tests/api/test_poll_chunks.py <existing-poll-test-file> -v` → all pass (non-chunked subtasks still poll fine with `chunks == []`).

- [ ] **Step 7: Tidy + commit.**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/schemas/subtask.py src/dlw/api/executors.py tests/api/test_poll_chunks.py
git add src/dlw/schemas/subtask.py src/dlw/api/executors.py tests/api/test_poll_chunks.py && git commit -m "feat(sp2): carry subtask_chunks in the executor poll payload"
```

### Task 2: M1 backend gate

- [ ] **Step 1:** `cd "D:/download_weights" && uv run pytest -q` → all pass.
- [ ] **Step 2:** `cd "D:/download_weights" && python tools/lint_invariants.py --strict` → OK.
- [ ] No commit.

---

## Milestone M2 — Executor: Range per chunk row

### Task 3: `Assignment.chunks` + runner build + chunk-aware downloader choice

**Files:**
- Modify: `src/dlw/executor/types.py`, `src/dlw/executor/runner.py`

- [ ] **Step 1: Add to `executor/types.py`** (before `Assignment`):
```python
@dataclass(frozen=True)
class ChunkAssignment:
    chunk_index: int
    byte_start: int
    byte_end: int
    source_id: str
```
And to `Assignment` (after `storage_config`):
```python
    chunks: tuple[ChunkAssignment, ...] = ()
```

- [ ] **Step 2: Build chunks in the runner** (`src/dlw/executor/runner.py`, where `Assignment(...)` is constructed ~line 203). Import `ChunkAssignment` from `dlw.executor.downloader` (which re-exports types) or `dlw.executor.types`. Before constructing `assignment`:
```python
            raw_chunks = subtask.get("chunks") or []
            chunks = tuple(
                ChunkAssignment(
                    chunk_index=c["chunk_index"], byte_start=c["byte_start"],
                    byte_end=c["byte_end"], source_id=c["source_id"])
                for c in raw_chunks)
```
and pass `chunks=chunks` into `Assignment(...)`.

- [ ] **Step 3: Chunk-aware `_choose_downloader`** (`runner.py:54`). Change to:
```python
    def _choose_downloader(self, file_size: int | None, *, has_chunks: bool = False):
        if has_chunks:
            return self._chunk_downloader
        threshold = self._s.chunk_level_threshold_bytes
        if file_size is None or file_size >= threshold:
            return self._chunk_downloader
        return self._stream_downloader
```
And the call site (`runner.py:232`): `downloader = self._choose_downloader(assignment.file_size, has_chunks=bool(assignment.chunks))`.

- [ ] **Step 4: Smoke import** (`cd "D:/download_weights" && uv run python -c "import dlw.executor.runner, dlw.executor.types; print('ok')"`). Commit:
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/executor/types.py src/dlw/executor/runner.py
git add src/dlw/executor/types.py src/dlw/executor/runner.py && git commit -m "feat(sp2): Assignment.chunks + chunk-aware downloader choice"
```

### Task 4: `DirectOffsetDownloader` uses chunk rows

**Files:**
- Modify: `src/dlw/executor/chunk_downloader.py`
- Test: `tests/executor/test_chunk_downloader.py`

- [ ] **Step 1: Write the failing tests.** In `tests/executor/test_chunk_downloader.py`, read the existing happy-path test to reuse its fixture style (the fake/mock `ControllerClient.stream_source` Range handler + moto S3 + `ExecutorSettings`). Add:
  - A test building an `Assignment` with `chunks=(ChunkAssignment(0,0,67108863,"modelscope"), ChunkAssignment(1,67108864,<size-1>,"hf_mirror"))` (use a small total like 2 chunks of e.g. 0..15 and 16..31 with a tiny `file_size=32` to keep the mock data small — the byte values just need to drive the Range headers; pick sizes that exercise two chunks). The fake `stream_source` records each `range_header`. Assert the recorded Range headers are EXACTLY the two chunk ranges (`bytes=0-15`, `bytes=16-31`), NOT the local `chunk_size_bytes` split. Assert the assembled object's SHA matches the concatenated bytes.
  - A malformed-chunks test: chunks with a gap (e.g. `(0,0,15)` then `(1,20,31)`) → `download` falls back to `plan_chunks` (records Ranges per the 16-MiB / configured `chunk_size_bytes` split, here the whole small file in one chunk) and still succeeds. (You may assert it does NOT use the chunk ranges, i.e. the recorded Ranges differ from the malformed boundaries.)

  Keep the mock data tiny: set `ExecutorSettings.chunk_size_bytes` to its min (5 MiB) or whatever the fixture uses; use a small `file_size` (e.g. 32 bytes) and chunk boundaries within it so the test data is trivial. The point is the Range HEADERS, not large transfers.

- [ ] **Step 2: Verify FAIL** (`cd "D:/download_weights" && uv run pytest tests/executor/test_chunk_downloader.py -k "chunk_align or malformed or aligned" -v`).

- [ ] **Step 3: Implement** in `src/dlw/executor/chunk_downloader.py`. Add `_plans_from_chunks` (module-level fn near `plan_chunks`):
```python
def _plans_from_chunks(chunks, file_size: int | None) -> list[ChunkPlan] | None:
    """One ChunkPlan per subtask_chunks row, ordered by index. Returns None if
    the rows don't contiguously tile [0, file_size-1] (defensive — controller
    guarantees tiling; on drift we fall back to plan_chunks, which is sha-safe)."""
    ordered = sorted(chunks, key=lambda c: c.chunk_index)
    plans: list[ChunkPlan] = []
    expect = 0
    for c in ordered:
        if c.byte_start != expect or c.byte_end < c.byte_start:
            return None
        plans.append(ChunkPlan(index=c.chunk_index, offset=c.byte_start,
                               length=c.byte_end - c.byte_start + 1))
        expect = c.byte_end + 1
    if not plans:
        return None
    if file_size is not None and expect != file_size:
        return None
    return plans
```
Modify `download` (replace the `plans = plan_chunks(...)` line + size resolution at the top):
```python
    async def download(self, *, assignment: Assignment) -> DownloadResult:
        if assignment.chunks:
            plans = _plans_from_chunks(assignment.chunks, assignment.file_size)
            if plans is not None and assignment.file_size is None:
                assignment = dataclasses.replace(
                    assignment, file_size=plans[-1].offset + plans[-1].length)
            if plans is None:
                logger.warning(
                    "subtask %s chunk rows don't tile file; falling back to "
                    "local split", assignment.subtask_id)
                if assignment.file_size is None:
                    assignment = await self._resolve_size(assignment)
                plans = plan_chunks(assignment.file_size, self._s.chunk_size_bytes)
        else:
            if assignment.file_size is None:
                assignment = await self._resolve_size(assignment)
            plans = plan_chunks(assignment.file_size, self._s.chunk_size_bytes)
        dest_dir = parts_dir_for(self._s.parts_dir_path, assignment.subtask_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            await self._pass1_parallel(assignment, plans, dest_dir)
        except DiskFullError:
            raise
        except Exception:
            cleanup_parts_dir(self._s.parts_dir_path, assignment.subtask_id)
            raise
        return await self._pass2_upload(assignment, plans, dest_dir)
```
(`dataclasses` is already imported. `_pass2_upload` uses `assignment.file_size` for the multipart size — when chunks supply it and file_size was None, we set it above so pass2 has the total. Verify `_pass2_upload`'s use of `file_size`; if it derives total from the parts instead, the replace is harmless.)

- [ ] **Step 4: Verify PASS** + full backward-compat: `cd "D:/download_weights" && uv run pytest tests/executor/test_chunk_downloader.py -v` → ALL pass (the new alignment + malformed tests AND every pre-existing test — the no-chunks path is untouched).

- [ ] **Step 5: Tidy + commit.**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/executor/chunk_downloader.py tests/executor/test_chunk_downloader.py
git add src/dlw/executor/chunk_downloader.py tests/executor/test_chunk_downloader.py && git commit -m "feat(sp2): DirectOffsetDownloader downloads one Range per subtask_chunks row"
```

### Task 5: M2 full backend gate

- [ ] **Step 1:** `cd "D:/download_weights" && uv run pytest -q` → all pass.
- [ ] **Step 2:** `cd "D:/download_weights" && python tools/lint_invariants.py --strict` → OK.
- [ ] No commit.

---

## Milestone M3 — Integration + docs

### Task 6: Alignment integration assertion + docs

**Files:**
- Test: a focused integration test (may live in `tests/executor/` or `tests/api/`)
- Modify: the SP2 spec banner / a doc note

- [ ] **Step 1: Integration assertion.** If not already covered by Task 4's Range-header assertion, add one test that exercises poll→executor: given a chunked subtask whose poll payload carries 2 chunks routed to 2 sources, the `DirectOffsetDownloader` (driven by the Assignment built from that payload) issues exactly the 2 chunk-aligned Range requests. (This can reuse Task 4's mechanism; the goal is to assert the END-TO-END mapping `subtask_chunks row → Assignment.chunks → Range header` once. If Task 1 + Task 4 tests together already prove each hop, state that and skip a redundant test.)

- [ ] **Step 2: Update the SP2 spec deferral note.** In `docs/superpowers/specs/2026-05-19-phase-3-sp2-multi-source-design.md`, append a one-line note to item 7 / ruling 6b that the chunk-Range alignment is now wired (PR reference) — or add a short note to an operator/architecture doc. Keep it factual; do NOT rewrite history, just mark the deferral closed.

- [ ] **Step 3: Commit.**
```bash
cd "D:/download_weights" && git add -A && git commit -m "docs(sp2): mark chunk-Range alignment wired; integration assertion"
```

---

## Self-Review

**1. Spec coverage:** §1.1 schema → Task 1 ✓; §1.2 post_poll → Task 1 ✓; §1.3 Assignment → Task 3 ✓; §1.4 downloader choice → Task 3 ✓; §1.5 download branch → Task 4 ✓; §2 schema → Task 1 ✓; §5 `_plans_from_chunks` → Task 4 ✓; §6 tests → Tasks 1,4,(6) ✓; §7 milestones → M1/M2/M3 ✓.

**2. Placeholder scan:** Task 1 Step 3 / Task 4 Step 1 say "reuse the existing poll/downloader test's fixtures" — the assertion targets are fully specified; only the harness must match the real test files (which the implementer reads). The defensive fallback + deferrals are deliberate, documented. No TODOs.

**3. Type consistency:** `ChunkAssignment{chunk_index,byte_start,byte_end,source_id}` (pydantic in schemas, frozen dataclass in executor); `SubTaskRead.chunks: list[ChunkAssignment]`; `Assignment.chunks: tuple[ChunkAssignment,...]`; `_plans_from_chunks(chunks, file_size) -> list[ChunkPlan] | None`; `_choose_downloader(file_size, *, has_chunks)`. Consistent across tasks.

**Open risks for reviewers:** (a) `_pass2_upload`'s use of `assignment.file_size` vs the parts — does setting file_size from chunks (when None) feed it correctly, or does pass2 derive total from the .bin parts (making the replace unnecessary but harmless)? (b) does the existing poll-test fixture make seeding a `SubtaskChunk` straightforward (FKs: subtask must exist first)? (c) is `ChunkAssignment.model_validate(c)` correct given `SubtaskChunk` ORM attr names match (chunk_index/byte_start/byte_end/source_id — they do per the model)? (d) the malformed-fallback test — does `plan_chunks` on a tiny file produce a single chunk so the assertion is unambiguous?
