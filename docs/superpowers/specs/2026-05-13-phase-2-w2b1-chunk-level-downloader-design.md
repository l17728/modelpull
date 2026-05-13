# Phase 2 Week 2b1 — Chunk-Level Downloader + Disk-Aware Scheduling Design

> **Status:** Draft (brainstormed 2026-05-13).
> **Companion plan:** `docs/superpowers/plans/2026-05-13-phase-2-w2b1-chunk-level-downloader.md` (to be written by writing-plans skill after spec approval).
> **Roadmap source:** `docs/v2.0/08-mvp-roadmap.md` §2.6 — Phase 2 Week 2 Day 5 + the half of Day 4 that disk-staging makes real (D7 paused_disk_full).
> **Companion split (W2b2):** D4 cancel API + cancelling state + D8 cancelling-aware completion + D13 paused_external + source_throttle_state live in a separate spec/plan to keep this PR scoped.
> **Distributed-correctness source:** `docs/v2.0/03-distributed-correctness.md` §9 (paused_disk_full, D7) + `docs/v2.0/01-architecture.md` §5.2/§5.3 (DirectOffsetDownloader + multi-executor host sharing) + INVARIANTS §D-22 (S3 multipart constraints).

---

## 1. Goal & Non-Goals

### 1.1 Goal

Two concerns shipped together because disk staging connects them:

1. **DirectOffsetDownloader.** New `src/dlw/executor/chunk_downloader.py`. For files ≥ `DLW_CHUNK_LEVEL_THRESHOLD_BYTES` (default 100 MiB) or with unknown size, executor pulls N HTTP Range requests in parallel into `.parts/<subtask_id>/<chunk_idx>.bin`, then in a second sequential pass reads each chunk from disk in order, computes streaming `hashlib.sha256`, and uploads it as an S3 multipart part. The W4 `HfS3StreamDownloader` is preserved as the small-file fast path; runner dispatches by `Assignment.file_size` threshold.

2. **D7 paused_disk_full.** Disk staging makes `ENOSPC` real. Executor catches `errno.ENOSPC` on chunk writes and raises `DiskFullError`; runner reports `status="paused_disk_full"` via `/subtasks/{id}/report`. Scheduler `claim_one_subtask` adds a disk pre-flight: a candidate subtask is acceptable only if the calling executor's `disk_free_gb*GiB - parts_dir_bytes >= file_size + 200 MiB safety`. A new `sweep_paused_disk_full` routine (run from the W2a `_sweep_loop_main` every 30s) flips `paused_disk_full` subtasks back to `pending` once disk frees up.

After W2b1, every Phase-2 executor with `DLW_PARTS_DIR` configured can stage large downloads to disk safely, and the controller refuses to assign subtasks that won't fit. A startup GC pass on the executor reaps `.parts/` directories that don't correspond to any active subtask, preventing leak across crashes.

### 1.2 Non-goals (deferred — explicit list)

| Item | Why deferred | Where it lands |
|---|---|---|
| `POST /tasks/{id}/cancel` + `cancelling` task state + complete_subtask cancelling-aware branch | Separate D8 concern, different state machine table | **W2b2** |
| `paused_external` subtask state + `source_throttle_state` table + 429/5xx detection + retry sweep | Self-contained D13; orthogonal to disk path | **W2b2** |
| `verified` subtask state (spec 03 §7.2) | W2b1 keeps `succeeded` as terminal-with-file-preserved; `verified` is a Phase 3 refinement when chunk-level supports cross-crash resume | **Phase 3** |
| Multipart upload resume across executor crashes | W1 has `multipart_upload_id` persisted but W2b1 aborts and restarts on any error. Resume requires reading remote parts list + skipping completed chunks | **Phase 3** |
| BLAKE3 streaming hash | Parallel hash requires Merkle-tree-friendly algorithm; SHA256 sequential is the W2b1 contract | **v2.2** |
| Heartbeat-carried cancellation signal | Requires the cancel state to exist first | **W2b2 or later** |
| Dynamic concurrency / NIC-aware worker count (per `01 §5.3` formula) | Requires host capacity ledger + active-executor count queries | **Phase 3** |
| Multi-source chunk-level routing (`06 §1.6`) | Phase 2 OUT list — single source (HF) only | **Phase 3 (v2.1)** |
| P-004 baseline (≥ 1 GB/s single executor) | Hardware-dependent; CI runner cannot exercise it | **After Phase 2 W3** |
| HF CDN URL If-Match commit pin (03 §10) | Requires `X-Repo-Commit` plumbing through proxy | **Phase 3** |

