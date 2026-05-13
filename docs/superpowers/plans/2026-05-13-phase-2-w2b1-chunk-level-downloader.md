# Phase 2 Week 2b1 — Chunk-Level Downloader + Disk-Aware Scheduling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land roadmap §2.6 D5 (chunk-level multi-threaded download with disk staging) + D7 (paused_disk_full subtask state + ENOSPC catch + scheduler disk pre-flight + sweep recovery). W2b2 (cancel API + paused_external) is a separate spec/plan.

**Architecture:** Introduce a new `DirectOffsetDownloader` (parallel HTTP Range pulls → `.parts/<subtask_id>/<idx>.bin` → sequential SHA256 + S3 multipart upload). Runner dispatches by `file_size` threshold (default 100 MiB) so W4's `HfS3StreamDownloader` stays the small-file fast path. Disk staging makes ENOSPC real; executor catches and reports `paused_disk_full`; scheduler refuses too-big subtasks via a 16-candidate scan with disk pre-flight; sweep loop recovers when disk frees up.

**Tech Stack:** httpx (async + MockTransport) + boto3 (S3 multipart) + tenacity (transient retry) + asyncio.Semaphore + stdlib `errno` / `pathlib`. No new runtime deps; no new dev deps; no new CI jobs; **zero alembic migrations** — all required schema shipped in W1.

**Scope:** 9 implementation tasks across 4 milestones. Branch `feat/phase-2-w2b1-chunk-level-downloader` exists with the spec committed (commit `f460bf5`). Companion spec: `docs/superpowers/specs/2026-05-13-phase-2-w2b1-chunk-level-downloader-design.md`.

**Pre-flight:** Phase 2 W2a merged into `main` at `8683b03`. Local PG 18 running on `localhost:5433`. `uv` 0.11.9. Existing pytest baseline = 148 passed, 1 deselected. Spec approved by the user 2026-05-13.

**Out-of-scope (deferred — see spec §1.2):** POST /tasks/{id}/cancel + cancelling state (W2b2); paused_external + source_throttle_state (W2b2); verified subtask state (Phase 3); multipart upload resume (Phase 3); BLAKE3 parallel hash (v2.2); heartbeat cancellation signal (W2b2); dynamic concurrency (Phase 3); multi-source chunk-level routing (Phase 3 v2.1); P-004 1 GB/s baseline (after Phase 2 W3); HF CDN If-Match commit pin (Phase 3).

---

## File Structure

After this plan:

```
modelpull/
├── src/dlw/
│   ├── executor/
│   │   ├── _io.py                              NEW (DRY shared S3/HTTP helpers extracted from downloader.py)
│   │   ├── chunk_downloader.py                 NEW (DirectOffsetDownloader + plan_chunks + DiskFullError)
│   │   ├── parts_dir.py                        NEW (parts_dir_for / cleanup / total_parts_bytes / startup_gc)
│   │   ├── downloader.py                       MODIFY (delegate helpers to _io.py; behavior unchanged)
│   │   ├── runner.py                           MODIFY (constructor takes both downloaders; _choose_downloader; startup_gc call; heartbeat parts_dir_bytes; DiskFullError except)
│   │   ├── cli.py                              MODIFY (build both downloaders)
│   │   └── config.py                           MODIFY (+5 new pydantic fields)
│   ├── services/
│   │   ├── scheduler.py                        MODIFY (candidate scan; disk pre-flight; complete_subtask paused_disk_full branch)
│   │   └── recovery.py                         MODIFY (+sweep_paused_disk_full)
│   ├── schemas/subtask.py                      MODIFY (SubTaskReport.status widens to add paused_disk_full)
│   └── main.py                                 MODIFY (_sweep_loop_main calls both sweepers)
├── tests/
│   ├── executor/
│   │   ├── test_chunk_downloader.py            NEW (3 cases: plan_chunks, happy path, ENOSPC)
│   │   ├── test_runner_dispatch.py             NEW (2 cases: stream vs chunk dispatch)
│   │   ├── test_runner.py                      MODIFY (~4-5 setups: pass both downloaders to ExecutorRunner)
│   │   ├── test_cli.py                         MODIFY (1 setup: build both downloaders)
│   │   └── test_parts_dir.py                   NEW (3 cases: cleanup, total bytes, startup_gc)
│   └── services/
│       ├── test_scheduler_disk_preflight.py    NEW (2 cases: skip too-big; pick next fitting)
│       └── test_sweep_paused_disk_full.py      NEW (1 case: recovery)
├── tools/lint_invariants.py                    MODIFY (+check_subtask_status_domain)
├── api/openapi.yaml                            MODIFY (SubTaskReport.status enum widens)
└── docs/operator/                              MODIFY (one-line note on DLW_EXECUTOR_PARTS_DIR_PATH)
```

**Why this structure:** `chunk_downloader.py` is the heart of W2b1, ~250 LOC including helpers; `parts_dir.py` is a 40-LOC pure-function module; `_io.py` is 40-LOC DRY extraction. No file balloons past 300 LOC. Scheduler and recovery diffs are small additive changes (candidate scan + sweeper function).

---

## Pre-flight checks

