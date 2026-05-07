# Phase 1 Week 3: Executor Process Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A standalone executor process that connects to the running controller, registers itself, polls for subtasks, "downloads" mock files, and reports completion. End of Week 3: `docker compose up` brings up controller + executor (+ existing PG), and a single `POST /api/v1/tasks` triggers a real autonomous run that finishes within seconds.

**Architecture:** New subpackage `src/dlw/executor/` inside the existing modelpull monorepo. Async (httpx + asyncio). Long-running main loop with graceful SIGTERM handling. Mock downloader generates a file of `file_size` random bytes and computes sha256 on the fly (replaced with real HuggingFace Hub fetch in Week 4 plan). CLI entry point `dlw-executor` registered via pyproject `[project.scripts]`.

**Tech Stack:** Same as Week 2 — Python 3.12, async (asyncio), httpx (already in deps), structlog, pydantic-settings. Adds `tenacity` for retry-with-backoff on transient HTTP failures. No new heavy deps.

**Scope:** Executor process only. UI scaffold (originally paired with Week 3 in `08-mvp-roadmap.md §1.6`) moves to a **separate plan** (`2026-05-1X-phase-1-week-3-ui-scaffold.md`) — too much surface area for one plan. After this plan: a CLI executor exists; UI doesn't.

**Pre-flight:** Phase 1 Foundation (PR #1) and Week 2 Controller Core (PR #2) **must both be merged** before starting this plan. The executor process depends on Week 2's `/api/v1/executors/*` and `/api/v1/subtasks/*` endpoints.

**Out-of-scope (deferred):**
- Real HuggingFace Hub API for file resolution → Week 4 plan
- Streaming SHA256 computed during download (vs after) → Week 4 plan
- Real S3 multipart upload → Week 4 plan
- mTLS executor authentication → Phase 2 plan
- `executor_epoch` fence-token logic on rejoin → Phase 2 plan
- Multi-source / hf-mirror / ModelScope failover → v2.1+
- Executor heartbeat-loss reclaim by controller → Phase 2 plan
- Vue 3 UI → separate Week 3 UI plan
- WebSocket `/ws/tasks/{id}/progress` → Week 3 UI plan (UI is the primary consumer)

---

## File Structure

After this plan:

```
modelpull/
├── pyproject.toml                                  # +tenacity dep, +[project.scripts] dlw-executor
├── src/dlw/executor/
│   ├── __init__.py
│   ├── config.py                                   # ExecutorSettings (DLW_EXECUTOR_*)
│   ├── client.py                                   # ControllerClient — HTTP wrapper
│   ├── downloader.py                               # MockDownloader — generates random file + sha256
│   ├── runner.py                                   # ExecutorRunner — main loop
│   └── cli.py                                      # `dlw-executor` entry point
├── docker-compose.dev.yml                          # +executor service alongside postgres
├── README.md                                       # +Week 3 demo: 3-process docker compose
└── tests/
    ├── executor/
    │   ├── __init__.py
    │   ├── test_config.py
    │   ├── test_client.py                          # httpx MockTransport
    │   ├── test_downloader.py
    │   ├── test_runner.py                          # asyncio.create_task + cancel
    │   └── test_cli.py                             # subprocess test
    └── e2e/
        └── test_executor_e2e.py                    # spin up real controller + executor → task succeeds
```

**Why this structure:** `dlw.executor` is a sibling subpackage to `dlw.api`/`dlw.services` — same pyproject, same venv, but the runtime entry points are different (controller is `uvicorn dlw.main:app`; executor is `dlw-executor` CLI). Tests mirror by file. The `e2e/` test exercises the controller-executor protocol end-to-end without any HTTP mocking.

---

## Plan Revisions Log

This plan was reviewed by 2 specialized agents on 2026-05-09 after the first draft. 9 fixes applied (W3-A through W3-I) before subagent execution.

| Tag | Severity | Issue | Fix applied |
|-----|----------|-------|-------------|
| W3-A | CRITICAL | Task 4 shutdown cancels mid-flight `_execute_subtask` → subtask stuck `assigned` forever (no failure report sent) | Removed `t.cancel()`; loops exit cleanly via `is_set()` guard after current iteration |
| W3-B | CRITICAL | Task 5 `except KeyboardInterrupt` inside `_async_main` unreachable on Windows (asyncio.run catches first) | Moved exception handler to `main()` outside asyncio.run |
| W3-C | CRITICAL | `MockDownloader.download` is `async def` but synchronously writes — blocks event loop; 1GB test file freezes tests | Wrapped blocking I/O in `asyncio.to_thread` |
| W3-D | CRITICAL | Task 6 Step 2.5 (small file size monkeypatch) was conditional ("if E2E times out"); 1GB mock will always block | Made mandatory + moved before Step 2 + spelled out exact function signature |
| W3-E | important | Task 6 `_SharedTransport` accesses httpx private `_transport` attribute | Replaced with direct shared `ASGITransport(app=app)` instance |
| W3-F | important | Task 4 test `DownloadResult.file_path` constructed as `str + "/path"`, dataclass typed `Path` | Use `Path(...) / "..."` |
| W3-G | important | Task 7 `Dockerfile.executor` doesn't COPY `alembic.ini`; `alembic upgrade head` would fail with FileNotFoundError | Added `COPY alembic.ini ./` before uv sync |
| W3-H | important | Task 7 executor's `depends_on: service_started` races with controller's migration → 5xx retry exhaustion | Added healthcheck on controller + executor uses `service_healthy` |
| W3-I | important | Task 7 controller `command` uses `&&` without `set -e` → alembic failure still starts uvicorn on broken schema | Added `set -e` to shell command |

Bonus refactor: simplified Task 2 `_post` to drop dead `_is_transient` use inside `_do()` (status code check is clearer).

---

## Pre-flight checks

- [ ] **PR #1 (Phase 1 Foundation) merged to main**
- [ ] **PR #2 (Week 2 Controller Core) merged to main**
- [ ] **Local PG running** (`pg_isready -h localhost -p 5433`)
- [ ] **`dlw` database has 9 tables + Week 2 indexes** (`alembic upgrade head` is no-op)
- [ ] **All 51 tests pass** (`uv run pytest`)
- [ ] **Branch created**: `git checkout -b feat/phase-1-week-3-executor` (off main, fresh)

---

## Task 1: Executor config (TDD)

**Files:**
- Create: `src/dlw/executor/__init__.py` (empty)
- Create: `src/dlw/executor/config.py`
- Create: `tests/executor/__init__.py` (empty)
- Create: `tests/executor/test_config.py`

- [ ] **Step 1: Write failing test `tests/executor/test_config.py`**

```python
"""Tests for ExecutorSettings."""
from __future__ import annotations

import pytest

from dlw.executor.config import ExecutorSettings


@pytest.mark.slow
def test_defaults_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DLW_EXECUTOR_ID", "host-test-w1")
    monkeypatch.setenv("DLW_EXECUTOR_BEARER_TOKEN", "secret")
    s = ExecutorSettings()
    assert s.id == "host-test-w1"
    assert s.bearer_token == "secret"
    assert s.controller_url == "http://localhost:8000"  # default
    assert s.heartbeat_interval_seconds == 10
    assert s.poll_interval_seconds == 2
    assert s.download_dir == "./downloads"


@pytest.mark.slow
def test_required_fields_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DLW_EXECUTOR_ID", raising=False)
    monkeypatch.delenv("DLW_EXECUTOR_BEARER_TOKEN", raising=False)
    with pytest.raises(Exception):  # pydantic ValidationError
        ExecutorSettings()


@pytest.mark.slow
def test_host_id_defaults_to_id_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """If host_id not set, derive from id by stripping -worker-N suffix."""
    monkeypatch.setenv("DLW_EXECUTOR_ID", "host-12.local-worker-3")
    monkeypatch.setenv("DLW_EXECUTOR_BEARER_TOKEN", "x")
    monkeypatch.delenv("DLW_EXECUTOR_HOST_ID", raising=False)
    s = ExecutorSettings()
    assert s.host_id == "host-12.local"
```

- [ ] **Step 2: Verify red**

```bash
uv run pytest tests/executor/test_config.py -v
```

Expected: ImportError on `dlw.executor.config`.

- [ ] **Step 3: Implement `src/dlw/executor/config.py`**

```python
"""Executor configuration via pydantic-settings — env_prefix DLW_EXECUTOR_."""
from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ExecutorSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DLW_EXECUTOR_",
        env_file=".env.executor",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Identity
    id: str = Field(min_length=1, max_length=64)
    host_id: str = Field(default="", max_length=64)

    # Connection to controller
    controller_url: str = Field(default="http://localhost:8000")
    bearer_token: str = Field(min_length=1)

    # Loop pacing
    heartbeat_interval_seconds: int = Field(default=10, ge=1, le=300)
    poll_interval_seconds: int = Field(default=2, ge=1, le=60)

    # Mock downloader
    download_dir: str = Field(default="./downloads")

    # Capabilities advertised at /join
    nic_speed_gbps: int = Field(default=1, ge=1, le=400)
    region: str = Field(default="local")

    @model_validator(mode="after")
    def _derive_host_id(self) -> "ExecutorSettings":
        """If host_id not set, derive from id by stripping any -worker-N suffix.

        Reflects invariant 9 convention: id = `host-X-worker-N`, host_id = `host-X`.
        """
        if not self.host_id:
            parts = self.id.rsplit("-worker-", 1)
            self.host_id = parts[0] if len(parts) == 2 else self.id
        return self
```

- [ ] **Step 4: Verify green**

```bash
uv run pytest tests/executor/test_config.py -v
```

Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dlw/executor/__init__.py src/dlw/executor/config.py tests/executor/
git commit -m "$(cat <<'EOF'
feat(executor): ExecutorSettings — env-driven config

DLW_EXECUTOR_ID + DLW_EXECUTOR_BEARER_TOKEN required; everything else has
sensible defaults. host_id auto-derived from id when not set (invariant 9
convention: host-X-worker-N → host-X).

Plan: docs/superpowers/plans/2026-05-09-phase-1-week-3-executor-process.md Task 1
EOF
)"
```

---

## Task 2: ControllerClient HTTP wrapper (TDD)

**Files:**
- Create: `src/dlw/executor/client.py`
- Create: `tests/executor/test_client.py`

Uses `httpx.AsyncClient` + `tenacity` retry. Wraps Week 2 endpoints.

- [ ] **Step 1: Add `tenacity` to pyproject deps**

In `pyproject.toml` under `[project] dependencies`:

```toml
    "tenacity>=9.0,<10.0",