---

## 2. Tech Stack Additions

**None.** Existing stack covers everything:

- `httpx` async client with `MockTransport` for tests (already in W4).
- `boto3` S3 multipart (already in W4).
- `tenacity` retry decorator for transient HTTP (already in W4).
- `asyncio.Semaphore` for chunk concurrency (stdlib).
- `pathlib.Path` for `.parts/` (stdlib).
- `errno` + `OSError.errno == ENOSPC` for disk-full catch (stdlib).
- `moto` for S3 mocking (already in W4 tests).
- `pytest` + existing `engine` / `db_session` conftest fixtures.

No new runtime deps. No new dev deps. No new CI jobs. No alembic migration.

---

## 3. Components

### 3.1 New: `src/dlw/executor/chunk_downloader.py`

Public surface mirrors `HfS3StreamDownloader` so `runner._execute_subtask` can dispatch by holding both instances:

```python
class DirectOffsetDownloader:
    def __init__(self, *, settings: ExecutorSettings) -> None: ...
    async def download(self, *, assignment: Assignment) -> DownloadResult: ...
```

`Assignment` and `DownloadResult` are re-used from `src/dlw/executor/downloader.py` — same fields, no copies. A new exception:

```python
class DiskFullError(Exception):
    """ENOSPC during chunk write. Runner translates to paused_disk_full report."""
```

A pure helper for the chunk plan:

```python
@dataclass(frozen=True)
class ChunkPlan:
    index: int      # 0..N-1
    offset: int     # inclusive
    length: int     # bytes; last chunk may be < chunk_size

def plan_chunks(file_size: int, chunk_size: int) -> list[ChunkPlan]: ...
```

`plan_chunks(0, ...)` returns `[]`. `plan_chunks(N, K)` returns `ceil(N/K)` plans; last plan's `length` is the remainder. S3 multipart constraints (D-22): the result is acceptable iff `chunk_size >= 5 MiB`, which we enforce via `assert` on the `DLW_CHUNK_SIZE_BYTES` env at module load (sane fail-fast); `ceil(N/K) <= 10000` is a soft check — for typical models (< 50 GiB) with 16 MiB chunks, that's ≤ 3200 parts, well under 10000.

Control flow inside `download`:

```python
async def download(self, *, assignment: Assignment) -> DownloadResult:
    if assignment.file_size is None:
        assignment = await self._resolve_size(assignment)
    plans = plan_chunks(assignment.file_size, self._s.chunk_size_bytes)
    dest_dir = parts_dir_for(self._s.parts_dir_path, assignment.subtask_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        await self._pass1_parallel(assignment, plans, dest_dir)
    except DiskFullError:
        # Deliberately leak parts; sweeper + startup GC reap.
        raise
    except Exception:
        cleanup_parts_dir(self._s.parts_dir_path, assignment.subtask_id)
        raise
    # Pass 2 manages its own cleanup (success + abort paths).
    return await self._pass2_upload(assignment, plans, dest_dir)
```

`_resolve_size(assignment)` signature: `async def _resolve_size(self, a: Assignment) -> Assignment`. Issues `HEAD {url}` with bearer auth if set; reads `Content-Length`; returns `dataclasses.replace(a, file_size=int(header))`. Raises `RuntimeError("file_size unresolvable: HEAD returned no Content-Length")` if header is missing.

`download(assignment)` runs two phases sequentially:

**Pass 1 (parallel)** — `_pass1_parallel`:

- For each `ChunkPlan` in `plans`, spawn an asyncio task gated by `asyncio.Semaphore(settings.chunk_concurrency)` (default 4).
- Each task issues `GET {hf_endpoint}/{repo_id}/resolve/{revision}/{filename}` with `Range: bytes={offset}-{offset+length-1}`.
- Writes `aiter_bytes(64 KiB)` to `parts_dir / "{index}.bin"`.
- On `OSError(errno=ENOSPC)` raises `DiskFullError`. Partial file is **not** deleted (sweeper recovery + executor startup GC clean up).
- Wrapped in tenacity `_TRANSIENT_RETRY` (same predicate as W4: 5xx, NetworkError, TimeoutException, ProtocolError; 3 attempts; exponential backoff).
- `asyncio.gather(...)` raises on first failure.

**Pass 2 (sequential)** — `_pass2_upload`:

- `s3.create_multipart_upload(Bucket, Key)` → `upload_id` (kept in memory only; Phase 3 will persist it via heartbeat for resume).
- For each plan in index order: `body = (parts_dir / f"{index}.bin").read_bytes()` → `sha.update(body)` → `s3.upload_part(... PartNumber=index+1, Body=body)` → record etag.
- After all parts: `s3.complete_multipart_upload(... MultipartUpload={"Parts": parts})`.
- `cleanup_parts_dir(parts_dir_path, subtask_id)`.
- Returns `DownloadResult(bytes_written=file_size, actual_sha256=sha.hexdigest(), s3_key=key)`.

**Error handling in pass 2**:

- On any exception, `s3.abort_multipart_upload(...)` (logged on its own failure, like W4).
- If the exception is `DiskFullError`, propagate without `cleanup_parts_dir` (parts leak deliberately so sweeper can recover after disk frees up).
- All other exceptions trigger `cleanup_parts_dir` then re-raise.

**`_resolve_size(assignment)`** — when `assignment.file_size is None`, issue `HEAD` for the URL, read `Content-Length`. If still None, raise `RuntimeError("file_size unresolvable")`; runner catches as generic failure (not paused_disk_full).

Private helpers (`_make_s3_client`, `_make_http_client`, `_compose_key`, `_upload_part`, `_TRANSIENT_RETRY`, `_HTTP_CHUNK_BYTES`) are copied from `downloader.py`. To keep DRY, a refactor extracts the shared 4 helpers into `src/dlw/executor/_io.py` — a small (≈ 40 LOC) helper module imported by both downloaders. The refactor is listed as an explicit task in the plan (not a hidden side effect).

### 3.2 New: `src/dlw/executor/parts_dir.py`

Three pure functions, no class:

```python
def parts_dir_for(root: str, subtask_id: uuid.UUID) -> pathlib.Path:
    """Return ${root}/${subtask_id_hex}; does NOT create."""

def cleanup_parts_dir(root: str, subtask_id: uuid.UUID) -> None:
    """rmtree the per-subtask dir if it exists; ignore errors."""

def total_parts_bytes(root: str) -> int:
    """Sum of file sizes under ${root}/, recursive; 0 if root missing."""

def startup_gc(root: str, active_subtask_ids: set[uuid.UUID]) -> int:
    """Scan ${root}/* directories; rmtree any whose hex name is NOT in
    active_subtask_ids. Returns count of removed dirs. Called by runner
    bootstrap before the poll loop starts."""
```

`active_subtask_ids` is fed by the runner from a one-time `GET /subtasks?executor_id=X&status=assigned,paused_disk_full`-style API. **W2b1 simplifies this:** the runner just calls `startup_gc(root, active_subtask_ids=set())` — i.e. removes EVERY pre-existing dir at startup. This is correct because:

- A crashed in-flight chunk download is unrecoverable in W2b1 (no resume); the controller will reclaim the subtask back to `pending` via `_sweep_loop_main`'s `sweep_executor_timeouts`, and a fresh executor will start over.
- A paused_disk_full subtask's `.parts/` dir is gone with the crash — but the sweeper will recover the subtask to pending anyway.

So the simpler **"remove all on startup"** behavior is sufficient for W2b1. The `active_subtask_ids` parameter exists in the API for a future Phase-3 multipart-resume world.

### 3.3 Modified: `src/dlw/executor/runner.py`

Constructor takes both downloaders:

```python
class ExecutorRunner:
    def __init__(
        self,
        *,
        settings: ExecutorSettings,
        client: ControllerClient,
        stream_downloader: HfS3StreamDownloader,
        chunk_downloader: DirectOffsetDownloader,
    ) -> None: ...
```