- [ ] On branch `feat/phase-2-w2b1-chunk-level-downloader`, spec committed (`git log --oneline -1` shows `f460bf5` or descendant).
- [ ] `main` at `8683b03` (PR #9 merge): `git log main --oneline -1`.
- [ ] PG running on `localhost:5433` (`pg_isready -h localhost -p 5433`).
- [ ] `dlw` database at alembic head `5cfd4bb519f6` (W2a state machine): `uv run alembic current`.
- [ ] Existing pytest suite green: `uv run pytest -x` → 148 passed, 1 deselected.
- [ ] `uv --version` ≥ 0.11.9.

---

## Milestone 1 — Shared IO refactor + chunk_downloader skeleton

After M1, the W4 `HfS3StreamDownloader` keeps its current behavior but reads its S3/HTTP helpers from a new shared `_io.py`. `chunk_downloader.py` exists with `plan_chunks`, `DiskFullError`, and the public `DirectOffsetDownloader` constructor signature; one new test exercises `plan_chunks`. No production path uses the chunk downloader yet.

---

### Task 1: Extract shared helpers into `src/dlw/executor/_io.py`

**Files:**
- Create: `src/dlw/executor/_io.py`
- Modify: `src/dlw/executor/downloader.py`

- [ ] **Step 1: Read the current `src/dlw/executor/downloader.py`**

Confirm it contains these private helpers/constants at module level or in `HfS3StreamDownloader`:

- `_HTTP_CHUNK_BYTES = 64 * 1024` (module constant)
- `_is_transient_http(exc)` (module function)
- `_TRANSIENT_RETRY` (module-level tenacity decorator)
- `HfS3StreamDownloader._make_s3_client(cfg)`
- `HfS3StreamDownloader._make_http_client()`
- `HfS3StreamDownloader._compose_key(assignment)`
- `HfS3StreamDownloader._upload_part(s3, bucket, key, upload_id, part_no, body)`

If any are absent or differ, stop and report — don't try to fabricate them.

- [ ] **Step 2: Create `src/dlw/executor/_io.py` with extracted helpers**

```python
"""Shared HTTP + S3 helpers for HfS3StreamDownloader and DirectOffsetDownloader.

Pure utilities — no behavior change. Both downloaders import what they need.
"""
from __future__ import annotations

from typing import Any

import boto3
import httpx
from botocore.config import Config
from tenacity import (
    retry, retry_if_exception, stop_after_attempt, wait_exponential,
)

from dlw.executor.config import ExecutorSettings
from dlw.executor.downloader import Assignment   # local-scoped: Assignment lives in downloader.py
from dlw.schemas.storage import StorageConfig

_HTTP_CHUNK_BYTES = 64 * 1024


def _is_transient_http(exc: BaseException) -> bool:
    """5xx HTTP + network/timeout/protocol = transient (retry-worthy)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return isinstance(exc, (
        httpx.NetworkError, httpx.TimeoutException, httpx.ProtocolError,
    ))


_TRANSIENT_RETRY = retry(
    retry=retry_if_exception(_is_transient_http),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1.0, max=8.0),
    reraise=True,
)


def make_s3_client(settings: ExecutorSettings, cfg: StorageConfig) -> Any:
    addressing = "path" if settings.s3_path_style else "virtual"
    boto_cfg = Config(
        region_name=cfg.region,
        s3={"addressing_style": addressing},
    )
    return boto3.client(
        "s3",
        region_name=cfg.region,
        endpoint_url=cfg.endpoint_url or settings.s3_endpoint_url,
        config=boto_cfg,
    )


def make_http_client(settings: ExecutorSettings) -> httpx.AsyncClient:
    """Test seam — overridable via monkeypatch."""
    return httpx.AsyncClient(
        timeout=settings.download_timeout_seconds,
        follow_redirects=True,
    )


def compose_key(a: Assignment) -> str:
    prefix = a.storage_config.key_prefix.strip("/")
    parts = [p for p in (prefix, a.repo_id, a.revision, a.filename) if p]
    return "/".join(parts)


def upload_part(
    s3: Any, bucket: str, key: str, upload_id: str,
    part_no: int, body: bytes,
) -> str:
    return s3.upload_part(
        Bucket=bucket, Key=key, UploadId=upload_id,
        PartNumber=part_no, Body=body,
    )["ETag"]
```

Note: `_io.py` imports `Assignment` from `downloader.py`. There is no circular dependency because `downloader.py` (after Step 3 below) imports the public names (`make_s3_client`, etc.) from `_io.py`, not `Assignment`. Python resolves this lazily at call time. If you hit a real circular import, move `Assignment` and `DownloadResult` into a new `src/dlw/executor/types.py` and have both `_io.py` and `downloader.py` import from there — that's a small fixup, include it in the same commit.

- [ ] **Step 3: Refactor `src/dlw/executor/downloader.py` to use `_io.py`**

At top of `downloader.py`, replace the local helpers with:

```python
from dlw.executor._io import (
    _HTTP_CHUNK_BYTES,
    _TRANSIENT_RETRY,
    compose_key as _compose_key_io,
    make_http_client,
    make_s3_client,
    upload_part as _upload_part_io,
)
```

Inside `HfS3StreamDownloader`:

- Remove `_is_transient_http`, the local `_TRANSIENT_RETRY` decorator definition, and the module constant `_HTTP_CHUNK_BYTES` if they exist at module level. Replace usages with the imports.
- Replace `_make_s3_client(self, cfg)` body with `return make_s3_client(self._s, cfg)`.
- Replace `_make_http_client(self)` body with `return make_http_client(self._s)`.
- Replace `_compose_key(self, a)` body with `return _compose_key_io(a)`.
- Replace `_upload_part(s3, bucket, key, upload_id, part_no, body)` body (static method) with `return _upload_part_io(s3, bucket, key, upload_id, part_no, body)`.

The class API stays identical; only the implementations delegate. The `_TRANSIENT_RETRY` decorator wrapping `download()` continues to work because it's imported by name.

- [ ] **Step 4: Run W4 tests to verify zero regression**

```
uv run pytest tests/executor/ -v
```

Expected: all W4 downloader / runner / CLI tests pass unchanged.

- [ ] **Step 5: Run the full suite**

```
uv run pytest -x
```

Expected: 148 passed, 1 deselected (unchanged baseline).

- [ ] **Step 6: Commit**

```bash
git add src/dlw/executor/_io.py src/dlw/executor/downloader.py
git commit -m "refactor(executor): extract shared HTTP/S3 helpers to _io.py (W2b1 M1)"
```

---

### Task 2: `chunk_downloader.py` skeleton + `plan_chunks` + `DiskFullError` + 1 test

**Files:**
- Create: `src/dlw/executor/chunk_downloader.py`
- Create: `tests/executor/test_chunk_downloader.py`

- [ ] **Step 1: Write the failing test for `plan_chunks`**

Create `tests/executor/test_chunk_downloader.py`:

```python
"""Tests for DirectOffsetDownloader (Phase 2 W2b1)."""
from __future__ import annotations

import pytest

from dlw.executor.chunk_downloader import ChunkPlan, DiskFullError, plan_chunks


def test_plan_chunks_splits_evenly() -> None:
    plans = plan_chunks(100, 30)
    assert plans == [
        ChunkPlan(index=0, offset=0,  length=30),
        ChunkPlan(index=1, offset=30, length=30),
        ChunkPlan(index=2, offset=60, length=30),
        ChunkPlan(index=3, offset=90, length=10),
    ]


def test_plan_chunks_exact_multiple() -> None:
    plans = plan_chunks(60, 30)
    assert plans == [
        ChunkPlan(index=0, offset=0,  length=30),
        ChunkPlan(index=1, offset=30, length=30),
    ]


def test_plan_chunks_smaller_than_chunk_size() -> None:
    plans = plan_chunks(5 * 1024 * 1024, 16 * 1024 * 1024)
    assert plans == [ChunkPlan(index=0, offset=0, length=5 * 1024 * 1024)]


def test_plan_chunks_zero_file_size_returns_empty() -> None:
    assert plan_chunks(0, 16 * 1024 * 1024) == []


def test_disk_full_error_is_exception_subclass() -> None:
    """Smoke: ensure the public DiskFullError is importable and an Exception."""
    e = DiskFullError("ENOSPC writing chunk 3")
    assert isinstance(e, Exception)
    assert "ENOSPC" in str(e)
```

- [ ] **Step 2: Run tests — verify they fail with `ModuleNotFoundError`**

```
uv run pytest tests/executor/test_chunk_downloader.py -v
```

Expected: 5 collection-time errors, `ModuleNotFoundError: No module named 'dlw.executor.chunk_downloader'`.

- [ ] **Step 3: Implement `src/dlw/executor/chunk_downloader.py` skeleton**

Create `src/dlw/executor/chunk_downloader.py`:

```python
"""DirectOffsetDownloader — Phase 2 W2b1 chunk-level pipeline.

Parallel HTTP Range pulls → .parts/<subtask_id>/<idx>.bin → sequential
S3 multipart upload with streaming SHA256.

W2b1 M1 ships the public surface + plan_chunks + DiskFullError. Pass 1
(parallel download) and Pass 2 (sequential upload) are added in M2.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from dlw.executor.config import ExecutorSettings
from dlw.executor.downloader import Assignment, DownloadResult


class DiskFullError(Exception):
    """ENOSPC during chunk write. Runner translates to paused_disk_full report."""


@dataclass(frozen=True)
class ChunkPlan:
    """Slice of [0, file_size) handled by a single Range request."""
    index: int       # 0..N-1
    offset: int      # inclusive
    length: int      # bytes; last chunk may be < chunk_size


def plan_chunks(file_size: int, chunk_size: int) -> list[ChunkPlan]:
    """Split [0, file_size) into ChunkPlans of length chunk_size (last may be smaller).

    Returns [] when file_size <= 0. S3 multipart constraints (INVARIANT D-22):
    callers must ensure chunk_size >= 5 MiB; this function does not enforce it.
    """
    if file_size <= 0:
        return []
    n = math.ceil(file_size / chunk_size)
    out: list[ChunkPlan] = []
    offset = 0
    for i in range(n):
        length = min(chunk_size, file_size - offset)
        out.append(ChunkPlan(index=i, offset=offset, length=length))
        offset += length
    return out


class DirectOffsetDownloader:
    """W2b1 M1: skeleton — download() raises NotImplementedError until M2."""

    def __init__(self, *, settings: ExecutorSettings) -> None:
        self._s = settings
        # Fail fast if operator misconfigured chunk_size below S3 min part size.
        assert settings.chunk_size_bytes >= 5 * 1024 * 1024, \
            f"chunk_size_bytes ({settings.chunk_size_bytes}) < 5 MiB"

    async def download(self, *, assignment: Assignment) -> DownloadResult:
        raise NotImplementedError("DirectOffsetDownloader.download lands in W2b1 M2")
```

Note: `DirectOffsetDownloader.__init__` reads `settings.chunk_size_bytes`. That field is added to `ExecutorSettings` in Task 6 (M3). The `assert` will fail at instantiation time if the field is missing — that's correct fail-fast. M1 tests don't instantiate `DirectOffsetDownloader`; they only import `plan_chunks` / `ChunkPlan` / `DiskFullError`. The first M3 test that instantiates the class will fail until Task 6 lands.

- [ ] **Step 4: Run tests — verify all 5 pass**

```
uv run pytest tests/executor/test_chunk_downloader.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Run full suite**

```
uv run pytest -x
```

Expected: 153 passed (baseline 148 + 5 new), 1 deselected.

- [ ] **Step 6: Commit**

```bash
git add src/dlw/executor/chunk_downloader.py tests/executor/test_chunk_downloader.py
git commit -m "feat(executor): chunk_downloader skeleton + plan_chunks + DiskFullError (W2b1 M1)"
```

---

### Milestone 1 verification (self)

- [ ] `_io.py` exists with the 4 helpers + retry + constant; `downloader.py` delegates to it; W4 tests unchanged.
- [ ] `chunk_downloader.py` skeleton imports cleanly; `plan_chunks` returns correct ChunkPlans for the 4 cases tested.
- [ ] Full suite count = 153 passed.

---

## Milestone 2 — DirectOffsetDownloader pass 1 + pass 2 + parts_dir util

After M2, `DirectOffsetDownloader.download()` works end-to-end against an in-memory HF stub + moto S3. `parts_dir.py` exists with `parts_dir_for` / `cleanup_parts_dir` / `total_parts_bytes` / `startup_gc`. ENOSPC translation to `DiskFullError` is exercised.

---

### Task 3: `parts_dir.py` utility + 3 tests

**Files:**
- Create: `src/dlw/executor/parts_dir.py`
- Create: `tests/executor/test_parts_dir.py`

- [ ] **Step 1: Write failing tests for `parts_dir.py`**

Create `tests/executor/test_parts_dir.py`:

```python
"""Tests for parts_dir helpers (Phase 2 W2b1)."""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from dlw.executor.parts_dir import (
    cleanup_parts_dir,
    parts_dir_for,
    startup_gc,
    total_parts_bytes,
)


def test_parts_dir_for_returns_hex_subdir(tmp_path) -> None:
    sub_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
    p = parts_dir_for(str(tmp_path), sub_id)
    assert p == tmp_path / "12345678123456781234567812345678"


def test_cleanup_parts_dir_removes_existing_and_ignores_missing(tmp_path) -> None:
    sub_id = uuid.uuid4()
    p = parts_dir_for(str(tmp_path), sub_id)
    p.mkdir(parents=True)
    (p / "0.bin").write_bytes(b"x" * 100)
    cleanup_parts_dir(str(tmp_path), sub_id)
    assert not p.exists()
    # Idempotent on missing dir:
    cleanup_parts_dir(str(tmp_path), sub_id)  # no exception


def test_total_parts_bytes_sums_all_files(tmp_path) -> None:
    (tmp_path / "abc").mkdir()
    (tmp_path / "abc" / "0.bin").write_bytes(b"x" * 100)
    (tmp_path / "abc" / "1.bin").write_bytes(b"y" * 50)
    (tmp_path / "def").mkdir()
    (tmp_path / "def" / "0.bin").write_bytes(b"z" * 25)
    assert total_parts_bytes(str(tmp_path)) == 175


def test_total_parts_bytes_returns_zero_for_missing_root(tmp_path) -> None:
    assert total_parts_bytes(str(tmp_path / "nope")) == 0


def test_startup_gc_removes_dirs_not_in_active_set(tmp_path) -> None:
    keep_id = uuid.uuid4()
    reap_id = uuid.uuid4()
    keep_dir = parts_dir_for(str(tmp_path), keep_id)
    reap_dir = parts_dir_for(str(tmp_path), reap_id)
    keep_dir.mkdir(parents=True)
    reap_dir.mkdir(parents=True)
    (keep_dir / "0.bin").write_bytes(b"x")
    (reap_dir / "0.bin").write_bytes(b"y")

    removed = startup_gc(str(tmp_path), active_subtask_ids={keep_id})

    assert removed == 1
    assert keep_dir.exists()
    assert not reap_dir.exists()


def test_startup_gc_with_empty_active_set_removes_all(tmp_path) -> None:
    for _ in range(3):
        d = parts_dir_for(str(tmp_path), uuid.uuid4())
        d.mkdir(parents=True)
        (d / "0.bin").write_bytes(b"x")

    removed = startup_gc(str(tmp_path), active_subtask_ids=set())
    assert removed == 3
    # tmp_path itself should still exist (we only reap children, not root):
    assert tmp_path.exists()
```

- [ ] **Step 2: Run tests — verify they fail with `ModuleNotFoundError`**

```
uv run pytest tests/executor/test_parts_dir.py -v
```

Expected: 6 collection-time errors, `ModuleNotFoundError: No module named 'dlw.executor.parts_dir'`.

- [ ] **Step 3: Implement `src/dlw/executor/parts_dir.py`**

```python
"""Helpers for the .parts/ staging area used by DirectOffsetDownloader."""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path


def parts_dir_for(root: str, subtask_id: uuid.UUID) -> Path:
    """Return ${root}/${subtask_id_hex}; does NOT create."""
    return Path(root) / subtask_id.hex


def cleanup_parts_dir(root: str, subtask_id: uuid.UUID) -> None:
    """rmtree the per-subtask dir if it exists; ignore errors."""
    p = parts_dir_for(root, subtask_id)
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)