```

Run `uv sync` to install.

- [ ] **Step 2: Write failing test `tests/executor/test_client.py`**

```python
"""Tests for ControllerClient using httpx MockTransport — no real network."""
from __future__ import annotations

import json
import uuid

import httpx
import pytest

from dlw.executor.client import ControllerClient


def _mock_handler(request: httpx.Request) -> httpx.Response:
    """Routes requests to canned responses based on URL path."""
    path = request.url.path
    body = json.loads(request.content) if request.content else {}

    if path == "/api/v1/executors/join" and request.method == "POST":
        return httpx.Response(201, json={
            "id": body["id"], "status": "joining", "health_score": 100,
        })
    if path.endswith("/heartbeat") and request.method == "POST":
        return httpx.Response(200, json={
            "id": "x", "status": "healthy", "health_score": body.get("health_score", 100),
        })
    if path.endswith("/poll") and request.method == "POST":
        return httpx.Response(200, json={
            "assigned": True,
            "subtask": {
                "id": str(uuid.uuid4()),
                "task_id": str(uuid.uuid4()),
                "filename": "model.safetensors",
                "file_size": 1024,
                "expected_sha256": None,
                "status": "assigned",
            },
            "assignment_token": str(uuid.uuid4()),
        })
    if "/subtasks/" in path and path.endswith("/report"):
        return httpx.Response(200, json={
            "subtask_status": "succeeded", "task_status": "pending",
        })
    return httpx.Response(404)


@pytest.fixture
def transport() -> httpx.MockTransport:
    return httpx.MockTransport(_mock_handler)


@pytest.mark.slow
async def test_join_sends_correct_body(transport) -> None:
    async with ControllerClient(
        base_url="http://test", bearer_token="t", _transport=transport
    ) as c:
        r = await c.join(executor_id="ex-1", host_id="h", capabilities={"nic_speed_gbps": 10})
    assert r["status"] == "joining"


@pytest.mark.slow
async def test_heartbeat_returns_state(transport) -> None:
    async with ControllerClient(
        base_url="http://test", bearer_token="t", _transport=transport
    ) as c:
        r = await c.heartbeat(executor_id="ex-1", health_score=88, parts_dir_bytes=0)
    assert r["status"] == "healthy"
    assert r["health_score"] == 88


@pytest.mark.slow
async def test_poll_returns_assignment(transport) -> None:
    async with ControllerClient(
        base_url="http://test", bearer_token="t", _transport=transport
    ) as c:
        r = await c.poll(executor_id="ex-1")
    assert r["assigned"] is True
    assert "subtask" in r
    assert "assignment_token" in r


@pytest.mark.slow
async def test_report_propagates_token(transport) -> None:
    async with ControllerClient(
        base_url="http://test", bearer_token="t", _transport=transport
    ) as c:
        r = await c.report(
            subtask_id=uuid.uuid4(),
            status="succeeded",
            assignment_token=uuid.uuid4(),
            actual_sha256="a" * 64,
            bytes_downloaded=1024,
        )
    assert r["subtask_status"] == "succeeded"


