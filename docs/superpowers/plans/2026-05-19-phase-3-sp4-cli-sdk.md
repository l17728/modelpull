# Phase 3 SP4 — CLI `dlw` + Python SDK Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (the project's validated variant: 2 opus pre-execution reviewers → implementer-only/controller per task → controller full-suite milestone E2E at M3 → opus final whole-impl review at M4 → controller push/PR/CI-wait/squash-merge). Steps use `- [ ]`.

**Goal:** A `dlw.sdk` Python SDK (sync `Client` + async `AsyncClient`) and a `dlw` argparse CLI (`submit/list/show/cancel/delete/watch`) wrapping the already-implemented controller REST API.

**Architecture:** Purely additive — new `src/dlw/sdk/` package + `src/dlw/cli/main.py` + one `[project.scripts]` entry. No controller endpoint/model/schema/migration/lint change. CLI is implemented on top of the SDK. Tests: the **async** `AsyncClient` runs against the real ASGI app via `httpx.ASGITransport` (proven `tests/api/test_tasks.py` pattern); the **sync** `Client` + CLI use `httpx.MockTransport` (httpx 0.27.2 `ASGITransport` is async-only). Both clients take an injectable `transport=` kwarg (test-only).

**Tech Stack:** stdlib `argparse`, `httpx` (sync+async, already a dep), `pyyaml` (already a dep), `pydantic` (already a dep). **No new runtime or dev dependency.** Python 3.12.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/dlw/sdk/__init__.py` | Public exports: `Client`, `AsyncClient`, `DownloadTask`, `errors` |
| `src/dlw/sdk/errors.py` | Error hierarchy + `exit_code_for()` |
| `src/dlw/sdk/_config.py` | server/token/config-file resolution precedence |
| `src/dlw/sdk/_http.py` | `raise_for_status()` — HTTP → typed error mapping |
| `src/dlw/sdk/models.py` | `DownloadTask` wrapper + `TERMINAL` set |
| `src/dlw/sdk/client.py` | sync `Client` + `TasksAPI` + `DownloadTask.wait/refresh` |
| `src/dlw/sdk/aclient.py` | async `AsyncClient` + `AsyncTasksAPI` + `AsyncDownloadTask` |
| `src/dlw/cli/main.py` | argparse parser, subcommand handlers, render, exit codes, `main()->int` |
| `pyproject.toml` | add `dlw = "dlw.cli.main:main"` to `[project.scripts]` |
| `tests/sdk/__init__.py`, `tests/cli/__init__.py` | package markers |
| `tests/sdk/_fixtures.py` | async-e2e fixtures (`__all__`-exported): seeded DB + system-JWT + async `aclient` over `ASGITransport` (mirrors `tests/api/test_tasks.py`) |
| `tests/sdk/_mock.py` | `make_mock_transport()` — sync-compatible `httpx.MockTransport` with an in-memory task store, for the sync `Client` + CLI tests (httpx 0.27.2 `ASGITransport` is async-only) |
| `tests/sdk/test_*.py`, `tests/cli/test_cli.py` | tests |
| `docs/operator/cli-sdk.md` | operator/user guide |

**Contract corrections baked in (the implemented API is authoritative over `docs/v2.0/11-cli-and-sdk-spec.md`):**
- `TaskCreate` requires `storage_id` (int > 0). `submit(storage_id=...)` is a **required keyword arg** (no default).
- `TaskCreate` has **no** `file_filter`/`file_glob`/`download_bytes_limit` — the SDK does not expose them.
- `TaskRead`/`TaskDetail` have **no** `progress` object. `DownloadTask` exposes `id, repo_id, revision, status, priority, created_at, completed_at, error_message, subtasks` (subtasks only on `get`/detail). CLI `watch` derives a files-done count from `subtasks`.
- `POST /api/v1/tasks` calls `list_repo_tree` (HF) inside `create_task` — every submit test MUST monkeypatch `dlw.services.task_service.list_repo_tree` (see Task 4 fixture).

---

# Milestone M1 — SDK core (sync)

### Task 1: Package skeleton + error hierarchy

**Files:**
- Create: `src/dlw/sdk/__init__.py`, `src/dlw/sdk/errors.py`, `tests/sdk/__init__.py`, `tests/cli/__init__.py`
- Test: `tests/sdk/test_errors.py`

- [ ] **Step 1: Write the failing test** — `tests/sdk/test_errors.py`:
```python
"""SDK error hierarchy + exit-code mapping (SP4)."""
from __future__ import annotations

from dlw.sdk import errors as e


def test_hierarchy():
    for c in (e.UsageError, e.NotFound, e.AuthError, e.QuotaExceeded,
              e.Conflict, e.Timeout, e.ApiError):
        assert issubclass(c, e.DlwError)


def test_exit_codes():
    assert e.exit_code_for(e.UsageError("x")) == 2
    assert e.exit_code_for(e.NotFound("x")) == 3
    assert e.exit_code_for(e.AuthError("x")) == 4
    assert e.exit_code_for(e.QuotaExceeded("x")) == 5
    assert e.exit_code_for(e.Conflict("x")) == 6
    assert e.exit_code_for(e.Timeout("x")) == 9
    assert e.exit_code_for(e.ApiError("x")) == 1
    assert e.exit_code_for(e.DlwError("x")) == 1
    assert e.exit_code_for(ValueError("x")) == 1


def test_fields_carried():
    ex = e.Conflict("nope", code="TASK_NOT_TERMINAL", status=409,
                    trace_id="abc", details={"status": "downloading"})
    assert ex.code == "TASK_NOT_TERMINAL"
    assert ex.status == 409
    assert ex.trace_id == "abc"
    assert ex.details == {"status": "downloading"}
    assert str(ex) == "nope"
```

- [ ] **Step 2: Run** `uv run pytest tests/sdk/test_errors.py -v` → FAIL (module missing).

- [ ] **Step 3: Implement**

`tests/sdk/__init__.py`: empty file.
`tests/cli/__init__.py`: empty file.

`src/dlw/sdk/errors.py`:
```python
"""dlw SDK typed errors + POSIX exit-code mapping (SP4; spec §4-§6)."""
from __future__ import annotations