def total_parts_bytes(root: str) -> int:
    """Sum of file sizes under ${root}/, recursive; 0 if root missing."""
    base = Path(root)
    if not base.exists():
        return 0
    total = 0
    for child in base.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                continue
    return total


def startup_gc(root: str, active_subtask_ids: set[uuid.UUID]) -> int:
    """Scan ${root}/* directories; rmtree any whose hex name is NOT in
    active_subtask_ids. Returns count of removed dirs.

    Called by runner bootstrap before the poll loop starts. W2b1 callers
    pass active_subtask_ids=set() (remove everything) — the parameter
    exists for a Phase-3 multipart-resume world.
    """
    base = Path(root)
    if not base.exists():
        return 0
    active_hex = {sub_id.hex for sub_id in active_subtask_ids}
    removed = 0
    for child in base.iterdir():
        if not child.is_dir():
            continue
        if child.name in active_hex:
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed += 1
    return removed
```

- [ ] **Step 4: Run tests — verify all 6 pass**

```
uv run pytest tests/executor/test_parts_dir.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Run full suite**

```
uv run pytest -x
```

Expected: 159 passed (153 + 6 new), 1 deselected.

- [ ] **Step 6: Commit**

```bash
git add src/dlw/executor/parts_dir.py tests/executor/test_parts_dir.py
git commit -m "feat(executor): parts_dir helpers (parts_dir_for/cleanup/total_bytes/startup_gc) (W2b1 M2)"
```

---

### Task 4: `DirectOffsetDownloader` pass 1 + pass 2 + 2 tests (happy path + ENOSPC)

**Files:**
- Modify: `src/dlw/executor/config.py` (add 5 new fields needed by chunk_downloader)
- Modify: `src/dlw/executor/chunk_downloader.py`
- Modify: `tests/executor/test_chunk_downloader.py` (add 2 cases)

- [ ] **Step 1: Add fields to `ExecutorSettings`**

Open `src/dlw/executor/config.py`. After the existing `# Phase 1 W4 — pipeline tuning` block (which has `multipart_part_size_bytes` and `download_timeout_seconds`), append:

```python
    # Phase 2 W2b1 — chunk-level downloader
    chunk_level_threshold_bytes: int = Field(
        default=100 * 1024 * 1024, ge=5 * 1024 * 1024,
        description="Files >= this size use DirectOffsetDownloader; smaller use HfS3StreamDownloader.",
    )
    chunk_size_bytes: int = Field(
        default=16 * 1024 * 1024, ge=5 * 1024 * 1024,
        description="Per-chunk size for DirectOffsetDownloader (must satisfy S3 multipart 5 MiB min).",
    )
    chunk_concurrency: int = Field(
        default=4, ge=1, le=16,
        description="Parallel HTTP Range workers in DirectOffsetDownloader pass 1.",
    )
    parts_dir_path: str = Field(
        default="./parts",
        description="Local staging dir for chunk-level downloads. Configure to a writable PV in prod.",
    )
```

`pydantic_settings` reads these from `DLW_EXECUTOR_CHUNK_LEVEL_THRESHOLD_BYTES`, `DLW_EXECUTOR_CHUNK_SIZE_BYTES`, `DLW_EXECUTOR_CHUNK_CONCURRENCY`, `DLW_EXECUTOR_PARTS_DIR_PATH` (snake-case to upper-case via the existing `env_prefix="DLW_EXECUTOR_"`).

- [ ] **Step 2: Verify config import does not break the existing skeleton instantiation guard**

The M1 skeleton has `assert settings.chunk_size_bytes >= 5 * 1024 * 1024`. With the field now defined, instantiation should succeed for default config. Quick smoke:

```
uv run python -c "
from dlw.executor.config import ExecutorSettings
s = ExecutorSettings(id='test', bearer_token='t')
print('threshold:', s.chunk_level_threshold_bytes)
print('chunk_size:', s.chunk_size_bytes)
print('concurrency:', s.chunk_concurrency)
print('parts_dir:', s.parts_dir_path)
"
```

Expected: prints 104857600 / 16777216 / 4 / ./parts.

- [ ] **Step 3: Write the failing tests for pass 1 + pass 2**

Open `tests/executor/test_chunk_downloader.py` and APPEND (do not replace existing tests):