`run()` calls `parts_dir.startup_gc(self._s.parts_dir_path, active_subtask_ids=set())` **before** the join request (cheapest: avoids any race with controller asking us to start a new download into a dir we're about to delete).

`_choose_downloader(file_size)`:

```python
def _choose_downloader(self, file_size: int | None):
    threshold = self._s.chunk_level_threshold_bytes
    if file_size is None or file_size >= threshold:
        return self._chunk_downloader
    return self._stream_downloader
```

`_execute_subtask` calls `_choose_downloader(assignment.file_size)`, then `await downloader.download(assignment=assignment)`. Two new except clauses:

```python
try:
    result = await downloader.download(assignment=assignment)
except DiskFullError as e:
    await self._client.report(
        subtask_id=sub_id,
        status="paused_disk_full",
        assignment_token=assignment_token,
        actual_sha256=None,
        bytes_downloaded=0,
        error=str(e),
    )
    return
except Exception as e:
    # existing failure path (unchanged)
```

`_heartbeat_loop` reads `parts_dir.total_parts_bytes(self._s.parts_dir_path)` each tick and passes as `parts_dir_bytes` instead of the W4 hard-coded `0`.

### 3.4 Modified: `src/dlw/executor/config.py`

Add 5 new fields to `ExecutorSettings`:

```python
chunk_level_threshold_bytes: int = int(os.environ.get("DLW_CHUNK_LEVEL_THRESHOLD_BYTES", 100 * 1024 * 1024))
chunk_size_bytes: int = int(os.environ.get("DLW_CHUNK_SIZE_BYTES", 16 * 1024 * 1024))
chunk_concurrency: int = int(os.environ.get("DLW_CHUNK_CONCURRENCY", 4))
parts_dir_path: str = os.environ.get("DLW_PARTS_DIR", "./parts")
disk_safety_margin_bytes: int = int(os.environ.get("DLW_DISK_SAFETY_MARGIN_BYTES", 200 * 1024 * 1024))
```

Pattern matches existing settings (env-overridable, defaults committed). `chunk_size_bytes` assertion (`>= 5 MiB`) lives in `chunk_downloader.py` module load, not here, since config is just I/O.

### 3.5 Modified: `src/dlw/executor/cli.py`

Build both downloaders and pass to runner:

```python
stream = HfS3StreamDownloader(settings=settings)
chunk = DirectOffsetDownloader(settings=settings)
runner = ExecutorRunner(
    settings=settings, client=client,
    stream_downloader=stream, chunk_downloader=chunk,
)
```

### 3.6 Modified: `src/dlw/services/scheduler.py`

`claim_one_subtask` candidate scan (current W2a body uses `.limit(1)`):

```python
async def claim_one_subtask(session, executor_id, executor_epoch):
    """W2b1 §2.5: candidate scan with disk pre-flight, in addition to the
    W2a self-eligibility and reverse host-affinity checks."""
    from sqlalchemy.orm import aliased

    e_self = await session.get(Executor, executor_id)
    if e_self is None or e_self.status not in ("healthy", "degraded"):
        return None, None

    sib = aliased(FileSubTask)
    e_other = aliased(Executor)
    same_host_holds = (
        select(sib.id)
        .join(e_other, e_other.id == sib.executor_id)
        .where(sib.task_id == FileSubTask.task_id)
        .where(sib.filename == FileSubTask.filename)
        .where(sib.status == "assigned")
        .where(e_other.host_id == e_self.host_id)
        .where(e_other.id != executor_id)
        .exists()
    )

    GiB = 1024 ** 3
    SAFETY = _DISK_SAFETY_MARGIN_BYTES   # env-configured, default 200 MiB
    free_bytes = (e_self.disk_free_gb or 0) * GiB - (e_self.parts_dir_bytes or 0)

    stmt = (
        select(FileSubTask)
        .where(FileSubTask.status == "pending")
        .where(~same_host_holds)
        .order_by(FileSubTask.created_at)
        .limit(_K_CANDIDATES)              # env-configured, default 16
        .with_for_update(skip_locked=True)
    )
    candidates = (await session.execute(stmt)).scalars().all()
    for sub in candidates:
        size = sub.file_size or 0
        if size + SAFETY <= free_bytes:
            token = uuid.uuid4()
            sub.status = "assigned"
            sub.executor_id = executor_id
            sub.executor_epoch = executor_epoch
            sub.assignment_token = token
            sub.assigned_at = datetime.now(UTC)
            return sub, token
    return None, None
```

Module-level constants:

```python
_K_CANDIDATES = int(os.environ.get("DLW_SCHEDULER_CANDIDATES", 16))
_DISK_SAFETY_MARGIN_BYTES = int(os.environ.get("DLW_DISK_SAFETY_MARGIN_BYTES", 200 * 1024 * 1024))
```

`complete_subtask` extends to accept `paused_disk_full`:

```python
async def complete_subtask(session, subtask_id, *, final_status, ...):
    # W1 epoch-mismatch gate (unchanged).
    ...
    if final_status == "paused_disk_full":
        sub.status = "paused_disk_full"
        sub.executor_id = None
        sub.executor_epoch = None
        sub.assignment_token = None
        sub.assigned_at = None
        sub.last_error = error
        # No parent.status transition; paused does not fail the task.
        # No transition_executor call; disk_full is environmental, not a quality signal.
        return sub, parent

    # W4 sha256 verify gate + W2a tail (transition_executor) — unchanged.
    ...
```

The `paused_disk_full` branch sits between the W1 fence check and the W4 sha256 verify gate (above the `final_status='succeeded'` flow).

### 3.7 Modified: `src/dlw/services/recovery.py`

New function:

```python
async def sweep_paused_disk_full(session: AsyncSession) -> int:
    """W2b1: recover paused_disk_full subtasks whose owning executor now has
    enough disk. Returns count recovered to pending. Caller commits."""
    GiB = 1024 ** 3
    SAFETY = _DISK_SAFETY_MARGIN_BYTES

    rows = (await session.execute(
        select(FileSubTask, Executor)
        .join(Executor, Executor.id == FileSubTask.executor_id)
        .where(FileSubTask.status == "paused_disk_full")
        .with_for_update(skip_locked=True, of=FileSubTask)
    )).all()

    recovered = 0
    for sub, ex in rows:
        size = sub.file_size or 0
        free_bytes = (ex.disk_free_gb or 0) * GiB - (ex.parts_dir_bytes or 0)
        if size + SAFETY <= free_bytes:
            sub.status = "pending"
            sub.executor_id = None
            sub.executor_epoch = None
            sub.assignment_token = None
            sub.assigned_at = None
            recovered += 1
    return recovered
```

`_DISK_SAFETY_MARGIN_BYTES` lives in `scheduler.py` and is imported here (single source of truth for the constant).

### 3.8 Modified: `src/dlw/main.py`

`_sweep_loop_main` extends to call both sweepers (W2a `sweep_executor_timeouts` + W2b1 `sweep_paused_disk_full`) on each tick:

```python
async def _sweep_loop_main(factory) -> None:
    from dlw.services.recovery import sweep_executor_timeouts, sweep_paused_disk_full
    while True:
        try:
            await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
            async with factory() as session:
                await sweep_executor_timeouts(session)
                await sweep_paused_disk_full(session)
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("sweep_loop iteration failed; will retry next tick")
```

### 3.9 Modified: `src/dlw/schemas/subtask.py`

`SubTaskReport.status` widens:

```python
class SubTaskReport(BaseModel):
    status: Literal["succeeded", "failed", "paused_disk_full"]   # was: succeeded | failed
    ...
```

### 3.10 Modified: `tools/lint_invariants.py`

New helper alongside W2a's `check_executor_status_domain`. Implementation mirrors the W2a helper exactly — same AST patterns (keyword arg + attribute Assign), but the target attribute is `status` reached via a different value-domain set:

```python
VALID_SUBTASK_STATUS = {
    "pending", "assigned", "succeeded", "failed", "cancelled", "paused_disk_full",
}


def check_subtask_status_domain() -> list[str]:
    """Lint string literals assigned to a `status` kwarg/attr in service modules
    where FileSubTask rows are mutated. Identical AST patterns to W2a's
    check_executor_status_domain; only the value-domain set differs."""
    errors: list[str] = []
    files = [
        ROOT / "src" / "dlw" / "services" / "scheduler.py",
        ROOT / "src" / "dlw" / "services" / "recovery.py",
        ROOT / "src" / "dlw" / "services" / "task_service.py",
    ]
    import ast as _ast
    for f in files:
        if not f.exists():
            continue
        tree = _ast.parse(f.read_text(encoding="utf-8"))
        for node in _ast.walk(tree):
            # Pattern 1: keyword arg `status="<literal>"` (pg_insert.values, dict, etc.)
            if isinstance(node, _ast.keyword) and node.arg == "status":
                if isinstance(node.value, _ast.Constant) and isinstance(node.value.value, str):
                    if node.value.value not in VALID_SUBTASK_STATUS:
                        errors.append(
                            f"{f.relative_to(ROOT)}:{node.value.lineno}: "
                            f"invalid subtask status: {node.value.value!r}"
                        )
            # Pattern 2: attribute assignment `x.status = "<literal>"`
            elif (isinstance(node, _ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], _ast.Attribute)
                    and node.targets[0].attr == "status"
                    and isinstance(node.value, _ast.Constant)
                    and isinstance(node.value.value, str)):
                if node.value.value not in VALID_SUBTASK_STATUS:
                    errors.append(
                        f"{f.relative_to(ROOT)}:{node.lineno}: "
                        f"invalid subtask status: {node.value.value!r}"
                    )
    return errors
```

Wired into `main()` via `failures.extend(check_subtask_status_domain())` next to the W2a entry.

**Disjoint file sets — no cross-talk with W2a.** W2a's `check_executor_status_domain` already scans only `state_machine.py` + `executor_service.py` (where `Executor` rows are mutated). W2b1's new helper scans only `scheduler.py` + `recovery.py` + `task_service.py` (where `FileSubTask` rows are mutated). No file is scanned by both helpers, so values valid for one row type but not the other (e.g. `"pending"` is valid for subtasks but not for executors) do not produce false positives.

---

## 4. Schema Changes

**None.** Every column needed is in place from earlier weeks (see §5.1).

The `file_subtasks.status` value domain widens from `{pending, assigned, succeeded, failed, cancelled}` to `{pending, assigned, succeeded, failed, cancelled, paused_disk_full}`. The widening is enforced by the new `check_subtask_status_domain` lint in `tools/lint_invariants.py`. No DB-level CHECK constraint (consistent with how W2a kept `executors.status` open-domain at the DB layer).

---

## 5. Wire Format Changes

**Two** widenings, both backwards-compatible:

### 5.1 `SubTaskReport.status` enum

`api/openapi.yaml`'s `SubTaskReport` schema: `status` field enum extends from `[succeeded, failed]` to `[succeeded, failed, paused_disk_full]`. Existing clients (W1 / W2a) that only emit the first two values keep working unchanged.

### 5.2 Heartbeat body `parts_dir_bytes`

W1's `ExecutorHeartbeat.parts_dir_bytes` field already exists and is already wire-defined. W4 runner sends `0`; W2b1 sends the real value from `total_parts_bytes`. No schema diff.

OpenAPI `ExecutorHeartbeat.parts_dir_bytes` description gets a one-line clarification: "Sum of bytes under `DLW_PARTS_DIR/`; controller uses this for disk pre-flight checks."

---

## 6. Refactor — `src/dlw/executor/_io.py`

Both downloaders need:

- `_make_s3_client(cfg: StorageConfig)`
- `_make_http_client()` (test seam)
- `_compose_key(assignment)`
- `_upload_part(s3, bucket, key, upload_id, part_no, body) -> str`
- `_TRANSIENT_RETRY` decorator
- `_HTTP_CHUNK_BYTES` constant
- `_is_transient_http` predicate

Extract to a single 40-line `src/dlw/executor/_io.py` module. Both downloaders import from there. This is the only refactor W2b1 makes; it's listed as an explicit plan task (not a hidden side effect of `chunk_downloader.py`). Without the refactor, `chunk_downloader.py` would duplicate ~30 lines from `downloader.py`.

---

## 7. Error Handling Matrix

| Situation | Behaviour |
|---|---|
| HF returns 4xx (404, 401, 403) for a chunk Range request | Not retried (`_is_transient_http` only catches 5xx + network). Failure propagates → runner reports `failed`. |
| HF returns 5xx for a chunk | `_TRANSIENT_RETRY` retries with exponential backoff, up to 3 attempts per chunk. Each chunk task independent — one slow chunk doesn't block others. |
| HF returns 206 with content shorter than requested Range | The aiter loop ends early; `target.bin` is smaller than `plan.length`. Pass 2 will `read_bytes()` it intact and upload as that part — S3 multipart accepts last part smaller than 5 MiB, but not internal parts. **Risk:** an internal undersize part fails `complete_multipart_upload`. Mitigation: pass 2 explicitly asserts `len(body) == plan.length or plan.index == len(plans) - 1`; raises a descriptive error to runner. |
| `errno.ENOSPC` mid-pass-1 | `DiskFullError` raised from the failing chunk task. Other parallel tasks may have already written full chunks; **we deliberately do NOT cleanup** so sweeper recovery + executor startup GC handle it. Runner catches and reports `paused_disk_full`. |
| `errno.ENOSPC` mid-pass-2 (read_bytes) | Cannot happen — pass 2 only reads from disk, doesn't write. |
| S3 `upload_part` fails mid-pass-2 | Caught by the broad `except BaseException` in pass 2; triggers `abort_multipart_upload` + `cleanup_parts_dir` + re-raise. Runner reports `failed`. |
| `complete_multipart_upload` fails (rare — bad ETag list, etc.) | Same as above. |
| `_resolve_size` finds Content-Length missing | `RuntimeError("file_size unresolvable")` → runner reports `failed`. |
| Scheduler pre-flight finds no candidates fit | `(None, None)` returned; the K-1 unused candidates' row locks release on session commit. Next poll cycle may pick them on a different executor with more disk. |
| `sweep_paused_disk_full` finds a recovered subtask whose owning executor has since died | Sub is recovered to `pending` regardless. The dead executor's leftover `.parts/<subtask_id>/` will be GC'd on that executor's next startup. New executor that claims the recovered subtask starts from scratch. |
| Executor crash mid-pass-1 | Controller's `sweep_executor_timeouts` (W2a) eventually marks the executor `suspect`/`faulty` after `HB_TIMEOUT_TO_SUSPECT` (~3 × 90 s = ~4.5 min worst case), reclaims its `assigned` subtasks back to `pending`. The executor's startup GC removes the leaked `.parts/` dirs on next boot. |
| Concurrent same-executor `claim_one_subtask` calls (two polls in flight) | `SKIP LOCKED` ensures each poll locks different candidate rows; both get a different subtask or one gets `(None, None)`. |

---

## 8. Testing Strategy

### 8.1 Unit + integration (8 new cases)

| # | File | Case | What it asserts |
|---|---|---|---|
| 1 | `tests/executor/test_chunk_downloader.py` | `test_plan_chunks_splits_evenly` | `plan_chunks(100, 30)` returns 4 plans of length 30/30/30/10; `plan_chunks(0, X)` returns []; `plan_chunks(5 MiB, 16 MiB)` returns 1 plan. |
| 2 | same | `test_pass1_pass2_happy_path_with_moto` | 200 MiB synthetic file → 2 chunks via Range MockTransport → moto S3 multipart → `actual_sha256` equals `hashlib.sha256(synthetic).hexdigest()`. |
| 3 | same | `test_pass1_enospc_raises_disk_full_and_leaks_parts` | Monkeypatch `Path.open(...).write` to raise `OSError(errno=ENOSPC)`; assert `DiskFullError` from `download(...)`; assert the parts dir still exists. |
| 4 | `tests/executor/test_runner_dispatch.py` | `test_runner_picks_stream_for_small_file` | `file_size = 50 MiB`; build runner with two Mock downloaders; assert `stream_downloader.download` called, `chunk_downloader.download` not called. |
| 5 | same | `test_runner_picks_chunk_for_large_file_and_for_unknown` | `file_size = 200 MiB` and `file_size = None`; reverse assertion. |
| 6 | `tests/services/test_scheduler_disk_preflight.py` | `test_claim_skips_subtask_too_big_for_executor` | Executor with `disk_free_gb=1`, `parts_dir_bytes=0`; subtask with `file_size=5 GiB`; assert `(None, None)`. |
| 7 | same | `test_claim_picks_next_candidate_if_first_too_big` | Two pending subtasks (first 5 GiB, second 100 MiB); executor has ~500 MiB free; assert claim returns the 100 MiB one and the 5 GiB one is still pending. |
| 8 | `tests/services/test_sweep_paused_disk_full.py` | `test_sweep_recovers_paused_disk_full_when_disk_free_increases` | Seed a paused_disk_full subtask; bump executor.disk_free_gb=10; run sweep; assert sub.status=`pending`, executor_id=None. |

Plus a tiny extension to `tests/lint/test_no_direct_status_write.py` is not needed (lint targets `Executor.status`, not `FileSubTask.status`). A new `tests/tools/test_lint_invariants_subtask_domain.py` covers the new `check_subtask_status_domain` helper — that's a 9th test if the implementer judges it warranted (the plan lists it as optional, contingent on whether the existing `tools/test_lint_invariants.py` already exercises the helper-extension pattern).

### 8.2 W4 + earlier test compatibility

Tests that need mechanical edits:

- `tests/executor/test_runner.py` — 4-5 setups change to pass `stream_downloader=` and `chunk_downloader=`. All assertions about behavior stay.
- `tests/executor/test_cli.py` — 1 setup change matching runner constructor.
- No test logic changes; no test deletions.

### 8.3 Test infrastructure

- **HTTP mocking**: `httpx.MockTransport`. A small fixture builds a transport that reads `Range: bytes=A-B` and returns 206 with a slice of an in-memory `bytes` object. W4 already has the pattern in `tests/executor/test_downloader.py`; copy into a `tests/executor/conftest.py` if reused across files.
- **S3 mocking**: `moto`. W4 tests already configure `mock_s3()` decorator pattern; re-use.
- **ENOSPC simulation**: `monkeypatch.setattr(pathlib.Path, "open", patched)` returning a mock file whose `.write(...)` raises `OSError(errno=errno.ENOSPC, strerror="No space left on device")`.
- **Disk pre-flight**: `env` fixture creates an Executor row with the test's required `disk_free_gb`/`parts_dir_bytes`. Re-use the pattern from `tests/services/test_scheduler_host_affinity.py`.

### 8.4 No new CI jobs

Lint extension lives in the existing `invariant_lint` job (same as W2a). Pytest runs in the existing `pytest (Phase 1 foundation)` job.

### 8.5 No new dev infra

No new docker-compose services. No new local commands. `DLW_PARTS_DIR` defaults to `./parts` for dev; tests use `tmp_path`.

---

## 9. Acceptance Criteria

- [ ] 8 new pytest cases pass on local PG 18:5433 and CI.
- [ ] The ~5 modified W4 setups pass; no other Phase-1 / W1 / W2a case regresses.
- [ ] `chunk_downloader.py` happy path (test 2) end-to-end works against moto + httpx MockTransport.
- [ ] `DirectOffsetDownloader` produces SHA256 that matches `hashlib.sha256(full_bytes).hexdigest()` for any tested file.
- [ ] `parts_dir.startup_gc(root, set())` removes all pre-existing per-subtask dirs (verified by test).
- [ ] `tools/lint_invariants.py` `check_subtask_status_domain` finds 0 violations on `main`; finds violations when seeded.
- [ ] Heartbeat from a runner with chunk-level work in flight reports non-zero `parts_dir_bytes`.
- [ ] `claim_one_subtask` skips a too-large subtask AND picks the next fitting candidate; verified by test.
- [ ] `sweep_paused_disk_full` recovers a subtask when disk frees up; verified by test.
- [ ] OpenAPI `SubTaskReport.status` enum lists 3 values; spectral CI passes.
- [ ] `docs/operator/` adds a one-line note about `DLW_PARTS_DIR` (PVC requirement, default location).
- [ ] No new runtime deps in `pyproject.toml`; no new dev deps; no new CI jobs; no alembic migration.

---

## 10. Implementation Phasing (preview for plan)

The plan will be written by the writing-plans skill after this spec is approved. Expected milestone shape (4 milestones, 7-9 tasks):

- **M1 — Shared IO refactor + chunk_downloader skeleton.** `_io.py` extraction + `chunk_downloader.py` shell with `plan_chunks` + DiskFullError + tests 1 (plan_chunks). Refactor is pure DRY, no behavior change to W4 path.
- **M2 — DirectOffsetDownloader pass 1 + pass 2 + parts_dir util.** Full happy path + ENOSPC error path; tests 2 + 3 + `parts_dir.py`. Runner unchanged yet — this milestone tests `chunk_downloader.download` in isolation.
- **M3 — Runner dispatch + heartbeat parts_dir_bytes + startup GC.** Tests 4 + 5; W4 runner test setups updated; CLI builds both. Subtask status `paused_disk_full` not yet wired controller-side.
- **M4 — Scheduler pre-flight + complete_subtask paused_disk_full + sweep_paused_disk_full + lint + OpenAPI + PR.** Tests 6 + 7 + 8 + lint extension + OpenAPI widening + PR open + CI monitor.

Branch: `feat/phase-2-w2b1-chunk-level-downloader`. Branched off `main` at `8683b03` (PR #9 merge).

---

## 11. References

- Spec source: brainstormed 2026-05-13 (this document).
- Roadmap: `docs/v2.0/08-mvp-roadmap.md` §2.6 Phase 2 W2 Day 5.
- Architecture: `docs/v2.0/01-architecture.md` §5.2 (Executor) + §5.3 (multi-executor host sharing, NIC bandwidth note).
- Distributed correctness: `docs/v2.0/03-distributed-correctness.md` §9 (paused_disk_full, D7).
- Invariants: `docs/v2.0/INVARIANTS.md` §D-22 (S3 multipart constraints).
- Test plan: `docs/v2.0/07-test-plan.md` (no specific U-/E2E- ID for chunk-level single-source; U-SRC-005 is the closest, but is multi-source — Phase 3).
- Predecessor specs:
  - `docs/superpowers/specs/2026-05-11-phase-2-week-1-fence-token-recovery-design.md` (W1 fence + recovery)
  - `docs/superpowers/specs/2026-05-13-phase-2-w2a-scheduler-state-machine-design.md` (W2a host-affinity + state machine)
- W2a PR (merged): https://github.com/l17728/modelpull/pull/9 (squash `8683b03`).