@pytest.mark.slow
async def test_unauthenticated_returns_401(transport) -> None:
    """ControllerClient should propagate 401 as an exception (caller decides retry)."""
    def unauth(_):
        return httpx.Response(401, json={"detail": "missing bearer token"})
    t = httpx.MockTransport(unauth)
    async with ControllerClient(base_url="http://test", bearer_token="bad", _transport=t) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await c.heartbeat(executor_id="ex-1", health_score=100, parts_dir_bytes=0)
```

- [ ] **Step 3: Verify red**

```bash
uv run pytest tests/executor/test_client.py -v
```

Expected: ImportError on `dlw.executor.client`.

- [ ] **Step 4: Implement `src/dlw/executor/client.py`**

```python
"""HTTP client wrapping the controller's executor + subtask endpoints.

All methods raise httpx.HTTPStatusError on non-2xx — caller decides retry policy.
Includes tenacity retry for transient (5xx, network) errors only.
"""
from __future__ import annotations

import uuid
from typing import Any, Self

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


def _is_transient(exc: BaseException) -> bool:
    """Retry transient HTTP failures: 5xx, timeouts, connection errors.

    Do NOT retry 4xx — those are bugs (auth, malformed body) that won't
    self-heal on retry.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))


_retry = retry(
    retry=retry_if_exception_type(
        (httpx.HTTPStatusError, httpx.TimeoutException, httpx.NetworkError)
    ),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, max=4.0),
    reraise=True,
)


class ControllerClient:
    """Async HTTP client for controller endpoints (executor side)."""

    def __init__(
        self,
        base_url: str,
        bearer_token: str,
        timeout_seconds: float = 30.0,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {bearer_token}"}
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=timeout_seconds,
            transport=_transport,  # for tests
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._client.aclose()

    async def _post(self, path: str, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        @_retry
        async def _do() -> httpx.Response:
            r = await self._client.post(path, json=json_body)
            if 500 <= r.status_code < 600:
                r.raise_for_status()  # 5xx is transient — tenacity will retry
            return r
        r = await _do()
        r.raise_for_status()  # 4xx falls through here, raised once (no retry)
        return r.json()

    async def join(
        self, *, executor_id: str, host_id: str, capabilities: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._post("/api/v1/executors/join", {
            "id": executor_id, "host_id": host_id, "capabilities": capabilities,
        })

    async def heartbeat(
        self, *, executor_id: str, health_score: int, parts_dir_bytes: int
    ) -> dict[str, Any]:
        return await self._post(
            f"/api/v1/executors/{executor_id}/heartbeat",
            {"health_score": health_score, "parts_dir_bytes": parts_dir_bytes},
        )

    async def poll(self, *, executor_id: str) -> dict[str, Any]:
        return await self._post(f"/api/v1/executors/{executor_id}/poll")

    async def report(
        self,
        *,
        subtask_id: uuid.UUID,
        status: str,
        assignment_token: uuid.UUID | None,
        actual_sha256: str | None,
        bytes_downloaded: int,
        error: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "status": status,
            "bytes_downloaded": bytes_downloaded,
        }
        if assignment_token is not None:
            body["assignment_token"] = str(assignment_token)
        if actual_sha256 is not None:
            body["actual_sha256"] = actual_sha256
        if error is not None:
            body["error"] = error
        return await self._post(f"/api/v1/subtasks/{subtask_id}/report", body)
```

- [ ] **Step 5: Verify green**

```bash
uv run pytest tests/executor/test_client.py -v
```

Expected: 5 PASS.

- [ ] **Step 6: Commit**

```bash
git add src/dlw/executor/client.py tests/executor/test_client.py pyproject.toml uv.lock
git commit -m "$(cat <<'EOF'
feat(executor): ControllerClient — HTTP wrapper with tenacity retry

Wraps Week 2 endpoints: join / heartbeat / poll / report.
Retries 5xx + network errors (3 attempts, exp backoff). Does NOT retry 4xx.
Bearer token in default headers. 5 unit tests via httpx.MockTransport.

Plan: docs/superpowers/plans/2026-05-09-phase-1-week-3-executor-process.md Task 2
EOF
)"
```

---

## Task 3: MockDownloader (TDD)

**Files:**
- Create: `src/dlw/executor/downloader.py`
- Create: `tests/executor/test_downloader.py`

In Week 3, the "downloader" generates random bytes of `file_size` and writes them to the configured `download_dir`. Computes sha256 along the way. Real HF Hub fetch is in Week 4 plan.

- [ ] **Step 1: Write failing test `tests/executor/test_downloader.py`**

```python
"""Tests for MockDownloader."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from dlw.executor.downloader import MockDownloader, DownloadResult


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.mark.slow
async def test_download_writes_file_of_correct_size(tmp_dir: Path) -> None:
    d = MockDownloader(download_dir=tmp_dir)
    result = await d.download(
        task_id="task-1", filename="model.safetensors", file_size=8192,
    )
    assert isinstance(result, DownloadResult)
    assert result.bytes_written == 8192
    file_path = tmp_dir / "task-1" / "model.safetensors"
    assert file_path.exists()
    assert file_path.stat().st_size == 8192


@pytest.mark.slow
async def test_download_returns_correct_sha256(tmp_dir: Path) -> None:
    d = MockDownloader(download_dir=tmp_dir, seed=42)  # reproducible
    result = await d.download(
        task_id="task-2", filename="config.json", file_size=4096,
    )
    file_path = tmp_dir / "task-2" / "config.json"
    expected = hashlib.sha256(file_path.read_bytes()).hexdigest()
    assert result.actual_sha256 == expected
    assert len(result.actual_sha256) == 64


@pytest.mark.slow
async def test_download_zero_bytes_succeeds(tmp_dir: Path) -> None:
    """file_size=0 (e.g., empty config) shouldn't crash."""
    d = MockDownloader(download_dir=tmp_dir)
    result = await d.download(
        task_id="task-3", filename="empty.json", file_size=0,
    )
    assert result.bytes_written == 0
    assert result.actual_sha256 == hashlib.sha256(b"").hexdigest()


@pytest.mark.slow
async def test_download_creates_subdirs(tmp_dir: Path) -> None:
    """Filenames with subpaths (e.g., 'subdir/model.bin') should auto-mkdir."""
    d = MockDownloader(download_dir=tmp_dir)
    await d.download(
        task_id="task-4", filename="weights/layer1.bin", file_size=128,
    )
    assert (tmp_dir / "task-4" / "weights" / "layer1.bin").exists()