```python

import asyncio
import errno
import hashlib
import uuid
from typing import Any

import boto3
import httpx
import pytest
from moto import mock_aws

from dlw.executor.chunk_downloader import (
    DirectOffsetDownloader,
    DiskFullError,
)
from dlw.executor.config import ExecutorSettings
from dlw.executor.downloader import Assignment
from dlw.schemas.storage import StorageConfig


_FILE_SIZE = 200 * 1024 * 1024   # 200 MiB → 4 chunks at 64 MiB
_CHUNK_SIZE = 64 * 1024 * 1024
_SYNTHETIC = bytes((i * 13 + 7) % 256 for i in range(_FILE_SIZE))   # deterministic
_EXPECTED_SHA = hashlib.sha256(_SYNTHETIC).hexdigest()


def _mock_transport_for_synthetic() -> httpx.MockTransport:
    """Respond to GET with HTTP 206 Partial Content reading Range header."""
    def handler(request: httpx.Request) -> httpx.Response:
        rng = request.headers.get("Range", "")
        # Range format: "bytes=A-B"
        assert rng.startswith("bytes="), f"unexpected Range header: {rng!r}"
        a, b = rng.removeprefix("bytes=").split("-")
        start, end = int(a), int(b)
        body = _SYNTHETIC[start:end + 1]
        return httpx.Response(
            status_code=206,
            content=body,
            headers={"Content-Length": str(len(body))},
        )
    return httpx.MockTransport(handler)


@pytest.fixture
def chunk_settings(tmp_path) -> ExecutorSettings:
    return ExecutorSettings(
        id="ex-chunk-test",
        bearer_token="t",
        hf_endpoint="http://hf.fake",
        chunk_size_bytes=_CHUNK_SIZE,
        chunk_concurrency=2,
        parts_dir_path=str(tmp_path / "parts"),
        s3_region="us-east-1",
        s3_endpoint_url=None,
    )


@pytest.mark.asyncio
async def test_pass1_pass2_happy_path_with_moto(chunk_settings, monkeypatch) -> None:
    """Full pipeline: 4 chunks via MockTransport → moto multipart → sha256 matches."""
    # Patch _make_http_client to return one wired to our MockTransport.
    from dlw.executor import _io as _io_mod

    transport = _mock_transport_for_synthetic()
    monkeypatch.setattr(
        _io_mod, "make_http_client",
        lambda settings: httpx.AsyncClient(transport=transport),
    )

    storage_config = StorageConfig(
        bucket="test-bucket", region="us-east-1", endpoint_url=None,
        access_key_id="dummy", secret_access_key="dummy",
        key_prefix="phase1",
    )

    sub_id = uuid.uuid4()
    a = Assignment(
        subtask_id=sub_id,
        task_id=uuid.uuid4(),
        repo_id="owner/repo",
        revision="b" * 40,
        filename="model.bin",
        file_size=_FILE_SIZE,
        expected_sha256=None,
        storage_config=storage_config,
    )

    with mock_aws():
        # moto needs the bucket to exist before multipart calls.
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-bucket")

        d = DirectOffsetDownloader(settings=chunk_settings)
        result = await d.download(assignment=a)

    assert result.bytes_written == _FILE_SIZE
    assert result.actual_sha256 == _EXPECTED_SHA
    assert result.s3_key.endswith("model.bin")

    # parts_dir is cleaned on success.
    from dlw.executor.parts_dir import parts_dir_for
    assert not parts_dir_for(chunk_settings.parts_dir_path, sub_id).exists()


@pytest.mark.asyncio
async def test_pass1_enospc_raises_disk_full_and_leaks_parts(
    chunk_settings, monkeypatch,
) -> None:
    """Inject ENOSPC into pass-1 chunk write → DiskFullError + parts NOT cleaned."""
    from dlw.executor import _io as _io_mod
    from dlw.executor import chunk_downloader as cd_mod

    transport = _mock_transport_for_synthetic()
    monkeypatch.setattr(
        _io_mod, "make_http_client",
        lambda settings: httpx.AsyncClient(transport=transport),
    )

    # Test seam: replace _open_writer on the class to raise ENOSPC on .write().
    class _NoSpaceWriter:
        def write(self, data):
            raise OSError(errno.ENOSPC, "No space left on device")
        def __enter__(self): return self
        def __exit__(self, *_): return False

    monkeypatch.setattr(
        cd_mod.DirectOffsetDownloader,
        "_open_writer",
        staticmethod(lambda path: _NoSpaceWriter()),
    )

    storage_config = StorageConfig(
        bucket="test-bucket", region="us-east-1", endpoint_url=None,
        access_key_id="dummy", secret_access_key="dummy",
        key_prefix="phase1",
    )
    sub_id = uuid.uuid4()
    a = Assignment(
        subtask_id=sub_id,
        task_id=uuid.uuid4(),
        repo_id="owner/repo",
        revision="b" * 40,
        filename="model.bin",
        file_size=_FILE_SIZE,
        expected_sha256=None,
        storage_config=storage_config,
    )

    d = DirectOffsetDownloader(settings=chunk_settings)
    with pytest.raises(DiskFullError):
        await d.download(assignment=a)

    # parts_dir is intentionally LEAKED on DiskFullError so sweeper recovery works.
    from dlw.executor.parts_dir import parts_dir_for
    assert parts_dir_for(chunk_settings.parts_dir_path, sub_id).exists()
```

- [ ] **Step 4: Run tests — verify they fail with NotImplementedError / AttributeError**

```
uv run pytest tests/executor/test_chunk_downloader.py -v
```

Expected: 2 failures (the new test_pass1_* cases) with `NotImplementedError` (from the M1 skeleton) or `AttributeError: 'DirectOffsetDownloader' object has no attribute '_open_writer'`.

- [ ] **Step 5: Implement pass 1 + pass 2 in `src/dlw/executor/chunk_downloader.py`**

Replace the body of `src/dlw/executor/chunk_downloader.py` with:

```python
"""DirectOffsetDownloader — Phase 2 W2b1 chunk-level pipeline.

Parallel HTTP Range pulls → .parts/<subtask_id>/<idx>.bin → sequential
S3 multipart upload with streaming SHA256.
"""
from __future__ import annotations

import asyncio
import dataclasses
import errno
import hashlib
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from dlw.executor._io import (
    _HTTP_CHUNK_BYTES,
    _TRANSIENT_RETRY,
    compose_key,
    make_http_client,
    make_s3_client,
    upload_part,
)
from dlw.executor.config import ExecutorSettings
from dlw.executor.downloader import Assignment, DownloadResult
from dlw.executor.parts_dir import cleanup_parts_dir, parts_dir_for

logger = logging.getLogger(__name__)


class DiskFullError(Exception):
    """ENOSPC during chunk write. Runner translates to paused_disk_full report."""


@dataclass(frozen=True)
class ChunkPlan:
    index: int
    offset: int
    length: int


def plan_chunks(file_size: int, chunk_size: int) -> list[ChunkPlan]:
    if file_size <= 0:
        return []
    n = math.ceil(file_size / chunk_size)
    out: list[ChunkPlan] = []
    offset = 0
    for i in range(n):
        length = min(chunk_size, file_size - offset)
        out.append(ChunkPlan(index=i, offset=offset, length=length))
        offset += length
    return out


class DirectOffsetDownloader:
    def __init__(self, *, settings: ExecutorSettings) -> None:
        self._s = settings
        assert settings.chunk_size_bytes >= 5 * 1024 * 1024, \
            f"chunk_size_bytes ({settings.chunk_size_bytes}) < 5 MiB"

    @staticmethod
    def _open_writer(path: Path):
        """Test seam — overridable to inject failures (e.g. ENOSPC)."""
        return path.open("wb")

    async def _resolve_size(self, a: Assignment) -> Assignment:
        url = (f"{self._s.hf_endpoint.rstrip('/')}/{a.repo_id}"
               f"/resolve/{a.revision}/{a.filename}")
        headers: dict[str, str] = {}
        if self._s.hf_token:
            headers["Authorization"] = f"Bearer {self._s.hf_token}"
        async with make_http_client(self._s) as hc:
            resp = await hc.head(url, headers=headers)
            resp.raise_for_status()
        cl = resp.headers.get("Content-Length")
        if cl is None:
            raise RuntimeError("file_size unresolvable: HEAD returned no Content-Length")
        return dataclasses.replace(a, file_size=int(cl))

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

    async def _pass1_parallel(
        self, a: Assignment, plans: list[ChunkPlan], dest_dir: Path,
    ) -> None:
        sem = asyncio.Semaphore(self._s.chunk_concurrency)
        async with make_http_client(self._s) as hc:
            async def one(plan: ChunkPlan) -> None:
                async with sem:
                    await self._download_one_chunk(hc, a, plan, dest_dir)
            await asyncio.gather(*(one(p) for p in plans))

    @_TRANSIENT_RETRY
    async def _download_one_chunk(
        self, hc: httpx.AsyncClient, a: Assignment,
        plan: ChunkPlan, dest_dir: Path,
    ) -> None:
        url = (f"{self._s.hf_endpoint.rstrip('/')}/{a.repo_id}"
               f"/resolve/{a.revision}/{a.filename}")
        headers = {"Range": f"bytes={plan.offset}-{plan.offset + plan.length - 1}"}
        if self._s.hf_token:
            headers["Authorization"] = f"Bearer {self._s.hf_token}"
        async with hc.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()
            target = dest_dir / f"{plan.index}.bin"
            try:
                with self._open_writer(target) as f:
                    async for buf in resp.aiter_bytes(_HTTP_CHUNK_BYTES):
                        try:
                            f.write(buf)
                        except OSError as ex:
                            if ex.errno == errno.ENOSPC:
                                raise DiskFullError(
                                    f"ENOSPC writing chunk {plan.index}"
                                ) from ex
                            raise
            except DiskFullError:
                # Do NOT delete the partial; sweeper handles paused_disk_full cleanup.
                raise

    async def _pass2_upload(
        self, a: Assignment, plans: list[ChunkPlan], dest_dir: Path,
    ) -> DownloadResult:
        s3 = make_s3_client(self._s, a.storage_config)
        bucket = a.storage_config.bucket
        key = compose_key(a)
        upload_id = await asyncio.to_thread(
            lambda: s3.create_multipart_upload(Bucket=bucket, Key=key)["UploadId"]
        )
        sha = hashlib.sha256()
        parts: list[dict[str, Any]] = []
        try:
            for plan in plans:
                src = dest_dir / f"{plan.index}.bin"
                body = src.read_bytes()
                if len(body) != plan.length and plan.index != len(plans) - 1:
                    raise RuntimeError(
                        f"chunk {plan.index} short: got {len(body)} expected {plan.length}"
                    )
                sha.update(body)
                etag = await asyncio.to_thread(
                    upload_part, s3, bucket, key, upload_id,
                    plan.index + 1, body,
                )
                parts.append({"PartNumber": plan.index + 1, "ETag": etag})
            await asyncio.to_thread(lambda: s3.complete_multipart_upload(
                Bucket=bucket, Key=key, UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            ))
            cleanup_parts_dir(self._s.parts_dir_path, a.subtask_id)
            return DownloadResult(
                bytes_written=a.file_size,
                actual_sha256=sha.hexdigest(),
                s3_key=key,
            )
        except BaseException:
            try:
                await asyncio.to_thread(lambda: s3.abort_multipart_upload(
                    Bucket=bucket, Key=key, UploadId=upload_id,
                ))
            except Exception as e:
                logger.warning("multipart abort failed: %s", e)
            cleanup_parts_dir(self._s.parts_dir_path, a.subtask_id)
            raise
```