class DlwError(Exception):
    def __init__(self, message: str, *, code: str | None = None,
                 status: int | None = None, trace_id: str | None = None,
                 details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.trace_id = trace_id
        self.details = details or {}


class UsageError(DlwError):
    """Bad CLI args / missing token (pre-flight)."""


class NotFound(DlwError):
    """HTTP 404."""


class AuthError(DlwError):
    """HTTP 401 / 403."""


class QuotaExceeded(DlwError):
    """HTTP 429 or code QUOTA_EXCEEDED."""


class Conflict(DlwError):
    """HTTP 409 (e.g. TASK_NOT_TERMINAL, duplicate)."""


class Timeout(DlwError):
    """wait/watch exceeded the deadline."""


class ApiError(DlwError):
    """Any other non-2xx."""


# Most-specific first; first isinstance match wins.
_ORDER: list[tuple[type, int]] = [
    (UsageError, 2), (NotFound, 3), (AuthError, 4), (QuotaExceeded, 5),
    (Conflict, 6), (Timeout, 9), (ApiError, 1), (DlwError, 1),
]


def exit_code_for(exc: BaseException) -> int:
    for cls, code in _ORDER:
        if isinstance(exc, cls):
            return code
    return 1
```

`src/dlw/sdk/__init__.py`:
```python
"""dlw Python SDK (Phase 3 SP4) — thin client over the controller REST API.

Monorepo note: published vision is `from dlw import Client`; here the
controller owns the `dlw` package, so the SDK lives at `dlw.sdk` to avoid
heavy-importing FastAPI/SQLAlchemy. See the SP4 design doc."""
from __future__ import annotations

from dlw.sdk import errors
from dlw.sdk.aclient import AsyncClient, AsyncDownloadTask
from dlw.sdk.client import Client
from dlw.sdk.models import DownloadTask

__all__ = ["Client", "AsyncClient", "AsyncDownloadTask",
           "DownloadTask", "errors"]
```
(NOTE: `__init__` imports `client`/`aclient`/`models` which are created in Tasks 3-7. Until Task 7, running `import dlw.sdk` fails — that is expected; Tasks 1-2 test their own modules directly (`from dlw.sdk import errors` works because `errors` has no forward deps — but the package `__init__` would still execute the broken imports). To keep Tasks 1-2 green, **write `__init__.py` minimally first** as just the docstring + `from dlw.sdk import errors` + `__all__ = ["errors"]`, and replace it with the full version in Task 7 Step 3.)

Therefore in Task 1 write `src/dlw/sdk/__init__.py` as ONLY:
```python
"""dlw Python SDK (Phase 3 SP4) — see SP4 design doc."""
from __future__ import annotations

from dlw.sdk import errors

__all__ = ["errors"]
```

- [ ] **Step 4: Run** `uv run pytest tests/sdk/test_errors.py -v` → 3 PASS.

- [ ] **Step 5: Commit**
```bash
git add src/dlw/sdk/__init__.py src/dlw/sdk/errors.py tests/sdk/__init__.py tests/cli/__init__.py tests/sdk/test_errors.py
git commit -m "feat(sp4): SDK package skeleton + typed error hierarchy + exit codes"
```

---

### Task 2: Config resolution

**Files:**
- Create: `src/dlw/sdk/_config.py`
- Test: `tests/sdk/test_config.py`

- [ ] **Step 1: Write the failing test** — `tests/sdk/test_config.py`:
```python
"""server/token/config precedence resolution (SP4)."""
from __future__ import annotations

import pytest

from dlw.sdk._config import resolve
from dlw.sdk.errors import UsageError


def test_flag_beats_env(monkeypatch):
    monkeypatch.setenv("DLW_SERVER", "http://env:8000")
    monkeypatch.setenv("DLW_TOKEN", "envtok")
    r = resolve(server="http://flag:9000", token="flagtok", config_path="")
    assert r.server == "http://flag:9000"
    assert r.token == "flagtok"


def test_env_token_fallback_order(monkeypatch):
    monkeypatch.delenv("DLW_TOKEN", raising=False)
    monkeypatch.setenv("DLW_SYSTEM_ADMIN_TOKEN", "systok")
    r = resolve(server=None, token=None, config_path="")
    assert r.token == "systok"
    assert r.server == "http://localhost:8000"


def test_config_file_used(tmp_path, monkeypatch):
    for v in ("DLW_SERVER", "DLW_TOKEN", "DLW_SYSTEM_ADMIN_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "current_context: dev\n"
        "contexts:\n  dev:\n    server: http://cfg:7000\n"
        "auth:\n  dev:\n    access_token: cfgtok\n")
    r = resolve(server=None, token=None, config_path=str(cfg))
    assert r.server == "http://cfg:7000"
    assert r.token == "cfgtok"


def test_missing_token_raises_usage(tmp_path, monkeypatch):
    for v in ("DLW_TOKEN", "DLW_SYSTEM_ADMIN_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(UsageError):
        resolve(server="http://x", token=None, config_path=str(tmp_path / "none.yaml"))


def test_server_trailing_slash_stripped(monkeypatch):
    monkeypatch.setenv("DLW_TOKEN", "t")
    r = resolve(server="http://x:8000/", token=None, config_path="")
    assert r.server == "http://x:8000"
```

- [ ] **Step 2: Run** `uv run pytest tests/sdk/test_config.py -v` → FAIL.

- [ ] **Step 3: Implement** — `src/dlw/sdk/_config.py`:
```python
"""Resolve effective server + token (flag > env > config > default)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from dlw.sdk.errors import UsageError

_DEFAULT_SERVER = "http://localhost:8000"


@dataclass(frozen=True)
class Resolved:
    server: str
    token: str


def _load_config(config_path: str | None) -> dict:
    # An explicit empty string means "do not read any config file".
    if config_path == "":
        return {}
    candidates: list[Path] = []
    explicit = config_path or os.environ.get("DLW_CONFIG")
    if explicit:
        candidates.append(Path(explicit))
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        if xdg:
            candidates.append(Path(xdg) / "dlw" / "config.yaml")
        candidates.append(Path.home() / ".dlw" / "config.yaml")
    for c in candidates:
        try:
            if c.is_file():
                return yaml.safe_load(c.read_text(encoding="utf-8")) or {}
        except OSError:
            continue
    return {}


def resolve(*, server: str | None, token: str | None,
            config_path: str | None = None) -> Resolved:
    cfg = _load_config(config_path)
    cur = cfg.get("current_context")
    ctx = ((cfg.get("contexts") or {}).get(cur) or {}) if cur else {}
    auth = ((cfg.get("auth") or {}).get(cur) or {}) if cur else {}

    srv = (server or os.environ.get("DLW_SERVER")
           or ctx.get("server") or _DEFAULT_SERVER)
    tok = (token or os.environ.get("DLW_TOKEN")
           or os.environ.get("DLW_SYSTEM_ADMIN_TOKEN")
           or auth.get("access_token"))
    if not tok:
        raise UsageError(
            "no API token: pass --token or set DLW_TOKEN / "
            "DLW_SYSTEM_ADMIN_TOKEN (or configure ~/.dlw/config.yaml)")
    return Resolved(server=str(srv).rstrip("/"), token=str(tok))
```

- [ ] **Step 4: Run** `uv run pytest tests/sdk/test_config.py -v` → 5 PASS.

- [ ] **Step 5: Commit**
```bash
git add src/dlw/sdk/_config.py tests/sdk/test_config.py
git commit -m "feat(sp4): SDK config resolution (flag>env>config>default)"
```

---

### Task 3: HTTP error mapping + DownloadTask model

**Files:**
- Create: `src/dlw/sdk/_http.py`, `src/dlw/sdk/models.py`
- Test: `tests/sdk/test_http_mapping.py`

- [ ] **Step 1: Write the failing test** — `tests/sdk/test_http_mapping.py`:
```python
"""raise_for_status maps HTTP+body to typed errors; DownloadTask.from_api."""
from __future__ import annotations

import httpx
import pytest

from dlw.sdk import errors as e
from dlw.sdk._http import raise_for_status
from dlw.sdk.models import TERMINAL, DownloadTask


def _resp(status, json_body=None, text=""):
    if json_body is not None:
        return httpx.Response(status, json=json_body,
                              request=httpx.Request("GET", "http://t/x"))
    return httpx.Response(status, text=text,
                          request=httpx.Request("GET", "http://t/x"))


def test_2xx_no_raise():
    raise_for_status(_resp(200, {"ok": True}))
    raise_for_status(_resp(204))


@pytest.mark.parametrize("status,cls", [
    (404, e.NotFound), (401, e.AuthError), (403, e.AuthError),
    (429, e.QuotaExceeded), (409, e.Conflict), (500, e.ApiError),
])
def test_status_mapping(status, cls):
    with pytest.raises(cls):
        raise_for_status(_resp(status, {"detail": "boom"}))


def test_conflict_code_and_details():
    with pytest.raises(e.Conflict) as ei:
        raise_for_status(_resp(409, {"detail": {
            "code": "TASK_NOT_TERMINAL", "status": "downloading"}}))
    assert ei.value.code == "TASK_NOT_TERMINAL"
    assert ei.value.details == {"status": "downloading"}


def test_quota_code_promotes_even_if_400():
    with pytest.raises(e.QuotaExceeded):
        raise_for_status(_resp(400, {"detail": {"code": "QUOTA_EXCEEDED"}}))


def test_non_json_body_tolerated():
    with pytest.raises(e.ApiError) as ei:
        raise_for_status(_resp(502, text="bad gateway"))
    assert ei.value.status == 502


def test_downloadtask_from_api():
    t = DownloadTask.from_api({
        "id": "11111111-1111-1111-1111-111111111111",
        "repo_id": "o/r", "revision": "abc", "status": "pending",
        "priority": 1, "created_at": "2026-05-19T00:00:00Z",
        "completed_at": None, "error_message": None,
        "subtasks": [{"status": "pending"}]}, api=None)
    assert t.id == "11111111-1111-1111-1111-111111111111"
    assert t.status == "pending" and t.subtasks == [{"status": "pending"}]
    assert "succeeded" in TERMINAL and "failed" in TERMINAL
    assert "cancelled" in TERMINAL
```

- [ ] **Step 2: Run** `uv run pytest tests/sdk/test_http_mapping.py -v` → FAIL.

- [ ] **Step 3: Implement**

`src/dlw/sdk/_http.py`:
```python
"""Map an httpx error response to a typed dlw.sdk error."""
from __future__ import annotations

import httpx

from dlw.sdk import errors as e


def raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    code = message = trace = None
    details: dict = {}
    try:
        body = resp.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        d = body.get("detail", body)
        if isinstance(d, dict):
            code = d.get("code")
            message = d.get("message") or d.get("detail")
            trace = d.get("trace_id")
            details = d.get("details") or {
                k: v for k, v in d.items()
                if k not in ("code", "message", "trace_id")}
        elif isinstance(d, str):
            message = d
    if message is None:
        message = (resp.text or f"HTTP {resp.status_code}")[:500]
    s = resp.status_code
    if s == 404:
        cls: type[e.DlwError] = e.NotFound
    elif s in (401, 403):
        cls = e.AuthError
    elif s == 429 or code == "QUOTA_EXCEEDED":
        cls = e.QuotaExceeded
    elif s == 409:
        cls = e.Conflict
    else:
        cls = e.ApiError
    raise cls(message, code=code, status=s, trace_id=trace, details=details)
```

`src/dlw/sdk/models.py`:
```python
"""Public DownloadTask wrapper + terminal-status set."""
from __future__ import annotations

from typing import Any

TERMINAL = {"succeeded", "failed", "cancelled"}


class DownloadTask:
    """Wraps a TaskRead/TaskDetail JSON object. `_api` (a TasksAPI or
    AsyncTasksAPI) backs refresh()/wait(); None for detached parsing."""

    def __init__(self, *, id: str, repo_id: str, revision: str, status: str,
                 priority: int, created_at: str, completed_at: str | None,
                 error_message: str | None, subtasks: list[dict],
                 raw: dict, api: Any = None) -> None:
        self.id = id
        self.repo_id = repo_id
        self.revision = revision
        self.status = status
        self.priority = priority
        self.created_at = created_at
        self.completed_at = completed_at
        self.error_message = error_message
        self.subtasks = subtasks
        self.raw = raw
        self._api = api

    @classmethod
    def from_api(cls, data: dict, *, api: Any = None) -> "DownloadTask":
        return cls(
            id=str(data["id"]), repo_id=data["repo_id"],
            revision=data["revision"], status=data["status"],
            priority=data["priority"], created_at=str(data["created_at"]),
            completed_at=data.get("completed_at"),
            error_message=data.get("error_message"),
            subtasks=list(data.get("subtasks") or []),
            raw=data, api=api)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    def files_done(self) -> tuple[int, int]:
        total = len(self.subtasks)
        done = sum(1 for s in self.subtasks
                   if s.get("status") == "succeeded")
        return done, total

    def refresh(self) -> "DownloadTask":
        if self._api is None:
            raise RuntimeError("detached DownloadTask has no client")
        return self._api.get(self.id)

    def wait(self, timeout: float | None = None,
             on_progress=None, poll_interval: float = 5.0) -> "DownloadTask":
        import time
        from dlw.sdk.errors import Timeout
        start = time.monotonic()
        cur: DownloadTask = self
        while cur.status not in TERMINAL:
            if timeout is not None and time.monotonic() - start > timeout:
                raise Timeout(f"task {self.id} not terminal after {timeout}s")
            time.sleep(poll_interval)
            cur = self._api.get(self.id)
            if on_progress is not None:
                on_progress(cur)
        return cur
```

- [ ] **Step 4: Run** `uv run pytest tests/sdk/test_http_mapping.py -v` → all PASS.

- [ ] **Step 5: Commit**
```bash
git add src/dlw/sdk/_http.py src/dlw/sdk/models.py tests/sdk/test_http_mapping.py
git commit -m "feat(sp4): HTTP->typed-error mapping + DownloadTask model"
```

---

### Task 4: Shared SDK test fixtures + sync `Client.tasks.submit/get`

**Files:**
- Create: `src/dlw/sdk/client.py`, `tests/sdk/_fixtures.py`
- Test: `tests/sdk/test_client_sync.py`

- [ ] **Step 1: Write the failing test**

**Pre-review BLOCKER R1/R2 applied.** `httpx==0.27.2` `ASGITransport` is async-only — a sync `httpx.Client` cannot drive it. So: the **async** `AsyncClient` is e2e-tested against the real app via `ASGITransport` through an *async, function-scoped* `aclient` fixture (mirrors `tests/api/test_tasks.py` exactly — proven in the full suite). The **sync `Client` and the CLI** are tested with `httpx.MockTransport` (sync-compatible httpx built-in) backed by a tiny in-memory store in `tests/sdk/_mock.py` returning real FastAPI-shaped responses. `_fixtures.py` declares an explicit `__all__` (incl. the `_`-prefixed autouse fixtures) — without it `from … import *` silently drops them.

`tests/sdk/_fixtures.py` (shared; the async-e2e half mirrors `tests/api/test_tasks.py`):
```python
"""Shared SP4 fixtures: seeded test DB + real system-JWT + async SDK client.

R2: explicit __all__ — `from tests.sdk._fixtures import *` would otherwise
drop the underscore-prefixed autouse fixtures and nothing would be seeded."""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base

SECRET = "unit-secret"

__all__ = ["SECRET", "_bootstrap", "_set_token", "_patch_hf",
           "token", "app", "aclient"]


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Tenant(id=1, slug="default", display_name="Default"))
        await s.flush()
        s.add(Project(id=1, tenant_id=1, name="default"))
        s.add(User(id=1, tenant_id=1, oidc_subject="dev",
                   email="d@l", role="tenant_admin"))
        s.add(StorageBackend(id=1, tenant_id=1, name="default",
                             backend_type="s3", config_encrypted=b""))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _set_token(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _patch_hf(monkeypatch):
    from dlw.services.hf_metadata import RepoFile

    async def fake(*a, **k):
        return [
            RepoFile(path="config.json", size=4096, sha256=None),
            RepoFile(path="model.safetensors", size=64 * 1024,
                     sha256="a" * 64),
        ]
    monkeypatch.setattr("dlw.services.task_service.list_repo_tree", fake)


@pytest.fixture
def token() -> str:
    from dlw.auth.principal import issue_system_jwt
    return issue_system_jwt(secret=SECRET, user_id=1, tenant_id=1,
                            role="tenant_admin", project_ids=[])


@pytest.fixture
def app(ephemeral_ca):
    from tests.conftest import make_app_with_state
    return make_app_with_state(ephemeral_ca, enrollment_token="e")


@pytest_asyncio.fixture
async def aclient(app, token):
    """Async SDK client over the real ASGI app — same pattern/loop as
    tests/api/test_tasks.py's `client` fixture (proven in full suite)."""
    from dlw.sdk.aclient import AsyncClient
    async with AsyncClient(server="http://test", token=token,
                           transport=ASGITransport(app=app)) as c:
        yield c
```

`tests/sdk/_mock.py` — a sync-compatible `httpx.MockTransport` with a tiny in-memory task store mirroring the real API surface (so the sync `Client`/CLI tests are deterministic without a live server; the real-contract risk is owned by the async e2e):
```python
"""Stateful httpx.MockTransport mirroring the real /api/v1/tasks surface.

Used only by the sync Client + CLI tests (httpx 0.27.2 ASGITransport is
async-only). Realistic FastAPI-shaped bodies/status; the async e2e
(test_client_async.py) validates these shapes against the real app."""
from __future__ import annotations

import json
import re
import uuid

import httpx

_VALID = "Bearer good"
TERMINAL = {"succeeded", "failed", "cancelled"}


def make_mock_transport() -> httpx.MockTransport:
    store: dict[str, dict] = {}

    def _task(repo, rev, status="pending"):
        return {"id": str(uuid.uuid4()), "repo_id": repo, "revision": rev,
                "status": status, "priority": 1,
                "created_at": "2026-05-19T00:00:00Z",
                "completed_at": None, "error_message": None}

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        if auth != _VALID:
            return httpx.Response(401, json={"detail": "unauthenticated"})
        path = request.url.path
        m = re.fullmatch(r"/api/v1/tasks/([^/]+)", path)
        mc = re.fullmatch(r"/api/v1/tasks/([^/]+)/cancel", path)
        if request.method == "POST" and path == "/api/v1/tasks":
            body = json.loads(request.content or b"{}")
            t = _task(body["repo_id"], body["revision"])
            store[t["id"]] = {**t, "subtasks": []}
            return httpx.Response(201, json=t)          # TaskRead (no subtasks)
        if request.method == "GET" and path == "/api/v1/tasks":
            return httpx.Response(200, json={
                "items": [{k: v for k, v in t.items() if k != "subtasks"}
                          for t in store.values()],
                "total": len(store)})
        if mc and request.method == "POST":
            t = store.get(mc.group(1))
            if t is None:
                return httpx.Response(404, json={"detail": "task not found"})
            t["status"] = "cancelling"
            return httpx.Response(202, json={
                k: v for k, v in t.items() if k != "subtasks"})
        if m and request.method == "GET":
            t = store.get(m.group(1))
            if t is None:
                return httpx.Response(404, json={"detail": "task not found"})
            return httpx.Response(200, json={**t, "subtasks": [
                {"status": "pending"}, {"status": "pending"}]})  # TaskDetail
        if m and request.method == "DELETE":
            t = store.get(m.group(1))
            if t is None:
                return httpx.Response(404, json={"detail": "task not found"})
            if t["status"] not in TERMINAL:
                return httpx.Response(409, json={"detail": {
                    "code": "TASK_NOT_TERMINAL", "status": t["status"]}})
            del store[m.group(1)]
            return httpx.Response(204)
        return httpx.Response(404, json={"detail": "not found"})

    return httpx.MockTransport(handler)


GOOD_TOKEN = "good"   # the SDK sends "Bearer good"; _mock accepts only that
```

`tests/sdk/test_client_sync.py`:
```python
"""sync Client.tasks.submit/get via httpx.MockTransport (SP4; R1)."""
from __future__ import annotations

import pytest

from dlw.sdk import errors as e
from dlw.sdk.client import Client
from tests.sdk._mock import GOOD_TOKEN, make_mock_transport


def _client():
    return Client(server="http://mock", token=GOOD_TOKEN,
                  transport=make_mock_transport())


def test_submit_then_get():
    with _client() as c:
        t = c.tasks.submit(repo_id="o/r", revision="0" * 40, storage_id=1)
        assert t.status == "pending" and t.repo_id == "o/r" and t.id
        got = c.tasks.get(t.id)
        assert got.id == t.id
        assert len(got.subtasks) == 2          # TaskDetail shape
        assert got.refresh().id == t.id


def test_submit_requires_storage():
    with _client() as c:
        with pytest.raises(TypeError):
            c.tasks.submit(repo_id="o/r", revision="0" * 40)  # no storage_id


def test_bad_token_is_auth_error():
    with Client(server="http://mock", token="wrong",
                transport=make_mock_transport()) as c:
        with pytest.raises(e.AuthError):
            c.tasks.list()
```

- [ ] **Step 2: Run** `uv run pytest tests/sdk/test_client_sync.py -v` → FAIL (no `client` module). (`test_client_sync.py` imports `tests.sdk._mock` only — no DB fixtures, deterministic.)

- [ ] **Step 3: Implement** — `src/dlw/sdk/client.py`:
```python
"""Synchronous dlw SDK client."""
from __future__ import annotations

from typing import Any

import httpx

from dlw.sdk._config import resolve
from dlw.sdk._http import raise_for_status
from dlw.sdk.models import DownloadTask


class TasksAPI:
    def __init__(self, http: httpx.Client) -> None:
        self._h = http

    def submit(self, repo_id: str, revision: str, *, storage_id: int,
               priority: int = 1, source_strategy: str = "auto_balance",
               source_blacklist: list[str] | None = None,
               trust_non_hf_sha256: bool = False,
               upgrade_from_revision: str | None = None,
               path_template: str = "{tenant}/{repo_id}/{revision}",
               ) -> DownloadTask:
        body: dict[str, Any] = {
            "repo_id": repo_id, "revision": revision,
            "storage_id": storage_id, "priority": priority,
            "source_strategy": source_strategy,
            "source_blacklist": source_blacklist or [],
            "trust_non_hf_sha256": trust_non_hf_sha256,
            "path_template": path_template,
        }
        if upgrade_from_revision is not None:
            body["upgrade_from_revision"] = upgrade_from_revision
        r = self._h.post("/api/v1/tasks", json=body)
        raise_for_status(r)
        return DownloadTask.from_api(r.json(), api=self)

    def get(self, task_id: str) -> DownloadTask:
        r = self._h.get(f"/api/v1/tasks/{task_id}")
        raise_for_status(r)
        return DownloadTask.from_api(r.json(), api=self)

    def list(self, *, status: str | list[str] | None = None,
             limit: int = 50) -> list[DownloadTask]:
        r = self._h.get("/api/v1/tasks")
        raise_for_status(r)
        items = r.json().get("items", [])
        if status is not None:
            want = {status} if isinstance(status, str) else set(status)
            items = [i for i in items if i.get("status") in want]
        return [DownloadTask.from_api(i, api=self) for i in items[:limit]]

    def cancel(self, task_id: str, reason: str | None = None) -> None:
        body = {"reason": reason} if reason else {}
        r = self._h.post(f"/api/v1/tasks/{task_id}/cancel", json=body)
        raise_for_status(r)

    def delete(self, task_id: str) -> None:
        r = self._h.request("DELETE", f"/api/v1/tasks/{task_id}")
        raise_for_status(r)


class Client:
    def __init__(self, server: str | None = None, token: str | None = None,
                 *, config_path: str | None = None, timeout: float = 30.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        r = resolve(server=server, token=token, config_path=config_path)
        self._http = httpx.Client(
            base_url=r.server, timeout=timeout,
            headers={"Authorization": f"Bearer {r.token}"},
            transport=transport)
        self.tasks = TasksAPI(self._http)

    @classmethod
    def from_env(cls, **kw: Any) -> "Client":
        return cls(**kw)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
```

- [ ] **Step 4: Run** `uv run pytest tests/sdk/test_client_sync.py -v` → 3 PASS.

- [ ] **Step 5: Commit**
```bash
git add src/dlw/sdk/client.py tests/sdk/_fixtures.py tests/sdk/_mock.py tests/sdk/test_client_sync.py
git commit -m "feat(sp4): sync Client + TasksAPI.submit/get + shared fixtures + mock transport"
```

---

### Task 5: sync `list` / `cancel` / `delete` + error paths

**Files:**
- Test: `tests/sdk/test_client_sync_ops.py` (new)

(`TasksAPI.list/cancel/delete` were implemented in Task 4's `client.py` — this task is the behavioral acceptance + error-mapping coverage.)

- [ ] **Step 1: Write the failing test** — `tests/sdk/test_client_sync_ops.py` (MockTransport, deterministic; R1/R3):
```python
"""list (client-side filter) + cancel + delete + error mapping (SP4)."""
from __future__ import annotations

import pytest

from dlw.sdk import errors as e
from dlw.sdk.client import Client
from tests.sdk._mock import GOOD_TOKEN, make_mock_transport


def _client(token=GOOD_TOKEN):
    return Client(server="http://mock", token=token,
                  transport=make_mock_transport())


def test_list_and_status_filter():
    with _client() as c:
        a = c.tasks.submit(repo_id="o/a", revision="0" * 40, storage_id=1)
        c.tasks.submit(repo_id="o/b", revision="1" * 40, storage_id=1)
        allt = c.tasks.list()
        assert {t.repo_id for t in allt} >= {"o/a", "o/b"}
        assert all(t.status == "pending"
                   for t in c.tasks.list(status="pending"))
        assert c.tasks.list(status="cancelled") == []
        assert len(c.tasks.list(limit=1)) == 1
        assert any(t.id == a.id for t in allt)


def test_cancel_sets_cancelling():
    with _client() as c:
        t = c.tasks.submit(repo_id="o/c", revision="2" * 40, storage_id=1)
        c.tasks.cancel(t.id, reason="user")
        # R3: cancel_task only ever sets "cancelling" synchronously.
        assert c.tasks.get(t.id).status == "cancelling"


def test_delete_non_terminal_raises_conflict():
    with _client() as c:
        t = c.tasks.submit(repo_id="o/d", revision="3" * 40, storage_id=1)
        with pytest.raises(e.Conflict) as ei:
            c.tasks.delete(t.id)        # still pending -> 409
        assert ei.value.code == "TASK_NOT_TERMINAL"


def test_get_missing_raises_notfound():
    with _client() as c:
        with pytest.raises(e.NotFound):
            c.tasks.get("99999999-9999-9999-9999-999999999999")


def test_bad_token_raises_autherror():
    with _client(token="wrong") as c:
        with pytest.raises(e.AuthError):
            c.tasks.list()


def test_missing_token_is_usage_error(monkeypatch):
    for v in ("DLW_TOKEN", "DLW_SYSTEM_ADMIN_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(e.UsageError):
        Client(server="http://test", token=None, config_path="")
```

- [ ] **Step 2: Run** `uv run pytest tests/sdk/test_client_sync_ops.py -v` → expected PASS (logic already in Task 4 `client.py`). If any FAIL, fix `client.py` (not the test). (Deterministic; no DB/app.)

- [ ] **Step 3:** (no new impl — Task 4 implemented these; this task is acceptance coverage.)

- [ ] **Step 4: Run** `uv run pytest tests/sdk/test_client_sync_ops.py -v` → 6 PASS.

- [ ] **Step 5: Commit**
```bash
git add tests/sdk/test_client_sync_ops.py
git commit -m "test(sp4): sync list/cancel/delete + error-mapping coverage"
```

---

# Milestone M2 — wait + async

### Task 6: `DownloadTask.wait` behavior

**Files:**
- Test: `tests/sdk/test_wait.py` (new)

(`wait` was implemented on `DownloadTask` in Task 3 — this task is its behavioral lock with a stub api, no executor/network.)

- [ ] **Step 1: Write the failing test** — `tests/sdk/test_wait.py`:
```python
"""DownloadTask.wait: polls refresh() until terminal / times out (SP4)."""
from __future__ import annotations

import pytest

from dlw.sdk.errors import Timeout
from dlw.sdk.models import DownloadTask


class _StubAPI:
    """get() yields the queued statuses in order, repeating the last."""
    def __init__(self, statuses):
        self._q = list(statuses)

    def get(self, _id):
        st = self._q.pop(0) if len(self._q) > 1 else self._q[0]
        return DownloadTask.from_api({
            "id": "t", "repo_id": "o/r", "revision": "r", "status": st,
            "priority": 1, "created_at": "2026-05-19T00:00:00Z",
            "completed_at": None, "error_message": None, "subtasks": []},
            api=self)


def _task(status, api):
    return DownloadTask.from_api({
        "id": "t", "repo_id": "o/r", "revision": "r", "status": status,
        "priority": 1, "created_at": "2026-05-19T00:00:00Z",
        "completed_at": None, "error_message": None, "subtasks": []},
        api=api)


def test_returns_immediately_when_already_terminal():
    api = _StubAPI(["succeeded"])
    t = _task("succeeded", api)
    assert t.wait(poll_interval=0).status == "succeeded"


def test_polls_until_terminal_and_calls_on_progress():
    api = _StubAPI(["downloading", "downloading", "succeeded"])
    seen: list[str] = []
    t = _task("downloading", api)
    out = t.wait(poll_interval=0, on_progress=lambda x: seen.append(x.status))
    assert out.status == "succeeded"
    assert seen and seen[-1] == "succeeded"


def test_timeout_raises():
    api = _StubAPI(["downloading"])
    t = _task("downloading", api)
    with pytest.raises(Timeout):
        t.wait(timeout=0.01, poll_interval=0.005)
```

- [ ] **Step 2: Run** `uv run pytest tests/sdk/test_wait.py -v` → expected PASS (`wait` from Task 3). If FAIL, fix `models.py`.

- [ ] **Step 3:** (no new impl.)

- [ ] **Step 4: Run** `uv run pytest tests/sdk/test_wait.py -v` → 3 PASS.

- [ ] **Step 5: Commit**
```bash
git add tests/sdk/test_wait.py
git commit -m "test(sp4): DownloadTask.wait poll/terminal/timeout coverage"
```

---

### Task 7: Async client (mirror)

**Files:**
- Create: `src/dlw/sdk/aclient.py`
- Modify: `src/dlw/sdk/__init__.py` (replace skeleton with full exports)
- Test: `tests/sdk/test_client_async.py`

- [ ] **Step 1: Write the failing test** — `tests/sdk/test_client_async.py` (real ASGI app via the async `aclient` fixture — proven `test_tasks.py` pattern; B-B1/R3):
```python
"""async AsyncClient over the REAL ASGI app + DB (SP4).

Uses the async `aclient` fixture from _fixtures.py (same loop as the
session-scoped engine — mirrors tests/api/test_tasks.py exactly)."""
from __future__ import annotations

import pytest

from dlw.sdk import errors as e
from tests.sdk._fixtures import *  # noqa: F401,F403  (fixtures + __all__)

pytestmark = pytest.mark.slow


async def test_async_submit_get_list_cancel(aclient):
    t = await aclient.tasks.submit(repo_id="o/r", revision="0" * 40,
                                   storage_id=1)
    assert t.status == "pending"
    got = await aclient.tasks.get(t.id)
    assert got.id == t.id
    assert len(got.subtasks) == 2          # TaskDetail, patched HF -> 2
    again = await got.refresh()
    assert again.id == t.id
    lst = await aclient.tasks.list(status="pending")
    assert any(x.id == t.id for x in lst)
    await aclient.tasks.cancel(t.id)
    cur = await aclient.tasks.get(t.id)
    assert cur.status == "cancelling"      # R3: never "cancelled" in tests


async def test_async_delete_non_terminal_conflict(aclient):
    t = await aclient.tasks.submit(repo_id="o/x", revision="4" * 40,
                                   storage_id=1)
    with pytest.raises(e.Conflict):
        await aclient.tasks.delete(t.id)


async def test_async_wait_polls_until_terminal():
    from dlw.sdk.aclient import AsyncDownloadTask

    class _AStub:
        def __init__(self, sts):
            self._q = list(sts)

        async def get(self, _id):
            st = self._q.pop(0) if len(self._q) > 1 else self._q[0]
            return AsyncDownloadTask.from_api({
                "id": "t", "repo_id": "o", "revision": "r", "status": st,
                "priority": 1, "created_at": "2026-05-19T00:00:00Z",
                "completed_at": None, "error_message": None,
                "subtasks": []}, api=self)

    api = _AStub(["downloading", "succeeded"])
    t = AsyncDownloadTask.from_api({
        "id": "t", "repo_id": "o", "revision": "r", "status": "downloading",
        "priority": 1, "created_at": "2026-05-19T00:00:00Z",
        "completed_at": None, "error_message": None, "subtasks": []},
        api=api)
    out = await t.wait(poll_interval=0)
    assert out.status == "succeeded"
```

- [ ] **Step 2: Run** `uv run pytest tests/sdk/test_client_async.py -v` → FAIL.

- [ ] **Step 3: Implement**

`src/dlw/sdk/aclient.py`:
```python
"""Asynchronous dlw SDK client — mirrors client.py."""
from __future__ import annotations

from typing import Any

import httpx

from dlw.sdk._config import resolve
from dlw.sdk._http import raise_for_status
from dlw.sdk.errors import Timeout
from dlw.sdk.models import TERMINAL, DownloadTask


class AsyncDownloadTask(DownloadTask):
    async def refresh(self) -> "AsyncDownloadTask":  # type: ignore[override]
        if self._api is None:
            raise RuntimeError("detached AsyncDownloadTask has no client")
        return await self._api.get(self.id)

    async def wait(self, timeout: float | None = None,  # type: ignore[override]
                   on_progress=None,
                   poll_interval: float = 5.0) -> "AsyncDownloadTask":
        import asyncio
        import time
        start = time.monotonic()
        cur: AsyncDownloadTask = self
        while cur.status not in TERMINAL:
            if timeout is not None and time.monotonic() - start > timeout:
                raise Timeout(
                    f"task {self.id} not terminal after {timeout}s")
            await asyncio.sleep(poll_interval)
            cur = await self._api.get(self.id)
            if on_progress is not None:
                on_progress(cur)
        return cur


class AsyncTasksAPI:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._h = http

    async def submit(self, repo_id: str, revision: str, *, storage_id: int,
                      priority: int = 1,
                      source_strategy: str = "auto_balance",
                      source_blacklist: list[str] | None = None,
                      trust_non_hf_sha256: bool = False,
                      upgrade_from_revision: str | None = None,
                      path_template: str = "{tenant}/{repo_id}/{revision}",
                      ) -> AsyncDownloadTask:
        body: dict[str, Any] = {
            "repo_id": repo_id, "revision": revision,
            "storage_id": storage_id, "priority": priority,
            "source_strategy": source_strategy,
            "source_blacklist": source_blacklist or [],
            "trust_non_hf_sha256": trust_non_hf_sha256,
            "path_template": path_template,
        }
        if upgrade_from_revision is not None:
            body["upgrade_from_revision"] = upgrade_from_revision
        r = await self._h.post("/api/v1/tasks", json=body)
        raise_for_status(r)
        return AsyncDownloadTask.from_api(r.json(), api=self)

    async def get(self, task_id: str) -> AsyncDownloadTask:
        r = await self._h.get(f"/api/v1/tasks/{task_id}")
        raise_for_status(r)
        return AsyncDownloadTask.from_api(r.json(), api=self)

    async def list(self, *, status: str | list[str] | None = None,
                   limit: int = 50) -> list[AsyncDownloadTask]:
        r = await self._h.get("/api/v1/tasks")
        raise_for_status(r)
        items = r.json().get("items", [])
        if status is not None:
            want = {status} if isinstance(status, str) else set(status)
            items = [i for i in items if i.get("status") in want]
        return [AsyncDownloadTask.from_api(i, api=self)
                for i in items[:limit]]

    async def cancel(self, task_id: str, reason: str | None = None) -> None:
        body = {"reason": reason} if reason else {}
        r = await self._h.post(f"/api/v1/tasks/{task_id}/cancel", json=body)
        raise_for_status(r)

    async def delete(self, task_id: str) -> None:
        r = await self._h.request("DELETE", f"/api/v1/tasks/{task_id}")
        raise_for_status(r)


class AsyncClient:
    def __init__(self, server: str | None = None, token: str | None = None,
                 *, config_path: str | None = None, timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        r = resolve(server=server, token=token, config_path=config_path)
        self._http = httpx.AsyncClient(
            base_url=r.server, timeout=timeout,
            headers={"Authorization": f"Bearer {r.token}"},
            transport=transport)
        self.tasks = AsyncTasksAPI(self._http)

    @classmethod
    def from_env(cls, **kw: Any) -> "AsyncClient":
        return cls(**kw)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "AsyncClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
```

Replace `src/dlw/sdk/__init__.py` with the full version:
```python
"""dlw Python SDK (Phase 3 SP4) — thin client over the controller REST API.

Monorepo note: the controller owns the `dlw` package, so the SDK lives at
`dlw.sdk` (not top-level `dlw`) to avoid heavy-importing FastAPI/SQLAlchemy.
See docs/superpowers/specs/2026-05-19-phase-3-sp4-cli-sdk-design.md."""
from __future__ import annotations

from dlw.sdk import errors
from dlw.sdk.aclient import AsyncClient, AsyncDownloadTask
from dlw.sdk.client import Client
from dlw.sdk.models import DownloadTask

__all__ = ["Client", "AsyncClient", "AsyncDownloadTask",
           "DownloadTask", "errors"]
```

- [ ] **Step 4: Run** `uv run pytest tests/sdk/test_client_async.py tests/sdk -v` → all PASS (async + every prior sdk test, incl. `import dlw.sdk` now resolving the full `__init__`).

- [ ] **Step 5: Commit**
```bash
git add src/dlw/sdk/aclient.py src/dlw/sdk/__init__.py tests/sdk/test_client_async.py
git commit -m "feat(sp4): async AsyncClient/AsyncDownloadTask mirror + full sdk exports"
```

---

# Milestone M3 — CLI

### Task 8: CLI skeleton (parser, dispatch, exit codes, version)

**Files:**
- Create: `src/dlw/cli/main.py`
- Modify: `pyproject.toml` (`[project.scripts]`)
- Test: `tests/cli/test_cli_skeleton.py`

- [ ] **Step 1: Write the failing test** — `tests/cli/test_cli_skeleton.py`:
```python
"""dlw CLI parser skeleton: version, help, usage errors (SP4)."""
from __future__ import annotations

import pytest

from dlw.cli.main import main


def test_version_exits_zero(capsys):
    assert main(["--version"]) == 0
    assert "dlw" in capsys.readouterr().out


def test_no_command_is_usage_error(capsys):
    assert main([]) == 2


def test_unknown_command_is_usage_error():
    assert main(["frobnicate"]) == 2


def test_missing_token_maps_to_exit_2(monkeypatch):
    for v in ("DLW_TOKEN", "DLW_SYSTEM_ADMIN_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    # `list` with no token + no config -> UsageError -> exit 2
    assert main(["--config", "", "list"]) == 2
```

- [ ] **Step 2: Run** `uv run pytest tests/cli/test_cli_skeleton.py -v` → FAIL.

- [ ] **Step 3: Implement** — `src/dlw/cli/main.py`:
```python
"""`dlw` CLI (Phase 3 SP4) — argparse front-end over dlw.sdk.

CLI-is-SDK: every handler builds a dlw.sdk.Client and calls it. Tests set
`dlw.cli.main._transport` to an httpx.ASGITransport; production leaves it
None (real network)."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from dlw.sdk import errors as e
from dlw.sdk.client import Client

_VERSION = "0.1.0-alpha"
_transport: Any = None          # test seam; None in production


def _make_client(args: argparse.Namespace) -> Client:
    return Client(server=args.server, token=args.token,
                  config_path=args.config, transport=_transport)


def _print_err(exc: e.DlwError, as_json: bool) -> None:
    if as_json:
        sys.stderr.write(json.dumps({
            "code": exc.code, "message": exc.message,
            "trace_id": exc.trace_id, "details": exc.details}) + "\n")
        return
    sys.stderr.write(f"Error: {exc.message}\n")
    if exc.code:
        sys.stderr.write(f"Code:  {exc.code}\n")
    if exc.trace_id:
        sys.stderr.write(f"Trace: {exc.trace_id}\n")
    if exc.details:
        sys.stderr.write("Details:\n")
        for k, v in exc.details.items():
            sys.stderr.write(f"  - {k}: {v}\n")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dlw",
        description="dlw — distributed HuggingFace model downloader CLI")
    p.add_argument("--version", action="store_true",
                   help="print version and exit")
    p.add_argument("--server", default=None, help="API base URL")
    p.add_argument("--token", default=None, help="bearer token")
    p.add_argument("-c", "--config", default=None, help="config file path")
    p.add_argument("-o", "--output", choices=["table", "json"],
                   default="table")
    p.add_argument("-q", "--quiet", action="store_true")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("submit", help="create a download task")
    s.add_argument("repo")
    s.add_argument("-r", "--revision", required=True)
    s.add_argument("-s", "--storage", type=int, required=True)
    s.add_argument("--priority", type=int, default=1)
    s.add_argument("--strategy", default="auto_balance")
    s.add_argument("--upgrade-from", default=None)
    s.add_argument("--wait", action="store_true")
    s.add_argument("--timeout", type=float, default=None)

    sub.add_parser("list", help="list tasks").add_argument(
        "--status", default=None)

    g = sub.add_parser("show", help="show one task")
    g.add_argument("task_id")

    cc = sub.add_parser("cancel", help="cancel a task")
    cc.add_argument("task_id")
    cc.add_argument("--reason", default=None)

    sub.add_parser("delete", help="delete a terminal task").add_argument(
        "task_id")

    w = sub.add_parser("watch", help="poll a task until terminal")
    w.add_argument("task_id")
    w.add_argument("--interval", type=float, default=5.0)
    w.add_argument("--timeout", type=float, default=None)
    return p


def _emit(obj: Any, args: argparse.Namespace) -> None:
    if args.output == "json":
        sys.stdout.write(json.dumps(obj, default=str) + "\n")
        return
    rows = obj if isinstance(obj, list) else [obj]
    if not rows:
        sys.stdout.write("(no tasks)\n")
        return
    cols = ["id", "repo_id", "revision", "status", "priority"]
    widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in rows))
              for c in cols}
    line = "  ".join(c.ljust(widths[c]) for c in cols)
    sys.stdout.write(line + "\n")
    for r in rows:
        sys.stdout.write("  ".join(
            str(r.get(c, "")).ljust(widths[c]) for c in cols) + "\n")


def _dispatch(args: argparse.Namespace) -> int:
    from dlw.cli import handlers
    return handlers.run(args, _make_client, _emit)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    # argparse exits 2 on parse error; intercept --version & no-cmd first.
    argv = sys.argv[1:] if argv is None else argv
    if "--version" in argv:
        sys.stdout.write(f"dlw {_VERSION}\n")
        return 0
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    if not args.cmd:
        parser.print_help(sys.stderr)
        return 2
    try:
        return _dispatch(args)
    except KeyboardInterrupt:
        sys.stderr.write("\nAborted.\n")
        return 8
    except e.DlwError as exc:
        _print_err(exc, args.output == "json")
        return e.exit_code_for(exc)


if __name__ == "__main__":
    raise SystemExit(main())
```

Add to `pyproject.toml` `[project.scripts]` (after the existing `dlw-seed` line):
```toml
dlw = "dlw.cli.main:main"
```

(NOTE: `_dispatch` imports `dlw.cli.handlers`, created in Task 9. Task 8's tests never reach `_dispatch` with a valid command except `list` — and `test_missing_token_maps_to_exit_2` calls `list`, which enters `_dispatch` → `handlers.run`. To keep Task 8 self-contained, create a **minimal** `src/dlw/cli/handlers.py` in Task 8 Step 3 too:)
```python
"""CLI subcommand handlers (SP4)."""
from __future__ import annotations

import argparse
from typing import Any, Callable


def run(args: argparse.Namespace, make_client: Callable,
        emit: Callable) -> int:
    client = make_client(args)        # may raise UsageError (missing token)
    try:
        raise NotImplementedError(args.cmd)   # filled in Task 9/10
    finally:
        client.close()
```
This makes `test_missing_token_maps_to_exit_2` pass (the `make_client(args)` call raises `UsageError` → caught in `main` → exit 2) while leaving real handlers for Task 9/10.

- [ ] **Step 4: Run** `uv run pytest tests/cli/test_cli_skeleton.py -v` → 4 PASS.

- [ ] **Step 5: Commit**
```bash
git add src/dlw/cli/main.py src/dlw/cli/handlers.py pyproject.toml tests/cli/test_cli_skeleton.py
git commit -m "feat(sp4): dlw CLI skeleton (parser/dispatch/exit-codes) + console script"
```

---

### Task 9: CLI `submit` + `show` handlers

**Files:**
- Modify: `src/dlw/cli/handlers.py`
- Test: `tests/cli/test_cli_submit_show.py`

- [ ] **Step 1: Write the failing test** — `tests/cli/test_cli_submit_show.py` (CLI builds a sync `Client`, so the seam injects `MockTransport`; deterministic, no app/DB):
```python
"""dlw submit / show through the SDK + httpx.MockTransport (SP4; R1)."""
from __future__ import annotations

import json

import pytest

import dlw.cli.main as cli
from tests.sdk._mock import GOOD_TOKEN, make_mock_transport


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    monkeypatch.setattr(cli, "_transport", make_mock_transport())
    monkeypatch.setenv("DLW_TOKEN", GOOD_TOKEN)
    monkeypatch.setenv("DLW_SERVER", "http://mock")
    yield
    monkeypatch.setattr(cli, "_transport", None)


def test_submit_json(capsys):
    rc = cli.main(["-o", "json", "submit", "o/r",
                   "-r", "0" * 40, "-s", "1"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["repo_id"] == "o/r" and out["status"] == "pending"


def test_show_after_submit(capsys):
    cli.main(["-o", "json", "submit", "o/s", "-r", "1" * 40, "-s", "1"])
    tid = json.loads(capsys.readouterr().out)["id"]
    rc = cli.main(["-o", "json", "show", tid])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["id"] == tid


def test_show_missing_exit_3(capsys):
    rc = cli.main(["show", "99999999-9999-9999-9999-999999999999"])
    assert rc == 3
```
(NOTE: a fresh `make_mock_transport()` per test = fresh in-memory store; `test_show_after_submit` submits then shows within the SAME `cli.main` process so the same transport instance — set once by `_wire` — persists the task across the two `cli.main` calls. Keep `_wire` building ONE transport for the test.)

- [ ] **Step 2: Run** `uv run pytest tests/cli/test_cli_submit_show.py -v` → FAIL (`NotImplementedError`).

- [ ] **Step 3: Implement** — replace `src/dlw/cli/handlers.py`:
```python
"""CLI subcommand handlers (SP4) — thin SDK calls + render."""
from __future__ import annotations

import argparse
import sys
from typing import Callable


def _task_dict(t) -> dict:
    return {"id": t.id, "repo_id": t.repo_id, "revision": t.revision,
            "status": t.status, "priority": t.priority,
            "created_at": t.created_at, "completed_at": t.completed_at,
            "error_message": t.error_message}


def run(args: argparse.Namespace, make_client: Callable,
        emit: Callable) -> int:
    client = make_client(args)
    try:
        if args.cmd == "submit":
            t = client.tasks.submit(
                repo_id=args.repo, revision=args.revision,
                storage_id=args.storage, priority=args.priority,
                source_strategy=args.strategy,
                upgrade_from_revision=args.upgrade_from)
            if args.wait:
                t = t.wait(timeout=args.timeout)
                if t.status == "failed":
                    sys.stderr.write(
                        f"task {t.id} failed: {t.error_message}\n")
                    emit(_task_dict(t), args)
                    return 1
            emit(_task_dict(t), args)
            return 0
        if args.cmd == "show":
            emit(_task_dict(client.tasks.get(args.task_id)), args)
            return 0
        raise NotImplementedError(args.cmd)   # list/cancel/delete/watch: T10
    finally:
        client.close()
```

- [ ] **Step 4: Run** `uv run pytest tests/cli/test_cli_submit_show.py -v` → 3 PASS.

- [ ] **Step 5: Commit**
```bash
git add src/dlw/cli/handlers.py tests/cli/test_cli_submit_show.py
git commit -m "feat(sp4): CLI submit/show handlers"
```

---

### Task 10: CLI `list` / `cancel` / `delete` / `watch`

**Files:**
- Modify: `src/dlw/cli/handlers.py`
- Test: `tests/cli/test_cli_ops.py`

- [ ] **Step 1: Write the failing test** — `tests/cli/test_cli_ops.py` (MockTransport seam; R1):
```python
"""dlw list/cancel/delete/watch via httpx.MockTransport (SP4)."""
from __future__ import annotations

import json

import pytest

import dlw.cli.main as cli
from tests.sdk._mock import GOOD_TOKEN, make_mock_transport


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    monkeypatch.setattr(cli, "_transport", make_mock_transport())
    monkeypatch.setenv("DLW_TOKEN", GOOD_TOKEN)
    monkeypatch.setenv("DLW_SERVER", "http://mock")
    yield
    monkeypatch.setattr(cli, "_transport", None)


def _submit(capsys, repo, rev):
    cli.main(["-o", "json", "submit", repo, "-r", rev, "-s", "1"])
    return json.loads(capsys.readouterr().out)["id"]


def test_list_json_and_filter(capsys):
    _submit(capsys, "o/l1", "0" * 40)
    rc = cli.main(["-o", "json", "list", "--status", "pending"])
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert isinstance(rows, list) and all(
        r["status"] == "pending" for r in rows)


def test_list_table_nonempty(capsys):
    _submit(capsys, "o/l2", "1" * 40)
    rc = cli.main(["list"])
    assert rc == 0
    assert "repo_id" in capsys.readouterr().out


def test_cancel_exit0(capsys):
    tid = _submit(capsys, "o/cc", "2" * 40)
    assert cli.main(["cancel", tid]) == 0


def test_delete_non_terminal_exit6(capsys):
    tid = _submit(capsys, "o/dd", "3" * 40)
    assert cli.main(["delete", tid]) == 6


def test_watch_terminal_exit0(capsys, monkeypatch):
    tid = _submit(capsys, "o/ww", "4" * 40)
    # MockTransport keeps a task "pending"; stub TasksAPI.get to flip the
    # status to terminal so watch's poll loop exits. `real` is captured
    # BEFORE monkeypatch so it's the unpatched method.
    from dlw.sdk.client import TasksAPI
    real = TasksAPI.get

    def fake_get(self, task_id):
        t = real(self, task_id)        # real HTTP via MockTransport
        t.status = "cancelled"
        return t
    monkeypatch.setattr(TasksAPI, "get", fake_get)
    assert cli.main(["watch", tid, "--interval", "0"]) == 0
```

- [ ] **Step 2: Run** `uv run pytest tests/cli/test_cli_ops.py -v` → FAIL.

- [ ] **Step 3: Implement** — replace the `raise NotImplementedError` tail of `run()` in `src/dlw/cli/handlers.py` with:
```python
        if args.cmd == "list":
            tasks = client.tasks.list(status=args.status)
            emit([_task_dict(t) for t in tasks], args)
            return 0
        if args.cmd == "cancel":
            client.tasks.cancel(args.task_id, reason=args.reason)
            if not args.quiet:
                sys.stdout.write(f"cancelling {args.task_id}\n")
            return 0
        if args.cmd == "delete":
            client.tasks.delete(args.task_id)
            if not args.quiet:
                sys.stdout.write(f"deleted {args.task_id}\n")
            return 0
        if args.cmd == "watch":
            t = client.tasks.get(args.task_id)
            t = t.wait(timeout=args.timeout, poll_interval=args.interval,
                       on_progress=lambda x: sys.stdout.write(
                           f"{x.status} "
                           f"{x.files_done()[0]}/{x.files_done()[1]}\n"))
            emit(_task_dict(t), args)
            return 1 if t.status == "failed" else 0
        raise NotImplementedError(args.cmd)
```
(Place these blocks BEFORE the final `raise NotImplementedError(args.cmd)`, after the `show` block from Task 9. The function structure: submit → show → list → cancel → delete → watch → raise.)

- [ ] **Step 4: Run** `uv run pytest tests/cli/test_cli_ops.py -v` → 5 PASS.

- [ ] **Step 5: Commit**
```bash
git add src/dlw/cli/handlers.py tests/cli/test_cli_ops.py
git commit -m "feat(sp4): CLI list/cancel/delete/watch handlers"
```

---

### Task 11: M3 milestone gate (controller-run — not a subagent task)

- [ ] **Step 1:** `uv run pytest -q` → full suite green: the prior suite count is unchanged + all new SP4 tests pass, **0 failed / 0 errors** (do NOT hardcode the absolute count — read it from the run; SP4 is additive so no prior test should change result).
- [ ] **Step 2:** `uv run python -m pytest tools/test_lint_invariants.py -q` → pass.
- [ ] **Step 3:** `python tools/lint_invariants.py` → `OK: 46 invariants`.
- [ ] **Step 4:** `python tools/lint_no_direct_status_write.py` → OK (SP4 writes no Executor.status).
- [ ] **Step 5:** `npx --yes @stoplight/spectral-cli@6 lint api/openapi.yaml --fail-severity=error` + `npx --yes @apidevtools/swagger-cli validate api/openapi.yaml` → 0 errors / valid (api yaml unchanged by SP4, but run the gate).
- [ ] **Step 6:** `uv run alembic upgrade head` → no-op (SP4 adds no migration; **verify the head is unchanged from `main`, do not hardcode the sha**). Confirm no dependency diff: `git diff --stat origin/main...HEAD` touches only `src/dlw/sdk/*`, `src/dlw/cli/*`, `pyproject.toml` (`[project.scripts]` line only — NOT `dependencies`/`[dependency-groups]`), `tests/sdk/*`, `tests/cli/*`, `docs/*`; `uv.lock` unchanged.
- [ ] No commit (gate only). If anything fails, fix before M4.

---

# Milestone M4 — Docs + final review + PR

### Task 12: Operator/user doc + final review + PR

**Files:**
- Create: `docs/operator/cli-sdk.md`

- [ ] **Step 1: Create** `docs/operator/cli-sdk.md` (~95 lines), structured like `docs/operator/multi-source.md`: H1 `# dlw CLI + Python SDK — Operator/User Guide (SP4)`; a `> **Cross-references**:` blockquote to `docs/v2.0/11-cli-and-sdk-spec.md` (full vision) + the SP4 spec; then sections:
  1. **Install caveat** — quote spec §1 banner: PyPI/Homebrew/curl installers are unreleased placeholders; today the CLI runs via the repo (`uv run dlw ...`) / the console script `dlw` once the package is installed.
  2. **Auth** — token-only (no OIDC in MVP). Precedence: `--token` > `DLW_TOKEN` > `DLW_SYSTEM_ADMIN_TOKEN` > `~/.dlw/config.yaml` (`auth.<context>.access_token`). Server: `--server` > `DLW_SERVER` > config > `http://localhost:8000`. Missing token → exit 2.
  3. **CLI commands** — `submit/list/show/cancel/delete/watch` with one example each (mirror the SDK test invocations), `-o table|json`, exit-code table (0/1/2/3/4/5/6/8/9 per spec §4.1).
  4. **Python SDK** — sync example (`from dlw.sdk import Client; with Client(server=..., token=...) as c: t = c.tasks.submit(repo_id, revision, storage_id=...); t.wait()`) and async example (`from dlw.sdk import AsyncClient; async with AsyncClient(...) as c: ...`). Note the **monorepo import path** is `dlw.sdk` (not `dlw`) and why.
  5. **MVP limitations (authoritative, deferred on purpose)** — verbatim the 3 from the SP4 spec §4: client-side `list` filter; polling `watch`/`wait` (no streaming/events endpoint); token-only auth. **Plus (R4):** `cancel --reason`/`cancel(reason=)` is accepted but **not persisted** — the controller cancel endpoint has no reason field yet; it is reserved, no-op for now. **Plus (I1):** `watch` on an already-terminal task emits only the final record (no progress line) — by design (`wait` returns immediately when status is already terminal). Plus the deferred command list (login/materialize/search/quota/exec/storage/audit/template/admin/completion, idempotency-key, yaml output).
  6. **Cross-ref** `docs/v2.0/11-cli-and-sdk-spec.md` §6-§7 and the SP4 design/plan.

- [ ] **Step 2: Commit**
```bash
git add docs/operator/cli-sdk.md
git commit -m "docs(sp4): dlw CLI + Python SDK operator/user guide"
```

- [ ] **Step 3 (controller):** Dispatch the opus final whole-impl reviewer over `git diff origin/main...HEAD`. SP4 adds a new public package + a console entrypoint — final review is mandatory per the SP1 "production-only wiring" lesson. Specifically scrutinize: (a) `dlw.sdk.__init__` import chain doesn't pull FastAPI/SQLAlchemy into a pure-client import (it imports `client`/`aclient`/`models`/`errors` only — verify none transitively import `dlw.main`/`dlw.db`); (b) the `transport=` seam can never leak into production (default None; only tests set it); (c) token never logged / printed (CLI error formatter prints `code/message/trace/details`, never the Authorization header); (d) `raise_for_status` correctly classifies the real FastAPI bodies (`{"detail": "..."}` vs `{"detail": {"code": ...}}`); (e) exit codes match spec §4.1; (f) no controller endpoint/model/migration/lint changed (additive-only claim holds — `git diff --stat` shows only `src/dlw/sdk/*`, `src/dlw/cli/*`, `pyproject.toml` scripts line, `tests/sdk/*`, `tests/cli/*`, `docs/*`). Address CRITICAL/HIGH; record-and-accept safe HIGHs per the SP2 downgrade lesson.

- [ ] **Step 4 (controller):** `git push -u origin feat/phase-3-sp4-cli-sdk` → `gh pr create --title "Phase 3 SP4 — CLI dlw + Python SDK" --body "<summary: additive SDK+CLI over existing REST API; roadmap §3 MVP; SP1-#15/SP2-#16/SP3-#17 merged; deferred items listed; test plan: full suite + invariant/openapi/yaml gates; no new dep, no api/migration change>"` → `gh pr checks <n> --watch` → squash-merge `--delete-branch` → sync local `main`.

- [ ] **Step 5 (controller):** Update memory: `reference_l17728_modelpull.md` (SP4 merged → **Phase 3 complete**, all 4 sub-projects done, new main sha); `feedback_subagent_driven_dev.md` (SP4 learnings: additive-sub-project pattern = lowest blast radius; spec-vs-implemented-contract corrections caught during plan-writing — `storage_id` required, no `progress`/`file_filter`; reconfirm zero-CI-iteration cycle if it holds).

---

## Self-Review

**Spec coverage:** §1 scope → Tasks 1-10; SDK `Client`/`AsyncClient`/`tasks.{submit,get,list,cancel,delete}`/`DownloadTask.{wait,refresh}` → T3/T4/T5/T6/T7; CLI `submit/list/show/cancel/delete/watch` + global flags + exit codes → T8/T9/T10; config precedence §2.1 → T2; error→exit table §5/§6 → T1 + `_http` T3 + CLI mapping T8; test strategy §7 (pre-review R1: **async** `AsyncClient` e2e via `ASGITransport`+real DB through the async `aclient` fixture mirroring `tests/api/test_tasks.py` → T7; **sync** `Client`+CLI via `httpx.MockTransport`/`_mock.py` → T4/T5/T9/T10; R2 `__all__` in `_fixtures.py`; real JWT, HF patch, FK-ordered seed + teardown drop_all, `__init__.py`, no new dep) → T4 `_fixtures.py`+`_mock.py` + every test; milestones M1-M4 → Tasks grouped accordingly; docs §8 → T12; "purely additive" → asserted in T11 Step 6 + T12 Step 3(f). No spec requirement is unmapped.

**Placeholder scan:** every code step contains complete runnable code; the only "filled in later" markers are the explicit, intentional staged stubs in T1 (`__init__` minimal→full in T7) and T8 (`handlers.run` minimal→T9→T10), each with the exact replacement code given in the later task. No `TODO`/`add error handling`/`similar to`.

**Type/name consistency:** `DownloadTask.from_api(data, *, api=None)`, `.wait(timeout, on_progress, poll_interval)`, `.refresh()`, `.files_done()`, `TERMINAL` — defined T3, reused T4/T5/T6/T7/T10 identically. `TasksAPI.submit(repo_id, revision, *, storage_id, priority, source_strategy, source_blacklist, trust_non_hf_sha256, upgrade_from_revision, path_template)` identical sync (T4) ↔ async (T7). `errors` classes + `exit_code_for` (T1) used by `_http` (T3) and CLI (T8). `_transport`/`_make_client`/`handlers.run(args, make_client, emit)` signature stable T8↔T9↔T10. Fixture names (`transport`,`token`,`app`,`_bootstrap`,`_set_token`,`_patch_hf`) consistent across all test files via `tests/sdk/_fixtures.py`.

**Contract correctness:** `submit` requires `storage_id` (TaskCreate `storage_id: int = Field(gt=0)`); no `file_filter`/`file_glob`/`download_bytes_limit`/`progress` exposed (not in the implemented schemas); submit tests monkeypatch `dlw.services.task_service.list_repo_tree` (create_task calls HF); delete-non-terminal asserts `Conflict`/exit 6 against the real SP3 `{"detail":{"code":"TASK_NOT_TERMINAL"}}`. Additive-only: no task touches `src/dlw/api`, `src/dlw/db`, `src/dlw/services`, `alembic`, `tools/lint_*`, `api/openapi.yaml`.