```

- [ ] **Step 2: Verify red**

```bash
uv run pytest tests/executor/test_downloader.py -v
```

Expected: ImportError on `dlw.executor.downloader`.

- [ ] **Step 3: Implement `src/dlw/executor/downloader.py`**

```python
"""Mock downloader: write `file_size` random bytes + compute sha256.

Real HuggingFace Hub fetch comes in Week 4 plan. The interface is designed
so the only swap needed in Week 4 is replacing the random-bytes generator
with the actual HF download stream.

W3-C: blocking I/O (file write + randbytes) is wrapped in asyncio.to_thread
so the executor's event loop stays responsive (heartbeat / poll / shutdown
keep working during a multi-second download).
"""
from __future__ import annotations

import asyncio
import hashlib
import random
from dataclasses import dataclass
from pathlib import Path

# Default chunk size for streaming write — keep small enough to test memory bounds
_CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True)
class DownloadResult:
    bytes_written: int
    actual_sha256: str
    file_path: Path


class MockDownloader:
    """Generates a file of `file_size` random bytes; computes sha256 on the fly."""

    def __init__(self, download_dir: Path, seed: int | None = None) -> None:
        self._download_dir = Path(download_dir)
        self._rng = random.Random(seed) if seed is not None else random.Random()

    async def download(
        self, *, task_id: str, filename: str, file_size: int
    ) -> DownloadResult:
        """Write `file_size` random bytes; computes sha256 on the fly.

        File I/O + RNG run inside asyncio.to_thread so event loop is not
        blocked during long downloads (W3-C). Memory is O(1) — chunked write.
        """
        return await asyncio.to_thread(
            self._download_sync, task_id=task_id, filename=filename, file_size=file_size
        )

    def _download_sync(
        self, *, task_id: str, filename: str, file_size: int
    ) -> DownloadResult:
        target = self._download_dir / task_id / filename
        target.parent.mkdir(parents=True, exist_ok=True)

        sha = hashlib.sha256()
        bytes_remaining = file_size
        bytes_written = 0
        with target.open("wb") as f:
            while bytes_remaining > 0:
                chunk_size = min(_CHUNK_SIZE, bytes_remaining)
                chunk = self._rng.randbytes(chunk_size)
                sha.update(chunk)
                f.write(chunk)
                bytes_written += chunk_size
                bytes_remaining -= chunk_size

        return DownloadResult(
            bytes_written=bytes_written,
            actual_sha256=sha.hexdigest(),
            file_path=target,
        )
```

- [ ] **Step 4: Verify green**

```bash
uv run pytest tests/executor/test_downloader.py -v
```

Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dlw/executor/downloader.py tests/executor/test_downloader.py
git commit -m "$(cat <<'EOF'
feat(executor): MockDownloader — generates random file + streams sha256

Writes file_size random bytes to download_dir/<task_id>/<filename> in 64KB
chunks; sha256 computed on the fly so memory is O(1).

Real HuggingFace Hub fetch deferred to Week 4 plan; interface designed for
drop-in replacement.

Plan: docs/superpowers/plans/2026-05-09-phase-1-week-3-executor-process.md Task 3
EOF
)"
```

---

## Task 4: ExecutorRunner main loop (TDD)

**Files:**
- Create: `src/dlw/executor/runner.py`
- Create: `tests/executor/test_runner.py`

The runner is the brain. One async loop with three concurrent coroutines:
1. Heartbeat loop (every `heartbeat_interval_seconds`)
2. Poll-and-execute loop (every `poll_interval_seconds`; if assigned, download + report)
3. Shutdown signal handler (SIGTERM/SIGINT cancels the other two)

- [ ] **Step 1: Write failing test `tests/executor/test_runner.py`**

```python
"""Tests for ExecutorRunner main loop."""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from dlw.executor.client import ControllerClient
from dlw.executor.config import ExecutorSettings
from dlw.executor.downloader import DownloadResult, MockDownloader
from dlw.executor.runner import ExecutorRunner


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ExecutorSettings:
    monkeypatch.setenv("DLW_EXECUTOR_ID", "host-test-w1")
    monkeypatch.setenv("DLW_EXECUTOR_BEARER_TOKEN", "secret")
    monkeypatch.setenv("DLW_EXECUTOR_DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setenv("DLW_EXECUTOR_HEARTBEAT_INTERVAL_SECONDS", "1")
    monkeypatch.setenv("DLW_EXECUTOR_POLL_INTERVAL_SECONDS", "1")
    return ExecutorSettings()


@pytest.mark.slow
async def test_runner_join_then_heartbeat_in_idle(settings) -> None:
    """When poll always returns assigned=False, runner heartbeats but does not download."""
    client = MagicMock(spec=ControllerClient)
    client.join = AsyncMock(return_value={"id": "host-test-w1", "status": "joining", "health_score": 100})
    client.heartbeat = AsyncMock(return_value={"id": "x", "status": "healthy", "health_score": 100})
    client.poll = AsyncMock(return_value={"assigned": False, "subtask": None, "assignment_token": None})
    downloader = MagicMock(spec=MockDownloader)

    runner = ExecutorRunner(settings=settings, client=client, downloader=downloader)
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(2.5)  # let it run a few cycles
    runner.request_shutdown()
    await asyncio.wait_for(task, timeout=5)

    client.join.assert_awaited_once()
    assert client.heartbeat.await_count >= 1
    assert client.poll.await_count >= 1
    downloader.download.assert_not_called()


@pytest.mark.slow
async def test_runner_executes_assigned_subtask(settings) -> None:
    """When poll returns an assignment, runner downloads + reports."""
    sub_id = uuid.uuid4()
    token = uuid.uuid4()

    client = MagicMock(spec=ControllerClient)
    client.join = AsyncMock(return_value={"id": "host-test-w1", "status": "joining", "health_score": 100})
    client.heartbeat = AsyncMock(return_value={"id": "x", "status": "healthy", "health_score": 100})

    poll_results = [
        {
            "assigned": True,
            "subtask": {
                "id": str(sub_id),
                "task_id": str(uuid.uuid4()),
                "filename": "config.json",
                "file_size": 1024,
                "expected_sha256": None,
                "status": "assigned",
            },
            "assignment_token": str(token),
        },
        {"assigned": False, "subtask": None, "assignment_token": None},
    ]
    client.poll = AsyncMock(side_effect=lambda **kw: poll_results.pop(0) if poll_results else {"assigned": False, "subtask": None, "assignment_token": None})

    download_result = DownloadResult(
        bytes_written=1024, actual_sha256="a" * 64,
        file_path=Path(settings.download_dir) / "task-1" / "config.json",
    )
    downloader = MagicMock(spec=MockDownloader)
    downloader.download = AsyncMock(return_value=download_result)

    client.report = AsyncMock(return_value={"subtask_status": "succeeded", "task_status": "pending"})

    runner = ExecutorRunner(settings=settings, client=client, downloader=downloader)
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(2.5)
    runner.request_shutdown()
    await asyncio.wait_for(task, timeout=5)

    downloader.download.assert_awaited_once()
    client.report.assert_awaited_once()
    call = client.report.await_args
    assert call.kwargs["status"] == "succeeded"
    assert call.kwargs["assignment_token"] == token
    assert call.kwargs["bytes_downloaded"] == 1024


@pytest.mark.slow
async def test_runner_reports_failure_on_download_error(settings) -> None:
    """If downloader raises, runner reports status=failed with the error message."""
    sub_id = uuid.uuid4()
    token = uuid.uuid4()

    client = MagicMock(spec=ControllerClient)
    client.join = AsyncMock(return_value={"id": "host-test-w1", "status": "joining", "health_score": 100})
    client.heartbeat = AsyncMock(return_value={"id": "x", "status": "healthy", "health_score": 100})
    client.poll = AsyncMock(side_effect=[
        {
            "assigned": True,
            "subtask": {"id": str(sub_id), "task_id": "x", "filename": "f", "file_size": 100, "expected_sha256": None, "status": "assigned"},
            "assignment_token": str(token),
        },
        {"assigned": False, "subtask": None, "assignment_token": None},
    ])
    downloader = MagicMock(spec=MockDownloader)
    downloader.download = AsyncMock(side_effect=OSError("disk full"))
    client.report = AsyncMock(return_value={"subtask_status": "failed", "task_status": "failed"})

    runner = ExecutorRunner(settings=settings, client=client, downloader=downloader)
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(2.5)
    runner.request_shutdown()
    await asyncio.wait_for(task, timeout=5)

    client.report.assert_awaited_once()
    call = client.report.await_args
    assert call.kwargs["status"] == "failed"
    assert "disk full" in call.kwargs["error"]


@pytest.mark.slow
async def test_runner_graceful_shutdown(settings) -> None:
    """request_shutdown() during execution should cleanly cancel the loops."""
    client = MagicMock(spec=ControllerClient)
    client.join = AsyncMock(return_value={"id": "x", "status": "joining", "health_score": 100})
    client.heartbeat = AsyncMock(return_value={"id": "x", "status": "healthy", "health_score": 100})
    client.poll = AsyncMock(return_value={"assigned": False, "subtask": None, "assignment_token": None})
    downloader = MagicMock(spec=MockDownloader)

    runner = ExecutorRunner(settings=settings, client=client, downloader=downloader)
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.3)
    runner.request_shutdown()
    # If shutdown works, task completes within 1-2s
    await asyncio.wait_for(task, timeout=3)
```