- [ ] **Step 6: Verify `tests/executor/test_chunk_downloader.py` now passes 7 cases**

```
uv run pytest tests/executor/test_chunk_downloader.py -v
```

Expected: 7 passed (5 from Task 2 + 2 new in Task 4).

If `test_pass1_pass2_happy_path_with_moto` fails because of `pytest.mark.asyncio` not being recognized: the existing test infra uses `pytest-asyncio` with `asyncio_mode = auto`; if the failing assertion is about that, change `@pytest.mark.asyncio` to a plain `async def` and the test runner handles it. Confirm by `grep -r "asyncio_mode" pyproject.toml`.

- [ ] **Step 7: Run full suite**

```
uv run pytest -x
```

Expected: 161 passed (159 + 2 new), 1 deselected.

- [ ] **Step 8: Commit**

```bash
git add src/dlw/executor/config.py src/dlw/executor/chunk_downloader.py tests/executor/test_chunk_downloader.py
git commit -m "feat(executor): DirectOffsetDownloader pass 1 + pass 2 + ENOSPC (W2b1 M2)"
```

---

### Milestone 2 verification (self)

- [ ] `chunk_downloader.py` round-trips a 200 MiB synthetic file through MockTransport + moto and produces matching SHA256.
- [ ] `DiskFullError` propagates with parts directory intact.
- [ ] Full suite count = 161 passed.

---

## Milestone 3 — Runner dispatch + heartbeat parts_dir_bytes + startup GC

After M3, `ExecutorRunner` takes both downloaders, dispatches by `file_size`, calls `startup_gc(root, set())` before joining, and reports real `parts_dir_bytes` in heartbeats.

---

### Task 5: Runner dispatch + cli.py + heartbeat parts_dir_bytes + startup GC + 2 tests

**Files:**
- Modify: `src/dlw/executor/runner.py`
- Modify: `src/dlw/executor/cli.py`
- Modify: `tests/executor/test_runner.py` (~4-5 setup updates)
- Modify: `tests/executor/test_cli.py` (1 setup update if needed)
- Create: `tests/executor/test_runner_dispatch.py`

- [ ] **Step 1: Write the failing dispatch tests**

Create `tests/executor/test_runner_dispatch.py`:

```python
"""Tests for ExecutorRunner._choose_downloader (Phase 2 W2b1 M3)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dlw.executor.config import ExecutorSettings
from dlw.executor.runner import ExecutorRunner


def _runner_with_mocks(file_size_threshold: int):
    settings = ExecutorSettings(
        id="ex-dispatch",
        bearer_token="t",
        chunk_level_threshold_bytes=file_size_threshold,
    )
    client = MagicMock()
    stream = MagicMock()
    stream.download = AsyncMock()
    chunk = MagicMock()
    chunk.download = AsyncMock()
    runner = ExecutorRunner(
        settings=settings, client=client,
        stream_downloader=stream, chunk_downloader=chunk,
    )
    return runner, stream, chunk


def test_runner_picks_stream_for_small_file() -> None:
    runner, stream, chunk = _runner_with_mocks(file_size_threshold=100 * 1024 * 1024)
    chosen = runner._choose_downloader(file_size=50 * 1024 * 1024)
    assert chosen is stream


def test_runner_picks_chunk_for_large_file_and_for_unknown() -> None:
    runner, stream, chunk = _runner_with_mocks(file_size_threshold=100 * 1024 * 1024)
    assert runner._choose_downloader(file_size=200 * 1024 * 1024) is chunk
    assert runner._choose_downloader(file_size=None) is chunk
    # Boundary: exactly threshold → chunk (uses >= comparison).
    assert runner._choose_downloader(file_size=100 * 1024 * 1024) is chunk
```

- [ ] **Step 2: Run the new test — expect `TypeError` on constructor**

```
uv run pytest tests/executor/test_runner_dispatch.py -v
```

Expected: 2 failures with `TypeError: __init__() got an unexpected keyword argument 'stream_downloader'` (W4 runner still uses `downloader=`).

- [ ] **Step 3: Modify `src/dlw/executor/runner.py`**

Open the file. Change:

```python
from dlw.executor.client import ControllerClient
from dlw.executor.config import ExecutorSettings
from dlw.executor.downloader import HfS3StreamDownloader

logger = logging.getLogger(__name__)


class ExecutorRunner:
    def __init__(
        self,
        *,
        settings: ExecutorSettings,
        client: ControllerClient,
        downloader: HfS3StreamDownloader,
    ) -> None:
        self._s = settings
        self._client = client
        self._downloader = downloader
        self._shutdown = asyncio.Event()
```

to:

```python
from dlw.executor.client import ControllerClient
from dlw.executor.chunk_downloader import DirectOffsetDownloader, DiskFullError
from dlw.executor.config import ExecutorSettings
from dlw.executor.downloader import HfS3StreamDownloader
from dlw.executor.parts_dir import startup_gc, total_parts_bytes

logger = logging.getLogger(__name__)


class ExecutorRunner:
    def __init__(
        self,
        *,
        settings: ExecutorSettings,
        client: ControllerClient,
        stream_downloader: HfS3StreamDownloader,
        chunk_downloader: DirectOffsetDownloader,
    ) -> None:
        self._s = settings
        self._client = client
        self._stream_downloader = stream_downloader
        self._chunk_downloader = chunk_downloader
        self._shutdown = asyncio.Event()

    def _choose_downloader(self, file_size: int | None):
        threshold = self._s.chunk_level_threshold_bytes
        if file_size is None or file_size >= threshold:
            return self._chunk_downloader
        return self._stream_downloader
```

In `run()`, before the `# 1. Join (one-shot)` line, add:

```python
        # W2b1 §3.2: clean up any stale .parts/ dirs from a previous crash.
        # active_subtask_ids=set() removes everything — W2b1 has no resume.
        removed = startup_gc(self._s.parts_dir_path, active_subtask_ids=set())
        if removed:
            logger.info("startup_gc removed %d stale parts dirs", removed)
```

In `_heartbeat_loop`, change:

```python
                await self._client.heartbeat(
                    executor_id=self._s.id, health_score=100, parts_dir_bytes=0
                )
```

to:

```python
                await self._client.heartbeat(
                    executor_id=self._s.id, health_score=100,
                    parts_dir_bytes=total_parts_bytes(self._s.parts_dir_path),
                )
```

In `_execute_subtask`, change:

```python
            assignment = Assignment(
                subtask_id=sub_id,
                task_id=uuid.UUID(subtask["task_id"]),
                repo_id=repo_id,
                revision=revision,
                filename=subtask["filename"],
                file_size=subtask.get("file_size"),
                expected_sha256=subtask.get("expected_sha256"),
                storage_config=StorageConfig(**storage_config),
            )
            result = await self._downloader.download(assignment=assignment)
            await self._client.report(
                subtask_id=sub_id,
                status="succeeded",
                assignment_token=assignment_token,
                actual_sha256=result.actual_sha256,
                bytes_downloaded=result.bytes_written,
                s3_key=result.s3_key,
            )
        except Exception as e:
```

to:

```python
            assignment = Assignment(
                subtask_id=sub_id,
                task_id=uuid.UUID(subtask["task_id"]),
                repo_id=repo_id,
                revision=revision,
                filename=subtask["filename"],
                file_size=subtask.get("file_size"),
                expected_sha256=subtask.get("expected_sha256"),
                storage_config=StorageConfig(**storage_config),
            )
            downloader = self._choose_downloader(assignment.file_size)
            try:
                result = await downloader.download(assignment=assignment)
            except DiskFullError as e:
                logger.warning("subtask %s paused_disk_full: %s", sub_id, e)
                await self._client.report(
                    subtask_id=sub_id,
                    status="paused_disk_full",
                    assignment_token=assignment_token,
                    actual_sha256=None,
                    bytes_downloaded=0,
                    error=str(e),
                )
                return
            await self._client.report(
                subtask_id=sub_id,
                status="succeeded",
                assignment_token=assignment_token,
                actual_sha256=result.actual_sha256,
                bytes_downloaded=result.bytes_written,
                s3_key=result.s3_key,
            )
        except Exception as e:
```

- [ ] **Step 4: Run the dispatch tests — verify they pass**

```
uv run pytest tests/executor/test_runner_dispatch.py -v
```

Expected: 2 passed (the two test functions; the second one bundles 3 assertions).

- [ ] **Step 5: Update `src/dlw/executor/cli.py` to build both downloaders**

Open `src/dlw/executor/cli.py`. Find where it currently builds a downloader and creates the `ExecutorRunner` (search for `HfS3StreamDownloader(`). Replace with:

```python
from dlw.executor.chunk_downloader import DirectOffsetDownloader
from dlw.executor.downloader import HfS3StreamDownloader

# ... inside the function that constructs the runner ...
stream = HfS3StreamDownloader(settings=settings)
chunk = DirectOffsetDownloader(settings=settings)
runner = ExecutorRunner(
    settings=settings, client=client,
    stream_downloader=stream, chunk_downloader=chunk,
)
```