- [ ] **Step 2: Verify red**

```bash
uv run pytest tests/executor/test_runner.py -v
```

Expected: ImportError on `dlw.executor.runner`.

- [ ] **Step 3: Implement `src/dlw/executor/runner.py`**

```python
"""ExecutorRunner — async main loop joining heartbeat + poll-and-execute.

On startup: register via /join. Then runs three concurrent loops:
  - Heartbeat every settings.heartbeat_interval_seconds
  - Poll every settings.poll_interval_seconds; if assigned, download + report
  - Shutdown signal listener (SIGTERM/SIGINT) → cancel both loops, send /heartbeat
    one last time with health_score=0 (best-effort)

Exits cleanly when request_shutdown() is called.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from dlw.executor.client import ControllerClient
from dlw.executor.config import ExecutorSettings
from dlw.executor.downloader import MockDownloader

logger = logging.getLogger(__name__)


class ExecutorRunner:
    def __init__(
        self,
        *,
        settings: ExecutorSettings,
        client: ControllerClient,
        downloader: MockDownloader,
    ) -> None:
        self._s = settings
        self._client = client
        self._downloader = downloader
        self._shutdown = asyncio.Event()

    def request_shutdown(self) -> None:
        self._shutdown.set()

    async def run(self) -> None:
        # 1. Join (one-shot)
        await self._client.join(
            executor_id=self._s.id,
            host_id=self._s.host_id,
            capabilities={
                "nic_speed_gbps": self._s.nic_speed_gbps,
                "region": self._s.region,
            },
        )

        # 2. Concurrent loops — both check self._shutdown.is_set() each iteration
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        poll_task = asyncio.create_task(self._poll_and_execute_loop())

        # 3. Wait for shutdown signal then let loops exit naturally
        # (W3-A fix): do NOT t.cancel() — that would interrupt _execute_subtask
        # mid-download, leaving the subtask stuck in 'assigned' status with no
        # failure report. The pacing waits inside each loop already react to
        # _shutdown.set() instantly via asyncio.wait_for(... timeout=...).
        await self._shutdown.wait()
        await asyncio.gather(heartbeat_task, poll_task, return_exceptions=True)

    async def _heartbeat_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                await self._client.heartbeat(
                    executor_id=self._s.id, health_score=100, parts_dir_bytes=0
                )
            except Exception as e:
                logger.warning("heartbeat failed: %s", e)
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(),
                    timeout=self._s.heartbeat_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass  # normal — interval elapsed without shutdown

    async def _poll_and_execute_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                resp = await self._client.poll(executor_id=self._s.id)
                if resp.get("assigned"):
                    await self._execute_subtask(
                        resp["subtask"],
                        uuid.UUID(resp["assignment_token"]),
                    )
                    continue  # immediately poll again — there may be more work
            except Exception as e:
                logger.warning("poll failed: %s", e)
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(),
                    timeout=self._s.poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass

    async def _execute_subtask(
        self, subtask: dict, assignment_token: uuid.UUID
    ) -> None:
        sub_id = uuid.UUID(subtask["id"])
        try:
            result = await self._downloader.download(
                task_id=str(subtask["task_id"]),
                filename=subtask["filename"],
                file_size=subtask.get("file_size") or 0,
            )
            await self._client.report(
                subtask_id=sub_id,
                status="succeeded",
                assignment_token=assignment_token,
                actual_sha256=result.actual_sha256,
                bytes_downloaded=result.bytes_written,
            )
        except Exception as e:
            logger.exception("subtask %s failed", sub_id)
            try:
                await self._client.report(
                    subtask_id=sub_id,
                    status="failed",
                    assignment_token=assignment_token,
                    actual_sha256=None,
                    bytes_downloaded=0,
                    error=str(e),
                )
            except Exception:
                logger.exception("report failure also failed for %s", sub_id)
```

- [ ] **Step 4: Verify green**

```bash
uv run pytest tests/executor/test_runner.py -v
```

Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dlw/executor/runner.py tests/executor/test_runner.py
git commit -m "$(cat <<'EOF'
feat(executor): ExecutorRunner — heartbeat + poll-execute concurrent loops

One asyncio.Event drives shutdown; both loops await it as their pacing wait
(so shutdown is instant, not delayed by the interval). Reports failures with
exception text. 4 unit tests with httpx + AsyncMock.

Plan: docs/superpowers/plans/2026-05-09-phase-1-week-3-executor-process.md Task 4
EOF
)"
```

---

## Task 5: CLI entry point + signal handling (TDD)

**Files:**
- Create: `src/dlw/executor/cli.py`
- Create: `tests/executor/test_cli.py`
- Modify: `pyproject.toml` (add `[project.scripts] dlw-executor`)

- [ ] **Step 1: Add CLI entry point to pyproject.toml**

Find the `[project]` section. After `dependencies = [...]`, add:

```toml
[project.scripts]
dlw-executor = "dlw.executor.cli:main"
```

Run `uv sync` to register.

- [ ] **Step 2: Write failing test `tests/executor/test_cli.py`**

```python
"""Tests for dlw-executor CLI."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.slow
def test_cli_help_exits_0() -> None:
    """`dlw-executor --help` should print usage and exit cleanly."""
    r = subprocess.run(
        [sys.executable, "-m", "dlw.executor.cli", "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 0
    assert "executor" in r.stdout.lower()


@pytest.mark.slow
def test_cli_missing_required_env_exits_nonzero() -> None:
    """Without DLW_EXECUTOR_ID/BEARER_TOKEN, CLI should fail at config validation."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("DLW_EXECUTOR_")}
    r = subprocess.run(
        [sys.executable, "-m", "dlw.executor.cli"],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert r.returncode != 0
    # pydantic ValidationError should mention the missing fields
    combined = r.stdout + r.stderr
    assert "id" in combined.lower() or "bearer_token" in combined.lower()
```

- [ ] **Step 3: Verify red**

```bash
uv run pytest tests/executor/test_cli.py -v
```

Expected: ModuleNotFoundError on `dlw.executor.cli`.

- [ ] **Step 4: Implement `src/dlw/executor/cli.py`**

```python
"""CLI entry point for dlw-executor.

Wires up SIGTERM/SIGINT handlers and runs ExecutorRunner.run() until shutdown.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from dlw.executor.client import ControllerClient
from dlw.executor.config import ExecutorSettings
from dlw.executor.downloader import MockDownloader
from dlw.executor.runner import ExecutorRunner


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="dlw-executor",
        description="modelpull executor — polls controller for download work",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p.parse_args(argv)


async def _async_main(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    settings = ExecutorSettings()
    client = ControllerClient(
        base_url=settings.controller_url, bearer_token=settings.bearer_token,
    )
    downloader = MockDownloader(download_dir=Path(settings.download_dir))
    runner = ExecutorRunner(settings=settings, client=client, downloader=downloader)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, runner.request_shutdown)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler — fall back to default
            # KeyboardInterrupt behavior; Ctrl-C still works in dev.
            pass

    async with client:
        await runner.run()
    return 0


def main() -> int:
    """Entry point. Catches KeyboardInterrupt OUTSIDE asyncio.run because on
    Windows asyncio.run catches Ctrl-C internally and re-raises after task
    cancellation — the inner except KeyboardInterrupt is unreachable (W3-B).
    """
    args = _parse_args()
    try:
        return asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        return 0  # graceful exit; asyncio.run already cancelled child tasks


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Verify green**

```bash
uv run pytest tests/executor/test_cli.py -v
```

Expected: 2 PASS.

Then verify CLI is on PATH:
```bash
uv run dlw-executor --help
```

Expected: prints usage; exit 0.

- [ ] **Step 6: Commit**

```bash
git add src/dlw/executor/cli.py tests/executor/test_cli.py pyproject.toml uv.lock
git commit -m "$(cat <<'EOF'
feat(executor): dlw-executor CLI entry point + signal handling

argparse for --log-level. SIGTERM/SIGINT handlers (POSIX) wire up to
runner.request_shutdown(). Windows falls back to KeyboardInterrupt.

Plan: docs/superpowers/plans/2026-05-09-phase-1-week-3-executor-process.md Task 5
EOF
)"
```

---

## Task 6: E2E integration test (real controller + executor)

**Files:**
- Create: `tests/e2e/test_executor_e2e.py`

This test spins up the real FastAPI controller in-process via httpx ASGITransport, pipes it into ControllerClient, and runs ExecutorRunner against it. No mocks, no subprocess, no network — but the entire protocol is exercised.

**Subagent context note**: `tests/conftest.py` (Week 1) provides session-scoped fixtures `engine` (AsyncEngine pointing at the per-session test DB) and `test_db_name`, plus an autouse `_point_app_at_test_db` that points the FastAPI app's lru_cached `get_engine()` at the same test DB. The module-scoped `_bootstrap` below depends on `engine` — that scope combination works because session > module > function.

- [ ] **Step 1: Write `tests/e2e/test_executor_e2e.py`**

```python
"""E2E: real controller in-process + real ExecutorRunner — full happy path.

W3-C/D: monkeypatches _MOCK_FILES to small sizes so the test actually finishes
in seconds. A 1GB random-bytes generation would block the event loop for many
seconds even with asyncio.to_thread (the thread is just doing CPU work).

W3-E: shares one httpx.ASGITransport(app=app) instance between the controller's
test AsyncClient and the executor's ControllerClient — no private attribute
access needed.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base
from dlw.executor.client import ControllerClient
from dlw.executor.config import ExecutorSettings
from dlw.executor.downloader import MockDownloader
from dlw.executor.runner import ExecutorRunner


_TOKEN = "e2e-executor-token"


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    """Seed default tenant/project/user/storage. engine is session-scoped (conftest)."""
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Tenant(id=1, slug="d", display_name="D"))
        await s.flush()
        s.add(Project(id=1, tenant_id=1, name="d"))
        s.add(User(id=1, tenant_id=1, oidc_subject="d",
                   email="d@l", role="tenant_admin"))
        s.add(StorageBackend(id=1, tenant_id=1, name="d",
                              backend_type="s3", config_encrypted=b""))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _set_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DLW_BEARER_TOKEN", _TOKEN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.slow