If `cli.py` currently does `downloader = MockDownloader(...)` for testing, leave that path alone but adapt the production branch.

- [ ] **Step 6: Update existing W4 runner / cli tests**

Open `tests/executor/test_runner.py`. Find every site that calls:

```python
ExecutorRunner(settings=..., client=..., downloader=...)
```

and change to:

```python
ExecutorRunner(settings=..., client=..., stream_downloader=..., chunk_downloader=...)
```

Where the test only used `downloader=stream`, pass `chunk_downloader=MagicMock()` (the test doesn't exercise chunk path).

Open `tests/executor/test_cli.py`. If it asserts a specific constructor argument set on `ExecutorRunner`, update similarly.

- [ ] **Step 7: Run executor tests**

```
uv run pytest tests/executor/ -v
```

Expected: all pass — `test_runner_dispatch.py` (3 assertions), updated `test_runner.py`, updated `test_cli.py`, `test_chunk_downloader.py`, `test_parts_dir.py`, plus the unchanged W4 `test_downloader.py` and `test_client.py`.

- [ ] **Step 8: Run full suite**

```
uv run pytest -x
```

Expected: 163 passed (161 + 2 new dispatch tests; the W4 setup edits don't change test count).

- [ ] **Step 9: Commit**

```bash
git add src/dlw/executor/runner.py src/dlw/executor/cli.py tests/executor/
git commit -m "feat(executor): runner dispatch + startup GC + heartbeat parts_dir_bytes (W2b1 M3)"
```

---

### Milestone 3 verification (self)

- [ ] `ExecutorRunner` constructor takes both downloaders; dispatch tests pass.
- [ ] `cli.py` builds both; running the executor CLI does not crash.
- [ ] Existing W4 `test_runner.py` / `test_cli.py` still green after setup edits.
- [ ] Full suite count = 163 passed.

---

## Milestone 4 — Scheduler pre-flight + complete_subtask paused_disk_full + sweep + lint + OpenAPI + PR

After M4, the controller refuses too-big subtasks via candidate scan, accepts `paused_disk_full` reports, recovers them via `sweep_paused_disk_full` on the lifespan loop, and the OpenAPI + lint surface match the new value domain.

---

### Task 6: Scheduler disk pre-flight + 2 tests

**Files:**
- Modify: `src/dlw/services/scheduler.py`
- Create: `tests/services/test_scheduler_disk_preflight.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/services/test_scheduler_disk_preflight.py`:

```python
"""Tests for claim_one_subtask disk pre-flight (Phase 2 W2b1 §3.6)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.executor import Executor
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User
from dlw.services.scheduler import claim_one_subtask


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def env(db_session: AsyncSession):
    """Seed minimum FK rows (tenant/project/user/storage). Mirror of the
    inline `env` fixture in tests/services/test_scheduler_host_affinity.py."""
    db_session.add(Tenant(id=1, slug="d", display_name="D"))
    await db_session.flush()
    db_session.add(Project(id=1, tenant_id=1, name="d"))
    db_session.add(User(id=1, tenant_id=1, oidc_subject="d",
                        email="d@l", role="tenant_admin"))
    db_session.add(StorageBackend(id=1, tenant_id=1, name="d",
                                  backend_type="s3", config_encrypted=b""))
    await db_session.flush()


async def _seed_pending_with_size(
    session: AsyncSession, file_size: int, filename: str = "model.bin",
) -> tuple[DownloadTask, FileSubTask]:
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="b" * 40, storage_id=1,
        path_template="t", priority=1, status="pending",
    )
    session.add(task)
    await session.flush()
    sub = FileSubTask(
        task_id=task.id, tenant_id=1, filename=filename,
        file_size=file_size, status="pending",
    )
    session.add(sub)
    await session.flush()
    return task, sub


@pytest.mark.slow
async def test_claim_skips_subtask_too_big_for_executor(
    db_session: AsyncSession, env,
) -> None:
    """Executor has 1 GB free; subtask is 5 GB → no claim."""
    db_session.add(Executor(
        id="ex-tiny", host_id="host-1", cert_fingerprint="x",
        status="healthy", epoch=1,
        disk_free_gb=1, parts_dir_bytes=0,
    ))
    await _seed_pending_with_size(db_session, file_size=5 * 1024**3)

    sub, token = await claim_one_subtask(db_session, "ex-tiny", 1)
    assert sub is None and token is None


@pytest.mark.slow
async def test_claim_picks_next_candidate_if_first_too_big(
    db_session: AsyncSession, env,
) -> None:
    """Executor has 500 MiB free. Two pending: 5 GB (first), 100 MiB (second).
    Claim must return the 100 MiB one."""
    db_session.add(Executor(
        id="ex-half-gb", host_id="host-2", cert_fingerprint="x",
        status="healthy", epoch=1,
        disk_free_gb=0,                 # 0 GiB integer
        parts_dir_bytes=0,
    ))
    # Override via raw column write: 500 MiB ~= 0.5 GiB but disk_free_gb is int.
    # Use a hack: bump to 1 GiB, then we set safety margin via env in this test.
    # Cleaner: just use 1 GiB free and a 100 MiB candidate with 200 MiB safety.
    big_task, big_sub = await _seed_pending_with_size(db_session, file_size=5 * 1024**3, filename="big.bin")
    # Make the big sub older so it's the first candidate by created_at order.
    import asyncio
    await asyncio.sleep(0.01)
    small_task, small_sub = await _seed_pending_with_size(db_session, file_size=100 * 1024**2, filename="small.bin")

    ex = await db_session.get(Executor, "ex-half-gb")
    ex.disk_free_gb = 1   # 1 GiB free
    await db_session.flush()

    sub, token = await claim_one_subtask(db_session, "ex-half-gb", 1)
    assert sub is not None
    assert sub.filename == "small.bin"
    assert token is not None
```

Note: `test_claim_picks_next_candidate_if_first_too_big` depends on `created_at` ordering. The 10 ms sleep is sufficient since the column is `TIMESTAMPTZ` with `server_default=now()` and Postgres timestamps are microsecond-precise. If flake reports come in, switch to explicit `created_at=...` arguments.

- [ ] **Step 2: Run tests — verify they fail because the current W2a scheduler ignores disk**

```
uv run pytest tests/services/test_scheduler_disk_preflight.py -v
```

Expected: 2 failures (first test: claim succeeds when it shouldn't; second test: claim returns the big subtask instead of the small one).

- [ ] **Step 3: Modify `src/dlw/services/scheduler.py`**

At the top of the file, add module-level constants near the existing imports:

```python
import os

_K_CANDIDATES = int(os.environ.get("DLW_SCHEDULER_CANDIDATES", "16"))
_DISK_SAFETY_MARGIN_BYTES = int(os.environ.get("DLW_DISK_SAFETY_MARGIN_BYTES", str(200 * 1024 * 1024)))
```

Replace the W2a `claim_one_subtask` body (everything inside the function from `from sqlalchemy.orm import aliased` to the `return sub, token` at the end) with:

```python
    from sqlalchemy.orm import aliased

    # (a) Self-eligibility — unchanged from W2a.
    e_self = await session.get(Executor, executor_id)
    if e_self is None or e_self.status not in ("healthy", "degraded"):
        return None, None

    # (b) Reverse host-affinity — unchanged from W2a.
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

    # (c) W2b1: candidate scan with disk pre-flight.
    GiB = 1024 ** 3
    free_bytes = (e_self.disk_free_gb or 0) * GiB - (e_self.parts_dir_bytes or 0)

    stmt = (
        select(FileSubTask)
        .where(FileSubTask.status == "pending")
        .where(~same_host_holds)
        .order_by(FileSubTask.created_at)
        .limit(_K_CANDIDATES)
        .with_for_update(skip_locked=True)
    )
    candidates = (await session.execute(stmt)).scalars().all()
    for sub in candidates:
        size = sub.file_size or 0
        if size + _DISK_SAFETY_MARGIN_BYTES <= free_bytes:
            token = uuid.uuid4()
            sub.status = "assigned"
            sub.executor_id = executor_id
            sub.executor_epoch = executor_epoch
            sub.assignment_token = token
            sub.assigned_at = datetime.now(UTC)
            return sub, token
    # No candidate fit; other locked rows release on session commit.
    return None, None
```

- [ ] **Step 4: Run the new tests — verify they pass**

```
uv run pytest tests/services/test_scheduler_disk_preflight.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Run all scheduler tests for regression**

```
uv run pytest tests/services/test_scheduler.py tests/services/test_scheduler_host_affinity.py tests/services/test_scheduler_disk_preflight.py -v
```

Expected: all pass. If a W2a host-affinity test fails because its executor row doesn't set `disk_free_gb`, fix the test setup to set `disk_free_gb=100` (plenty of headroom).

- [ ] **Step 6: Run the full suite**

```
uv run pytest -x
```

Expected: 165 passed (163 + 2 new).

- [ ] **Step 7: Commit**

```bash
git add src/dlw/services/scheduler.py tests/services/test_scheduler_disk_preflight.py
git commit -m "feat(scheduler): candidate scan + disk pre-flight (W2b1 M4)"
```

---

### Task 7: `complete_subtask` paused_disk_full branch + SubTaskReport widening + sweep_paused_disk_full + lint extension + 1 test

**Files:**
- Modify: `src/dlw/services/scheduler.py` (complete_subtask)
- Modify: `src/dlw/services/recovery.py` (+sweep_paused_disk_full)
- Modify: `src/dlw/schemas/subtask.py` (Literal widening)
- Modify: `src/dlw/main.py` (call new sweeper from loop)
- Modify: `tools/lint_invariants.py` (+check_subtask_status_domain)
- Create: `tests/services/test_sweep_paused_disk_full.py`

- [ ] **Step 1: Write the failing sweep test**

Create `tests/services/test_sweep_paused_disk_full.py`:

```python
"""Tests for sweep_paused_disk_full (Phase 2 W2b1 §3.7)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.executor import Executor
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User
from dlw.services.recovery import sweep_paused_disk_full


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def env(db_session: AsyncSession):
    """Seed minimum FK rows. Mirror of test_scheduler_host_affinity.env."""
    db_session.add(Tenant(id=1, slug="d", display_name="D"))
    await db_session.flush()
    db_session.add(Project(id=1, tenant_id=1, name="d"))
    db_session.add(User(id=1, tenant_id=1, oidc_subject="d",
                        email="d@l", role="tenant_admin"))
    db_session.add(StorageBackend(id=1, tenant_id=1, name="d",
                                  backend_type="s3", config_encrypted=b""))
    await db_session.flush()


@pytest.mark.slow
async def test_sweep_recovers_paused_disk_full_when_disk_free_increases(
    db_session: AsyncSession, env,
) -> None:
    """Seed paused_disk_full subtask; bump disk_free_gb; run sweep; assert pending."""
    ex = Executor(
        id="ex-recovered", host_id="host-r", cert_fingerprint="x",
        status="healthy", epoch=1,
        disk_free_gb=0, parts_dir_bytes=0,
    )
    db_session.add(ex)
    await db_session.flush()

    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="b" * 40, storage_id=1,
        path_template="t", priority=1, status="pending",
    )
    db_session.add(task)
    await db_session.flush()

    sub = FileSubTask(
        task_id=task.id, tenant_id=1, filename="big.bin",
        file_size=100 * 1024 * 1024, status="paused_disk_full",
        executor_id="ex-recovered", executor_epoch=1,
    )
    db_session.add(sub)
    await db_session.flush()

    # Initial sweep — disk still tight, no recovery.
    recovered = await sweep_paused_disk_full(db_session)
    assert recovered == 0
    fresh = await db_session.get(FileSubTask, sub.id)
    assert fresh.status == "paused_disk_full"

    # Bump disk_free_gb; sweep again.
    ex.disk_free_gb = 10
    await db_session.flush()
    recovered = await sweep_paused_disk_full(db_session)
    assert recovered == 1
    fresh = await db_session.get(FileSubTask, sub.id)
    assert fresh.status == "pending"
    assert fresh.executor_id is None
    assert fresh.executor_epoch is None
```

- [ ] **Step 2: Run the test — verify it fails with `ImportError`**

```
uv run pytest tests/services/test_sweep_paused_disk_full.py -v
```

Expected: 1 collection-time error, `ImportError: cannot import name 'sweep_paused_disk_full' from 'dlw.services.recovery'`.

- [ ] **Step 3: Add `sweep_paused_disk_full` to `src/dlw/services/recovery.py`**

At the end of the file, append:

```python
async def sweep_paused_disk_full(session: AsyncSession) -> int:
    """W2b1 §3.7: recover paused_disk_full subtasks whose owning executor now
    has enough disk. Returns count recovered to pending. Caller commits."""
    from dlw.services.scheduler import _DISK_SAFETY_MARGIN_BYTES

    GiB = 1024 ** 3

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
        if size + _DISK_SAFETY_MARGIN_BYTES <= free_bytes:
            sub.status = "pending"
            sub.executor_id = None
            sub.executor_epoch = None
            sub.assignment_token = None
            sub.assigned_at = None
            recovered += 1
    return recovered
```

- [ ] **Step 4: Add the `paused_disk_full` branch to `complete_subtask` in `src/dlw/services/scheduler.py`**

Find `complete_subtask`. Locate the W1 fence check block (the `if executor_epoch is not None and sub.executor_epoch != executor_epoch:` block that raises ValueError). Just below that block (BEFORE the W4 sha256 verify gate), insert:

```python
    # W2b1: paused_disk_full short-circuits — environmental, not a quality signal.
    if final_status == "paused_disk_full":
        sub.status = "paused_disk_full"
        sub.executor_id = None
        sub.executor_epoch = None
        sub.assignment_token = None
        sub.assigned_at = None
        sub.last_error = error
        # Don't transition parent task; don't call transition_executor.
        parent = await session.get(DownloadTask, sub.task_id)
        return sub, parent
```

- [ ] **Step 5: Widen `SubTaskReport.status` in `src/dlw/schemas/subtask.py`**

Open `src/dlw/schemas/subtask.py`. Find `SubTaskReport`:

```python
class SubTaskReport(BaseModel):
    status: Literal["succeeded", "failed"]
    ...
```

Change to:

```python
class SubTaskReport(BaseModel):
    status: Literal["succeeded", "failed", "paused_disk_full"]
    ...
```

- [ ] **Step 6: Wire `sweep_paused_disk_full` into the lifespan loop**

Open `src/dlw/main.py`. Find `_sweep_loop_main`. Update the import + body:

```python
async def _sweep_loop_main(factory) -> None:
    """Background task: every N seconds, transition stale executors + reclaim
    + recover paused_disk_full subtasks."""
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

- [ ] **Step 7: Run the new sweep test — verify it passes**

```
uv run pytest tests/services/test_sweep_paused_disk_full.py -v
```

Expected: 1 passed.

- [ ] **Step 8: Run all controller-side tests for regression**

```
uv run pytest tests/services/ tests/api/ -v
```

Expected: all pass. If a complete_subtask test in `tests/services/test_scheduler.py` was relying on the W4-only path, the new paused_disk_full branch is a no-op for `final_status in ('succeeded', 'failed')`, so no regression should occur.

- [ ] **Step 9: Run full suite**

```
uv run pytest -x
```

Expected: 166 passed (165 + 1 new sweep test).

- [ ] **Step 10: Commit**

```bash
git add src/dlw/services/scheduler.py src/dlw/services/recovery.py \
        src/dlw/schemas/subtask.py src/dlw/main.py \
        tests/services/test_sweep_paused_disk_full.py
git commit -m "feat(controller): complete_subtask paused_disk_full + sweep + main loop (W2b1 M4)"
```

---

### Task 8: Lint extension + OpenAPI enum widening + operator doc

**Files:**
- Modify: `tools/lint_invariants.py` (+check_subtask_status_domain)
- Modify: `api/openapi.yaml` (SubTaskReport.status enum)
- Modify: `docs/operator/` (one-line note)

- [ ] **Step 1: Add `check_subtask_status_domain` to `tools/lint_invariants.py`**

Open `tools/lint_invariants.py`. After the existing `check_executor_status_domain` function (and its `VALID_EXECUTOR_STATUS` set), add:

```python
VALID_SUBTASK_STATUS = {
    "pending", "assigned", "succeeded", "failed", "cancelled", "paused_disk_full",
}


def check_subtask_status_domain() -> list[str]:
    """Lint string literals assigned to a `status` kwarg/attr in service modules
    where FileSubTask rows are mutated. Identical AST patterns to W2a's
    check_executor_status_domain; only the value-domain set + scanned files differ."""
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
            if isinstance(node, _ast.keyword) and node.arg == "status":
                if isinstance(node.value, _ast.Constant) and isinstance(node.value.value, str):
                    if node.value.value not in VALID_SUBTASK_STATUS:
                        errors.append(
                            f"{f.relative_to(ROOT)}:{node.value.lineno}: "
                            f"invalid subtask status: {node.value.value!r}"
                        )
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

In `main()`, find the existing `failures.extend(check_executor_status_domain())` line and add immediately below:

```python
    failures.extend(check_subtask_status_domain())
```

- [ ] **Step 2: Run lint to verify clean on production tree**

```
python tools/lint_invariants.py
```

Expected: exits 0 with the OK banner. If a violation appears for a literal like `"verified"` somewhere — that's not in `VALID_SUBTASK_STATUS` for W2b1 — investigate. If a real test mutator uses an unexpected literal, fix the source rather than weakening the set.

- [ ] **Step 3: Run lint_invariants self-tests**

```
uv run pytest tools/test_lint_invariants.py -v
```

Expected: all pass (no regression from the new helper).

- [ ] **Step 4: Widen `SubTaskReport.status` in `api/openapi.yaml`**

Open `api/openapi.yaml`. Find the `SubTaskReport` schema (search for `SubTaskReport:`). Locate its `status:` property. The W1 form may be:

```yaml
        status:
          type: string
          enum: [succeeded, failed]
```

or just `type: string` without an enum. Change to:

```yaml
        status:
          type: string
          enum: [succeeded, failed, paused_disk_full]
          description: Subtask completion status reported by executor.
```

If `ExecutorHeartbeat.parts_dir_bytes` has a description, append: ` Controller uses this for disk pre-flight checks.` (Single-line append to existing description; don't restructure.)

- [ ] **Step 5: Add operator documentation note**

Find the existing operator docs root: `ls docs/operator/` (run from repo root). If `oidc-setup.md` is there from W5, the operator docs root is `docs/operator/`. Create or open `docs/operator/executor-runbook.md` (create if absent) and append (or create the file with) this section:

```markdown
## `.parts/` staging area (Phase 2 W2b1+)

Executors that handle files ≥ 100 MiB stage downloads into
`${DLW_EXECUTOR_PARTS_DIR_PATH}/` (default `./parts`) before uploading
to S3. In production:

- Mount a writable PV at the configured path; sized to at least the
  largest expected single file + 20% headroom.
- Operator must `chown` the dir to the user running the executor
  process.
- Controller's `sweep_paused_disk_full` recovers subtasks back to
  `pending` if disk frees up. No manual intervention needed for
  transient ENOSPC.
```

If `docs/operator/` doesn't exist, create the directory + file. Verify by `ls docs/operator/executor-runbook.md`.

- [ ] **Step 6: Run the full suite + lint once more**

```
uv run pytest -x
python tools/lint_invariants.py
python tools/lint_no_direct_status_write.py
```

Expected: 166 passed, 1 deselected; both lints clean.

- [ ] **Step 7: Commit**

```bash
git add tools/lint_invariants.py api/openapi.yaml docs/operator/
git commit -m "ci(lint): subtask status value-domain + OpenAPI enum + operator runbook (W2b1 M4)"
```

---

### Task 9: Push branch + open PR + monitor CI (controller does this)

- [ ] **Step 1: Confirm branch state**

```bash
git status
git log main..HEAD --oneline
```

Expected: clean working tree; ~10 commits on the branch (1 spec + 9 task commits — Task 9 itself is just push/PR).

- [ ] **Step 2: Push**

```bash
git push -u origin feat/phase-2-w2b1-chunk-level-downloader
```

- [ ] **Step 3: Open the PR**

```bash
gh pr create \
  --title "Phase 2 Week 2b1 — chunk-level downloader + disk-aware scheduling" \
  --body "$(cat <<'EOF'
## Summary

W2b1 half of `docs/v2.0/08-mvp-roadmap.md` §2.6 Day 5:

- **DirectOffsetDownloader.** New `src/dlw/executor/chunk_downloader.py`. Parallel HTTP Range pulls (default 4 workers, 16 MiB chunks) → `.parts/<subtask_id>/<idx>.bin` → sequential SHA256 + S3 multipart upload. Runner dispatches by `file_size`: files < 100 MiB stay on W4's `HfS3StreamDownloader`; ≥ 100 MiB (or unknown) use the new path. Helpers extracted to `_io.py` (DRY).
- **`.parts/` lifecycle.** `parts_dir.py` provides `parts_dir_for` / `cleanup_parts_dir` / `total_parts_bytes` / `startup_gc`. Runner reaps stale dirs at startup (W2b1 has no resume, so `active_subtask_ids=set()`). Heartbeat reports real `parts_dir_bytes` instead of W4's hard-coded 0.
- **D7 paused_disk_full.** `ENOSPC` catch → `DiskFullError` → runner reports `status="paused_disk_full"`. Scheduler `claim_one_subtask` adds candidate scan (LIMIT 16) with disk pre-flight (`disk_free_gb*GiB - parts_dir_bytes >= file_size + 200 MiB safety`). `sweep_paused_disk_full` (called from W2a `_sweep_loop_main`) recovers subtasks to `pending` when disk frees up.
- **CI / docs.** `tools/lint_invariants.py` gains `check_subtask_status_domain` covering `{pending, assigned, succeeded, failed, cancelled, paused_disk_full}`. OpenAPI `SubTaskReport.status` enum widens. `docs/operator/executor-runbook.md` documents the `DLW_EXECUTOR_PARTS_DIR_PATH` PV requirement.

Spec: `docs/superpowers/specs/2026-05-13-phase-2-w2b1-chunk-level-downloader-design.md`.
Plan: `docs/superpowers/plans/2026-05-13-phase-2-w2b1-chunk-level-downloader.md`.

W2b2 (cancel API + paused_external) is a follow-up spec.

## Test plan

- [x] Backend pytest: baseline + 18 new (5 plan_chunks/skel + 2 chunk_downloader happy/ENOSPC + 6 parts_dir + 2 runner dispatch + 2 scheduler disk pre-flight + 1 sweep_paused_disk_full) = 166 passed, 1 deselected. Zero regressions.
- [x] `_io.py` extraction preserves W4 `HfS3StreamDownloader` behavior; W4 tests unchanged.
- [x] `tools/lint_no_direct_status_write.py` returns 0; `tools/lint_invariants.py` returns 0 (with new subtask domain check).
- [x] OpenAPI `SubTaskReport.status` enum lists 3 values; spectral CI passes.
- [x] Zero alembic migrations (all required columns shipped in W1).
- [x] No new runtime / dev deps; no new CI jobs.
- [x] FastAPI lifespan smoke: `_sweep_loop_main` now calls both `sweep_executor_timeouts` and `sweep_paused_disk_full`.

## Out of scope (deferred — see spec §1.2)

POST `/tasks/{id}/cancel` + `cancelling` + paused_external (W2b2); `verified` subtask state (Phase 3); multipart resume across crashes (Phase 3); BLAKE3 (v2.2); dynamic concurrency / NIC-aware (Phase 3); multi-source chunk-level (Phase 3 v2.1); P-004 baseline (after W3).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Monitor CI**

```bash
gh pr checks $(gh pr view --json number -q .number) --watch
```

Expected: 12 checks pass. If any fail:

- **pytest** — re-run locally first. moto + asyncio interactions occasionally surface ordering issues on CI that don't reproduce locally; pin moto version if needed (it's already pinned via uv lock).
- **OpenAPI lint** — spectral may complain about the enum addition if other operations reference the old shape. Search `api/openapi.yaml` for any inline references to `succeeded` / `failed` enums that should also widen.
- **Invariant + cross-ref lint** — the new `check_subtask_status_domain` may catch a forgotten literal somewhere. Fix the source.

---

### Milestone 4 verification (self)

- [ ] PR opened; CI 12/12 green.
- [ ] No diff outside the File Structure list (`gh pr diff --name-only`).
- [ ] All 18 new tests pass; no W4 / W1 / W2a regressions.

---

## Definition of Done

- [ ] All 9 implementation tasks committed on `feat/phase-2-w2b1-chunk-level-downloader`.
- [ ] PR opened, CI 12/12 green.
- [ ] 18 new pytest tests pass; baseline + 18 total = 166.
- [ ] `_io.py` shared by both downloaders; W4 tests unchanged.
- [ ] `chunk_downloader.py` end-to-end works against moto + httpx MockTransport.
- [ ] `DirectOffsetDownloader` produces SHA256 matching `hashlib.sha256(full_bytes).hexdigest()`.
- [ ] `startup_gc(root, set())` removes all pre-existing per-subtask dirs.
- [ ] Heartbeat reports non-zero `parts_dir_bytes` when chunk work is in flight.
- [ ] `claim_one_subtask` candidate scan + disk pre-flight working in both tests.
- [ ] `sweep_paused_disk_full` wired into `_sweep_loop_main`; recovers when disk frees up.
- [ ] OpenAPI `SubTaskReport.status` lists `[succeeded, failed, paused_disk_full]`.
- [ ] `tools/lint_invariants.py` includes `check_subtask_status_domain`; clean on `main`.
- [ ] `docs/operator/executor-runbook.md` (or equivalent) documents `DLW_EXECUTOR_PARTS_DIR_PATH`.
- [ ] No new runtime / dev deps; no new CI jobs; zero alembic migrations.

---

## Plan Revisions Log

(Empty on first draft.)

| Tag | Severity | Issue | Fix applied |
|-----|----------|-------|-------------|
| _(none yet)_ | | | |

---

## References

- Spec: `docs/superpowers/specs/2026-05-13-phase-2-w2b1-chunk-level-downloader-design.md`
- Predecessor specs:
  - `docs/superpowers/specs/2026-05-11-phase-2-week-1-fence-token-recovery-design.md` (W1)
  - `docs/superpowers/specs/2026-05-13-phase-2-w2a-scheduler-state-machine-design.md` (W2a)
- Predecessor plans:
  - `docs/superpowers/plans/2026-05-11-phase-2-week-1-fence-token-recovery.md` (W1)
  - `docs/superpowers/plans/2026-05-13-phase-2-w2a-scheduler-state-machine.md` (W2a)
- Roadmap source: `docs/v2.0/08-mvp-roadmap.md` §2.6 Phase 2 W2 — Day 5 + D7 from Day 4
- Architecture: `docs/v2.0/01-architecture.md` §5.2 (Executor) + §5.3 (multi-executor host NIC sharing)
- Distributed correctness: `docs/v2.0/03-distributed-correctness.md` §9 (paused_disk_full, D7)
- Invariants: `docs/v2.0/INVARIANTS.md` §D-22 (S3 multipart constraints)
- W2a PR (merged): https://github.com/l17728/modelpull/pull/9 (squash `8683b03`)