async def test_executor_completes_real_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end with real controller + real executor (no mocks).

    1. Reduce mock file sizes so test finishes in seconds (W3-D mandatory)
    2. POST a task to the controller (creates 2 mock subtasks)
    3. Run an ExecutorRunner targeting the controller via shared ASGI transport
    4. Assert task transitions to 'succeeded' within 5 seconds
    """
    # W3-D: shrink mock files so 1GB safetensors doesn't burn 30+ seconds of CPU
    import dlw.services.task_service as ts
    monkeypatch.setattr(ts, "_MOCK_FILES", [
        ("config.json", 4096, None),
        ("model.safetensors", 64 * 1024, None),  # 64KB instead of 1GB
    ])

    from dlw.main import create_app
    app = create_app()

    # W3-E: single shared ASGITransport, no private attribute access
    asgi_transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=asgi_transport, base_url="http://test"
    ) as ctrl_client:
        auth = {"Authorization": f"Bearer {_TOKEN}"}

        # 2. Create task
        r = await ctrl_client.post("/api/v1/tasks", json={
            "repo_id": "o/e2e",
            "revision": "0" * 40,
            "storage_id": 1,
        }, headers=auth)
        assert r.status_code == 201
        task_id = r.json()["id"]

        # 3. Build ControllerClient sharing the same ASGI transport
        executor_client = ControllerClient(
            base_url="http://test",
            bearer_token=_TOKEN,
            _transport=asgi_transport,
        )

        # 4. Settings + downloader + runner
        settings = ExecutorSettings(
            id="e2e-host-w1",
            host_id="e2e-host",
            controller_url="http://test",
            bearer_token=_TOKEN,
            heartbeat_interval_seconds=1,
            poll_interval_seconds=1,
            download_dir=str(tmp_path),
        )
        downloader = MockDownloader(download_dir=tmp_path)
        runner = ExecutorRunner(
            settings=settings, client=executor_client, downloader=downloader
        )

        async with executor_client:
            run_task = asyncio.create_task(runner.run())
            await asyncio.sleep(4)
            runner.request_shutdown()
            await asyncio.wait_for(run_task, timeout=5)

        # 5. Verify task is succeeded
        r = await ctrl_client.get(f"/api/v1/tasks/{task_id}", headers=auth)
        assert r.json()["status"] == "succeeded", r.json()
        assert r.json()["completed_at"] is not None

        # 6. Verify mock files were written
        files = list(tmp_path.rglob("*"))
        file_names = {p.name for p in files if p.is_file()}
        assert "config.json" in file_names
        assert "model.safetensors" in file_names
```

- [ ] **Step 2: Run**

```bash
uv run pytest tests/e2e/test_executor_e2e.py -v
```

Expected: 1 PASS within ~5 seconds. If it times out, raise the `asyncio.sleep(4)` to `asyncio.sleep(6)` — but 64KB random bytes + 4KB random bytes should complete very fast.

- [ ] **Step 3: Run regression**

```bash
uv run pytest tests/ 2>&1 | tail -3
```

Expected: 51 (Week 2) + ~14 (executor: 3+5+4+2 unit tests) + 1 (e2e) = ~66 PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/test_executor_e2e.py
git commit -m "$(cat <<'EOF'
test(e2e): real controller + real ExecutorRunner end-to-end

No mocks. Spins up FastAPI controller in-process via ASGITransport,
ExecutorRunner connects to it via shared transport, runs a real task
to completion within 5s.

Plan: docs/superpowers/plans/2026-05-09-phase-1-week-3-executor-process.md Task 6
EOF
)"
```

---

## Task 7: Docker compose: add executor service

**Files:**
- Modify: `docker-compose.dev.yml`
- Create: `Dockerfile.executor` (or add a multi-stage to existing one)

Until now `docker-compose.dev.yml` only has PG. Now add an executor that runs `dlw-executor`.

- [ ] **Step 1: Create `Dockerfile.executor`**

```dockerfile
# Multi-stage: build with uv, run with slim
FROM python:3.12-slim AS builder
WORKDIR /app
RUN pip install --no-cache-dir uv==0.11.9
COPY pyproject.toml uv.lock alembic.ini ./
COPY src/ src/
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/alembic.ini ./alembic.ini
# curl for the controller's healthcheck (W3-H); ~1 MB extra
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONPATH=/app/src
ENTRYPOINT ["dlw-executor"]
```

NOTE (W3-G): `alembic.ini` MUST be copied — without it, `alembic upgrade head` in the controller container fails with `FileNotFoundError`. NOTE: `uv==0.11.9` is the correct version (verified locally + in CI; do NOT change to 0.4.x even if some sources claim that's the latest).

- [ ] **Step 2: Update `docker-compose.dev.yml`**

Replace the file entirely:

```yaml
# Dev profile: PG + 1 controller + 1 executor.
# Run via: docker compose -f docker-compose.dev.yml up -d
services:
  postgres:
    image: postgres:16-alpine
    container_name: dlw-postgres-dev
    environment:
      POSTGRES_USER: dlw
      POSTGRES_PASSWORD: dlw_dev_password
      POSTGRES_DB: dlw
    ports:
      - "5432:5432"
    volumes:
      - dlw-pg-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U dlw -d dlw"]
      interval: 5s
      timeout: 5s
      retries: 10

  controller:
    build:
      context: .
      dockerfile: Dockerfile.executor  # same image, override entrypoint
    container_name: dlw-controller-dev
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      DLW_DB_HOST: postgres
      DLW_DB_PORT: "5432"
      DLW_DB_USER: dlw
      DLW_DB_PASSWORD: dlw_dev_password
      DLW_DB_NAME: dlw
      DLW_BEARER_TOKEN: dev-token-change-me
    ports:
      - "8000:8000"
    entrypoint: []
    # W3-I: set -e so alembic failure aborts the container (no broken-schema uvicorn)
    command:
      - sh
      - -c
      - "set -e && alembic upgrade head && uvicorn dlw.main:app --host 0.0.0.0 --port 8000"
    # W3-H: healthcheck on /health/ready so executor's depends_on: service_healthy
    # waits for migration + uvicorn before starting (otherwise executor hammers 5xx)
    healthcheck:
      test: ["CMD-SHELL", "curl -sf http://localhost:8000/health/ready || exit 1"]
      interval: 3s
      timeout: 5s
      retries: 10
      start_period: 30s

  executor:
    build:
      context: .
      dockerfile: Dockerfile.executor
    container_name: dlw-executor-dev
    depends_on:
      controller:
        condition: service_healthy   # W3-H: was service_started
    environment:
      DLW_EXECUTOR_ID: docker-host-worker-1
      DLW_EXECUTOR_HOST_ID: docker-host
      DLW_EXECUTOR_CONTROLLER_URL: http://controller:8000
      DLW_EXECUTOR_BEARER_TOKEN: dev-token-change-me
      DLW_EXECUTOR_DOWNLOAD_DIR: /downloads
    volumes:
      - dlw-downloads:/downloads

volumes:
  dlw-pg-data:
  dlw-downloads:
```

- [ ] **Step 3: Verify compose syntax**

```bash
docker compose -f docker-compose.dev.yml config 2>&1 | tail -5
```

Expected: prints the parsed config. Exit 0.

(If user has no Docker, this step is a manual check via `python -c "import yaml; yaml.safe_load(open('docker-compose.dev.yml'))"`.)

- [ ] **Step 4: Commit**

```bash
git add docker-compose.dev.yml Dockerfile.executor
git commit -m "$(cat <<'EOF'
feat(deploy): docker-compose dev — controller + executor + PG

Three services. Executor depends on controller; controller depends on PG
healthy. Executor uses internal DNS http://controller:8000.

DLW_BEARER_TOKEN=dev-token-change-me hardcoded for dev — change for any
non-local deployment.

Plan: docs/superpowers/plans/2026-05-09-phase-1-week-3-executor-process.md Task 7
EOF
)"
```

---

## Task 8: README — Week 3 demo with docker compose

- [ ] **Step 1: Update README quickstart**

Find the existing Week 2 demo block. Add a new "Week 3 demo: full docker compose" section after it:

```markdown
### Week 3 demo: end-to-end with docker compose

```bash
# 1 command: PG + controller + executor all up
docker compose -f docker-compose.dev.yml up -d --build

# Wait for health
until curl -s http://localhost:8000/health/ready | grep -q ok; do sleep 1; done

# Submit a task
TOKEN_HEADER="Authorization: Bearer dev-token-change-me"
TASK_ID=$(curl -s -X POST http://localhost:8000/api/v1/tasks \
  -H "$TOKEN_HEADER" -H "Content-Type: application/json" \
  -d '{"repo_id":"o/e2e","revision":"0123456789abcdef0123456789abcdef01234567","storage_id":1}' \
  | python -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "Task: $TASK_ID"

# Watch the executor pick it up + complete (within ~10s)
for i in $(seq 1 15); do
  STATUS=$(curl -s "http://localhost:8000/api/v1/tasks/$TASK_ID" -H "$TOKEN_HEADER" \
    | python -c "import sys,json; print(json.load(sys.stdin)['status'])")
  echo "[$i] task status: $STATUS"
  if [ "$STATUS" = "succeeded" ]; then break; fi
  sleep 1
done

# Inspect downloaded mock files in the executor container
docker compose -f docker-compose.dev.yml exec executor ls -la /downloads
```
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
docs(readme): add Week 3 demo — full docker compose end-to-end

Single docker compose up brings up PG + controller + executor.
Submit task, watch executor complete it within seconds.

Plan: docs/superpowers/plans/2026-05-09-phase-1-week-3-executor-process.md Task 8
EOF
)"
```

---

## Task 9: Push branch + open PR + wait for CI

- [ ] **Step 1: Push**

```bash
git push -u origin feat/phase-1-week-3-executor 2>&1 | tail -3
```

- [ ] **Step 2: Open PR (base = main)**

```bash
gh pr create --title "feat(week-3): executor process — autonomous polling + mock downloader" --body "$(cat <<'EOF'
## Summary

Phase 1 Week 3 — executor process. A `dlw-executor` CLI runs in its own process
(no FastAPI), polls the controller, "downloads" mock files, and reports.

End of this PR: `docker compose -f docker-compose.dev.yml up -d --build` brings up
PG + controller + executor. Submit a task → executor picks it up within 1s →
mock files written to volume → task transitions to 'succeeded'.

## Out of scope

- Real HuggingFace Hub API → Week 4
- Vue 3 UI scaffold → separate Week 3 UI plan (paired with this Week 3 in roadmap)
- WebSocket /ws/tasks/{id}/progress → Week 3 UI plan
- mTLS + executor_epoch fence-token → Phase 2

## Test plan

- [x] Unit: 14 tests across config / client / downloader / runner / cli
- [x] E2E: 1 in-process test (real controller + real executor) within 5s
- [ ] CI green

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)" 2>&1 | tail -3
```

- [ ] **Step 3: Wait for CI**

```bash
PR_NUM=$(gh pr list --head feat/phase-1-week-3-executor --json number --jq '.[0].number')
until gh pr checks $PR_NUM --json bucket --jq 'length > 0 and all(.[]; .bucket != "pending")' | grep -q true; do sleep 30; done
gh pr checks $PR_NUM
```

Expected: all 9 (or 10 with aggregate) jobs PASS. If pytest fails on the new executor tests, look at the specific failure (CI uses different PG port + the docker compose isn't exercised in CI — only the pytest tests).

---

## Acceptance criteria — done when ALL hold

- [ ] All Task 1-9 commits exist on branch
- [ ] `uv run pytest tests/` shows ~66 PASS, 0 FAIL
- [ ] `docker compose -f docker-compose.dev.yml up -d --build` succeeds (manual check; not in CI)
- [ ] After compose up + curl POST task, task status transitions to 'succeeded' within 10 seconds
- [ ] PR opened against main + CI green

---

## What's next

- **Week 3 UI scaffold (separate plan)** — Vue 3 + Pinia + Element Plus + login + task list/detail with WebSocket. Paired with this Week 3 per roadmap §1.6 but split into its own plan due to scope.
- **Week 4 (separate plan)** — real HuggingFace Hub fetch + streaming SHA256 + S3 multipart upload.
- **Phase 2 (separate plan)** — mTLS + executor_epoch fence token + automatic crashed-executor reclaim.

---

## Plan self-review

**Spec coverage:**
- ✅ docs/v2.0/01-architecture.md §3 executor state machine — joining → healthy via heartbeat ✓
- ✅ docs/v2.0/02-protocol.md — uses POST /executors/* + /subtasks/{id}/report ✓
- ✅ docs/v2.0/08-mvp-roadmap.md §1.6 Week 3 — executor side covered (UI side deferred to separate plan)
- ✅ Invariant 9 conventions — id format `host-X-worker-N` recommended; host_id auto-derived
- ✅ Invariant 6 (CAS-then-enqueue) — executor relies entirely on controller's atomic claim
- ⚠️ Invariant 7 (fence token) — executor passes `assignment_token` through; controller side already verifies (W2-F)

**Placeholder scan:** No "TODO" / "TBD" / "implement later" without explicit Phase deferral.

**Type consistency:** `subtask_id`, `task_id`, `assignment_token` are all `uuid.UUID`. `executor_id` is str. `file_size`, `bytes_downloaded` are int. `actual_sha256` is `str | None`.

**TDD adherence:** Tasks 1-5 each have failing-test-first cycle. Task 6 is pure E2E (no impl change). Tasks 7-8 are scaffold/docs — no TDD.

**YAGNI:** Mock downloader, no real HF; no UI; no WebSocket; no metrics endpoint; no graceful drain; single executor process.

**Frequent commits:** 9 task commits + 1 plan commit = 10 total.

**Recommended:** Run a 2-agent pre-execution review on this plan (similar to W2-A through W2-J for Week 2) to catch any concurrency / DB / wire-protocol issues before kicking off subagents.
