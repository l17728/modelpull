# W3b HF Reverse Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a controller-side HF reverse proxy so the tenant HF token never reaches an executor (SEC-02 / INVARIANT 2).

**Architecture:** A new `GET /api/v1/hf-proxy/subtask/{subtask_id}` endpoint authenticates the executor (W3a mTLS+JWT), verifies subtask ownership (confused-deputy guard + assignment_token fence + epoch fence), reconstructs the HF URL from the subtask row, injects `Settings.hf_token`, follows HF's 302→CDN redirect, and streams the bytes back. Executor downloaders fetch through a new `ControllerClient.stream_hf` context manager instead of calling HF directly; `ExecutorSettings.hf_token`/`hf_endpoint` are deleted.

**Tech Stack:** FastAPI, `httpx` (streaming + `follow_redirects`), `fastapi.responses.StreamingResponse`, SQLAlchemy 2.x async, pytest + `httpx.MockTransport` + `httpx.ASGITransport`. No new runtime/dev deps, no new CI jobs, **zero alembic migrations**.

**Spec:** `docs/superpowers/specs/2026-05-14-phase-2-w3b-hf-reverse-proxy-design.md`

**Branch:** `feat/phase-2-w3b-hf-reverse-proxy` (already created off `main`).

---

## File Structure

**New files:**
- `src/dlw/api/hf_proxy.py` — the reverse-proxy router (one endpoint). Owns: auth + ownership verification, HF URL reconstruction, token injection, streaming passthrough.
- `tests/api/test_hf_proxy.py` — proxy endpoint tests (9 cases).
- `tests/tools/__init__.py` + `tests/tools/test_lint_no_hf_token.py` — self-test for the new invariant lint.
- `tests/test_config_w3b.py` — `Settings.hf_proxy_timeout_seconds` config test.

**Modified files:**
- `src/dlw/config.py` — `Settings` gains `hf_proxy_timeout_seconds`.
- `src/dlw/main.py` — `create_app()` includes the hf_proxy router.
- `src/dlw/executor/client.py` — `ControllerClient` gains `stream_hf` (`@asynccontextmanager`).
- `src/dlw/executor/types.py` — `Assignment` gains `assignment_token`.
- `src/dlw/executor/downloader.py` — `HfS3StreamDownloader` rewired to `client.stream_hf`; `_make_http_client` deleted.
- `src/dlw/executor/chunk_downloader.py` — `DirectOffsetDownloader` rewired to `client.stream_hf`; `_resolve_size` becomes a range probe.
- `src/dlw/executor/runner.py` — `_execute_subtask` threads `assignment_token` into `Assignment`.
- `src/dlw/executor/cli.py` — builds `ControllerClient` first, passes it into both downloaders.
- `src/dlw/executor/config.py` — `ExecutorSettings.hf_token` + `hf_endpoint` deleted.
- `src/dlw/executor/_io.py` — `make_http_client` deleted (now unused).
- `tools/lint_invariants.py` — gains `check_no_hf_token_in_executor`.
- `tests/conftest.py` — gains `make_fake_controller_client` test double.
- `tests/executor/test_downloader.py` — migrated to the fake controller client.
- `tests/executor/test_chunk_downloader.py` — migrated to the fake controller client; `_resolve_size` tests added.
- `tests/e2e/test_executor_e2e.py` — HF MockTransport moves to the controller side.
- `api/openapi.yaml` — documents the new endpoint.
- `docs/operator/executor-runbook.md` — notes removed env vars + bandwidth tradeoff.

**Out of scope (do NOT touch):** `tests/e2e/test_hf_s3_smoke_local.py` — `@pytest.mark.manual` (never runs on CI) and already stale from W3a (uses removed `ControllerClient(bearer_token=...)` kwarg + wrong `ExecutorRunner(downloader=...)` kwarg). Leave it; fixing it is not W3b's concern.

---

## Milestone 1 — Controller-side proxy endpoint

### Task 1: Add `hf_proxy_timeout_seconds` to controller Settings

**Files:**
- Modify: `src/dlw/config.py:33-37`
- Test: `tests/test_config_w3b.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_w3b.py`:

```python
"""W3b: Settings.hf_proxy_timeout_seconds config field."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from dlw.config import Settings


def test_settings_has_hf_proxy_timeout_default() -> None:
    s = Settings()
    assert s.hf_proxy_timeout_seconds == 300


def test_settings_hf_proxy_timeout_rejects_below_min() -> None:
    with pytest.raises(ValidationError):
        Settings(hf_proxy_timeout_seconds=5)


def test_settings_hf_proxy_timeout_rejects_above_max() -> None:
    with pytest.raises(ValidationError):
        Settings(hf_proxy_timeout_seconds=4000)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config_w3b.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'hf_proxy_timeout_seconds'`.

- [ ] **Step 3: Add the field**

In `src/dlw/config.py`, after the W3a block (the line `tls_trusted_proxy: bool = Field(default=False)`) and before the `@property` `db_url`, add:

```python
    # Phase 2 W3b — HF reverse-proxy
    hf_proxy_timeout_seconds: int = Field(default=300, ge=10, le=3600)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config_w3b.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/dlw/config.py tests/test_config_w3b.py
git commit -m "feat(config): add hf_proxy_timeout_seconds Setting (W3b)"
```

---

### Task 2: HF reverse-proxy endpoint — happy path (streaming, token injection, URL reconstruction)

This task builds the endpoint with auth + subtask lookup + URL reconstruction + streaming. The ownership/fence verification chain is added in Task 3 (so Task 3's tests have a real red phase).

**Files:**
- Create: `src/dlw/api/hf_proxy.py`
- Modify: `src/dlw/main.py:122-135`
- Test: `tests/api/test_hf_proxy.py` (create)

- [ ] **Step 1: Write the failing test file**

Create `tests/api/test_hf_proxy.py`:

```python
"""W3b: GET /api/v1/hf-proxy/subtask/{id} — controller-side HF reverse proxy."""
from __future__ import annotations

import uuid

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base
from dlw.db.models.task import FileSubTask
from tests.conftest import (
    executor_request_headers, register_test_executor, signed_heartbeat_headers,
)

_TOKEN = "hf-proxy-ui-token"
_ENROLL = "hf-proxy-enrollment-token"
_HF_TOKEN = "test-hf-token-xyz"


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    """Tables + minimal seed (tenant + project + user + storage)."""
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
async def _cleanup_tasks(engine):
    """Truncate task rows after each test so a later test's poll cannot claim a
    prior test's leftover subtask (matches tests/api/test_subtasks.py)."""
    yield
    from sqlalchemy import text
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE file_subtasks, download_tasks CASCADE"))


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DLW_BEARER_TOKEN", _TOKEN)
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    monkeypatch.setenv("DLW_HF_TOKEN", _HF_TOKEN)
    monkeypatch.setenv("DLW_HF_ENDPOINT", "https://huggingface.co")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def proxy_app(ephemeral_ca):
    from dlw.auth.hmac_nonce import NonceStore
    from dlw.main import create_app
    app = create_app()
    app.state.ca = ephemeral_ca["ca"]
    app.state.jwt_keypair = ephemeral_ca["jwt_keypair"]
    app.state.nonce_store = NonceStore(maxsize=1000, ttl_seconds=300)
    app.state.enrollment_token = _ENROLL
    return app


def _install_hf_mock(monkeypatch, handler):
    """Point the proxy's HF client factory at an httpx.MockTransport(handler)."""
    import dlw.api.hf_proxy as hf_proxy_mod

    def _fake_make_hf_client(timeout_seconds):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True,
        )
    monkeypatch.setattr(hf_proxy_mod, "_make_hf_client", _fake_make_hf_client)


async def _create_task_and_claim(c, reg):
    """POST a task (2 files via the _patch_hf_global autouse fixture), heartbeat
    once to flip the executor joining->healthy (the scheduler only assigns work
    to healthy/degraded executors), poll once as `reg`, and return
    (subtask_id, assignment_token, filename)."""
    r = await c.post("/api/v1/tasks", json={
        "repo_id": "deepseek-ai/DeepSeek-V3",
        "revision": "a" * 40,
        "storage_id": 1,
    }, headers={"Authorization": f"Bearer {_TOKEN}"})
    assert r.status_code == 201, r.text
    hb_body = b'{"health_score": 100, "parts_dir_bytes": 0, "disk_free_gb": 100}'
    r = await c.post(f"/api/v1/executors/{reg['executor_id']}/heartbeat",
                     content=hb_body,
                     headers=signed_heartbeat_headers(reg, hb_body))
    assert r.status_code == 200, r.text
    r = await c.post(f"/api/v1/executors/{reg['executor_id']}/poll",
                     headers=executor_request_headers(reg))
    assert r.status_code == 200, r.text
    assert r.json()["assigned"] is True
    sub = r.json()["subtask"]
    return sub["id"], r.json()["assignment_token"], sub["filename"]


@pytest.mark.slow
async def test_proxy_streams_file_with_token_injected(
    proxy_app, monkeypatch,
) -> None:
    seen: dict = {}

    def hf_handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, content=b"PROXIED-BYTES")
    _install_hf_mock(monkeypatch, hf_handler)

    async with AsyncClient(transport=ASGITransport(app=proxy_app),
                           base_url="http://test") as c:
        reg = await register_test_executor(
            c, enrollment_token=_ENROLL,
            executor_id="hp-worker-1", host_id="hp-host",
        )
        sub_id, token, _ = await _create_task_and_claim(c, reg)
        r = await c.get(f"/api/v1/hf-proxy/subtask/{sub_id}", headers={
            **executor_request_headers(reg),
            "X-Assignment-Token": token,
        })

    assert r.status_code == 200, r.text
    assert r.content == b"PROXIED-BYTES"
    assert seen["auth"] == f"Bearer {_HF_TOKEN}"


@pytest.mark.slow
async def test_proxy_forwards_range_header(proxy_app, monkeypatch) -> None:
    seen: dict = {}

    def hf_handler(request: httpx.Request) -> httpx.Response:
        seen["range"] = request.headers.get("range")
        return httpx.Response(206, content=b"RANGE",
                              headers={"Content-Range": "bytes 0-4/100"})
    _install_hf_mock(monkeypatch, hf_handler)

    async with AsyncClient(transport=ASGITransport(app=proxy_app),
                           base_url="http://test") as c:
        reg = await register_test_executor(
            c, enrollment_token=_ENROLL,
            executor_id="hp-worker-2", host_id="hp-host",
        )
        sub_id, token, _ = await _create_task_and_claim(c, reg)
        r = await c.get(f"/api/v1/hf-proxy/subtask/{sub_id}", headers={
            **executor_request_headers(reg),
            "X-Assignment-Token": token,
            "Range": "bytes=0-4",
        })

    assert r.status_code == 206
    assert seen["range"] == "bytes=0-4"
    assert r.headers["content-range"] == "bytes 0-4/100"


@pytest.mark.slow
async def test_proxy_reconstructs_url_from_subtask(proxy_app, monkeypatch) -> None:
    seen: dict = {}

    def hf_handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, content=b"OK")
    _install_hf_mock(monkeypatch, hf_handler)

    async with AsyncClient(transport=ASGITransport(app=proxy_app),
                           base_url="http://test") as c:
        reg = await register_test_executor(
            c, enrollment_token=_ENROLL,
            executor_id="hp-worker-3", host_id="hp-host",
        )
        sub_id, token, filename = await _create_task_and_claim(c, reg)
        r = await c.get(f"/api/v1/hf-proxy/subtask/{sub_id}", headers={
            **executor_request_headers(reg),
            "X-Assignment-Token": token,
        })

    assert r.status_code == 200
    expected = (f"https://huggingface.co/deepseek-ai/DeepSeek-V3"
                f"/resolve/{'a' * 40}/{filename}")
    assert seen["url"] == expected


@pytest.mark.slow
async def test_proxy_forwards_hf_429(proxy_app, monkeypatch) -> None:
    def hf_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"rate limited")
    _install_hf_mock(monkeypatch, hf_handler)

    async with AsyncClient(transport=ASGITransport(app=proxy_app),
                           base_url="http://test") as c:
        reg = await register_test_executor(
            c, enrollment_token=_ENROLL,
            executor_id="hp-worker-4", host_id="hp-host",
        )
        sub_id, token, _ = await _create_task_and_claim(c, reg)
        r = await c.get(f"/api/v1/hf-proxy/subtask/{sub_id}", headers={
            **executor_request_headers(reg),
            "X-Assignment-Token": token,
        })

    assert r.status_code == 429


@pytest.mark.slow
async def test_proxy_rejects_unauthenticated(proxy_app) -> None:
    async with AsyncClient(transport=ASGITransport(app=proxy_app),
                           base_url="http://test") as c:
        r = await c.get(f"/api/v1/hf-proxy/subtask/{uuid.uuid4()}", headers={
            "X-Assignment-Token": str(uuid.uuid4()),
        })
    assert r.status_code == 401


@pytest.mark.slow
async def test_proxy_404_on_missing_subtask(proxy_app) -> None:
    async with AsyncClient(transport=ASGITransport(app=proxy_app),
                           base_url="http://test") as c:
        reg = await register_test_executor(
            c, enrollment_token=_ENROLL,
            executor_id="hp-worker-5", host_id="hp-host",
        )
        r = await c.get(f"/api/v1/hf-proxy/subtask/{uuid.uuid4()}", headers={
            **executor_request_headers(reg),
            "X-Assignment-Token": str(uuid.uuid4()),
        })
    assert r.status_code == 404
```

- [ ] **Step 2: Run the test file to verify it fails**

Run: `uv run pytest tests/api/test_hf_proxy.py -v`
Expected: FAIL — all cases error/404 because the route does not exist yet (`GET /api/v1/hf-proxy/...` → 404 from FastAPI's default handler, or import error if `dlw.api.hf_proxy` is referenced).

- [ ] **Step 3: Create the proxy router**

Create `src/dlw/api/hf_proxy.py`:

```python
"""HF reverse-proxy — controller-side, injects the tenant HF token (SEC-02).

The executor never holds the HF token (INVARIANT 2). It calls this proxy keyed
by subtask_id; the controller verifies ownership (assignment_token + epoch fence
+ confused-deputy guard — added in Task 3), reconstructs the HF URL from the
subtask row, injects Settings.hf_token, follows HF's 302->CDN redirect
server-side, and streams the bytes back.
"""
from __future__ import annotations

import uuid

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.api.tasks import _session
from dlw.auth.executor_jwt_dep import require_executor_jwt
from dlw.config import get_settings
from dlw.db.models.executor import Executor
from dlw.db.models.task import DownloadTask, FileSubTask

router = APIRouter(prefix="/api/v1/hf-proxy", tags=["executors"])

_HF_HEADER_ALLOWLIST = frozenset({
    "content-length", "content-range", "content-type",
    "accept-ranges", "etag",
})


def _make_hf_client(timeout_seconds: int) -> httpx.AsyncClient:
    """Test seam — monkeypatched in tests to inject an httpx.MockTransport."""
    return httpx.AsyncClient(follow_redirects=True, timeout=timeout_seconds)


@router.get("/subtask/{subtask_id}")
async def hf_proxy_subtask(
    subtask_id: uuid.UUID,
    request: Request,
    x_assignment_token: str = Header(..., alias="X-Assignment-Token"),
    auth_ex: Executor = Depends(require_executor_jwt),
    session: AsyncSession = Depends(_session),
) -> StreamingResponse:
    sub = await session.get(FileSubTask, subtask_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="subtask not found")

    task = await session.get(DownloadTask, sub.task_id)
    if task is None:                       # FK guarantees this won't happen
        raise HTTPException(status_code=500, detail="parent task missing")

    settings = get_settings()
    hf_url = (f"{settings.hf_endpoint.rstrip('/')}/{task.repo_id}"
              f"/resolve/{task.revision}/{sub.filename}")

    hf_headers: dict[str, str] = {}
    if settings.hf_token:
        hf_headers["Authorization"] = f"Bearer {settings.hf_token}"
    range_header = request.headers.get("Range")
    if range_header:
        hf_headers["Range"] = range_header

    hf_client = _make_hf_client(settings.hf_proxy_timeout_seconds)
    hf_req = hf_client.build_request("GET", hf_url, headers=hf_headers)
    try:
        hf_resp = await hf_client.send(hf_req, stream=True)
    except (httpx.TimeoutException, httpx.NetworkError) as e:
        await hf_client.aclose()
        raise HTTPException(
            status_code=503, detail=f"HF upstream unreachable: {e}",
        ) from e
    except BaseException:
        await hf_client.aclose()
        raise

    fwd = {
        k: v for k, v in hf_resp.headers.items()
        if k.lower() in _HF_HEADER_ALLOWLIST
    }

    async def _body():
        try:
            async for chunk in hf_resp.aiter_bytes(64 * 1024):
                yield chunk
        finally:
            await hf_resp.aclose()
            await hf_client.aclose()

    return StreamingResponse(
        _body(), status_code=hf_resp.status_code, headers=fwd,
    )
```

> The `try/except` around `hf_client.send()` guarantees the per-request client is closed even when the HF connection fails before the `StreamingResponse` body generator is constructed (otherwise the `AsyncClient` + its transport leak). Transport errors map to a `503` so executors can distinguish "HF unreachable, retry" from a controller bug; any other exception closes the client and re-raises unchanged.

- [ ] **Step 4: Wire the router into the app**

In `src/dlw/main.py`, inside `create_app()`, after the `subtasks_router` include (line ~134) and before `return app`, add:

```python
    from dlw.api.hf_proxy import router as hf_proxy_router
    app.include_router(hf_proxy_router)
```

- [ ] **Step 5: Run the test file to verify it passes**

Run: `uv run pytest tests/api/test_hf_proxy.py -v`
Expected: PASS (6 passed) — the 6 Task-2 cases.

- [ ] **Step 6: Commit**

```bash
git add src/dlw/api/hf_proxy.py src/dlw/main.py tests/api/test_hf_proxy.py
git commit -m "feat(api): HF reverse-proxy endpoint — streaming + token injection (W3b)"
```

---

### Task 3: HF reverse-proxy — ownership + fence verification chain

Adds the three checks between the 404 check and the `task = ...` lookup: confused-deputy guard, assignment_token fence, epoch fence.

**Files:**
- Modify: `src/dlw/api/hf_proxy.py`
- Test: `tests/api/test_hf_proxy.py` (append 3 cases)

- [ ] **Step 1: Write the failing tests**

Append to `tests/api/test_hf_proxy.py`:

```python
@pytest.mark.slow
async def test_proxy_rejects_not_your_subtask(proxy_app) -> None:
    """Authenticated executor B cannot proxy-fetch executor A's subtask."""
    async with AsyncClient(transport=ASGITransport(app=proxy_app),
                           base_url="http://test") as c:
        reg_a = await register_test_executor(
            c, enrollment_token=_ENROLL,
            executor_id="hp-owner-a", host_id="hp-host",
        )
        reg_b = await register_test_executor(
            c, enrollment_token=_ENROLL,
            executor_id="hp-owner-b", host_id="hp-host",
        )
        sub_id, token, _ = await _create_task_and_claim(c, reg_a)
        r = await c.get(f"/api/v1/hf-proxy/subtask/{sub_id}", headers={
            **executor_request_headers(reg_b),
            "X-Assignment-Token": token,
        })
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "NOT_YOUR_SUBTASK"


@pytest.mark.slow
async def test_proxy_rejects_stale_assignment_token(proxy_app) -> None:
    async with AsyncClient(transport=ASGITransport(app=proxy_app),
                           base_url="http://test") as c:
        reg = await register_test_executor(
            c, enrollment_token=_ENROLL,
            executor_id="hp-worker-stale", host_id="hp-host",
        )
        sub_id, _token, _ = await _create_task_and_claim(c, reg)
        r = await c.get(f"/api/v1/hf-proxy/subtask/{sub_id}", headers={
            **executor_request_headers(reg),
            "X-Assignment-Token": str(uuid.uuid4()),   # wrong token
        })
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "STALE_ASSIGNMENT"


@pytest.mark.slow
async def test_proxy_rejects_epoch_mismatch(proxy_app, db_session) -> None:
    async with AsyncClient(transport=ASGITransport(app=proxy_app),
                           base_url="http://test") as c:
        reg = await register_test_executor(
            c, enrollment_token=_ENROLL,
            executor_id="hp-worker-epoch", host_id="hp-host",
        )
        sub_id, token, _ = await _create_task_and_claim(c, reg)
        # Corrupt the subtask's stamped epoch so it no longer matches the
        # authenticated executor's current epoch. commit() (not flush()) is
        # required: the proxy reads via its own _session dependency on a
        # separate connection, so the update must be committed to be visible.
        await db_session.execute(
            update(FileSubTask)
            .where(FileSubTask.id == uuid.UUID(sub_id))
            .values(executor_epoch=99_999)
        )
        await db_session.commit()
        r = await c.get(f"/api/v1/hf-proxy/subtask/{sub_id}", headers={
            **executor_request_headers(reg),
            "X-Assignment-Token": token,
        })
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "EPOCH_MISMATCH"
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/api/test_hf_proxy.py -v -k "not_your_subtask or stale_assignment or epoch_mismatch"`
Expected: FAIL — the handler has no ownership/fence checks yet, so it proceeds to the HF call. `not_your_subtask` and `stale_assignment` reach the (un-mocked) real `_make_hf_client` and error or return a non-403/409 status; `epoch_mismatch` likewise. All three assertions fail.

- [ ] **Step 3: Add the verification chain**

In `src/dlw/api/hf_proxy.py`, replace this block:

```python
    sub = await session.get(FileSubTask, subtask_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="subtask not found")

    task = await session.get(DownloadTask, sub.task_id)
```

with:

```python
    sub = await session.get(FileSubTask, subtask_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="subtask not found")
    if sub.executor_id != auth_ex.id:
        raise HTTPException(
            status_code=403,
            detail={"code": "NOT_YOUR_SUBTASK",
                    "subtask_executor": sub.executor_id,
                    "authenticated": auth_ex.id},
        )
    if sub.assignment_token is None or str(sub.assignment_token) != x_assignment_token:
        raise HTTPException(
            status_code=409, detail={"code": "STALE_ASSIGNMENT"},
        )
    if sub.executor_epoch != auth_ex.epoch:
        raise HTTPException(
            status_code=409,
            detail={"code": "EPOCH_MISMATCH",
                    "expected": sub.executor_epoch, "got": auth_ex.epoch},
        )

    task = await session.get(DownloadTask, sub.task_id)
```

- [ ] **Step 4: Run the full proxy test file to verify all pass**

Run: `uv run pytest tests/api/test_hf_proxy.py -v`
Expected: PASS (9 passed).

- [ ] **Step 5: Commit**

```bash
git add src/dlw/api/hf_proxy.py tests/api/test_hf_proxy.py
git commit -m "feat(api): HF proxy ownership + assignment_token + epoch fence (W3b)"
```

---

## Milestone 2 — Executor rewiring

### Task 4: `Assignment.assignment_token` + `ControllerClient.stream_hf`

**Files:**
- Modify: `src/dlw/executor/types.py:14-24`
- Modify: `src/dlw/executor/client.py`
- Test: `tests/executor/test_client.py` (append 1 case)

- [ ] **Step 1: Write the failing test**

Append to `tests/executor/test_client.py`:

```python
@pytest.mark.slow
async def test_stream_hf_attaches_auth_and_token_headers(tmp_path) -> None:
    """stream_hf carries Authorization + X-Executor-Epoch + X-Assignment-Token,
    forwards Range when given, and hits /api/v1/hf-proxy/subtask/{id}."""
    seen: dict = {}
    body = b"hf-proxied-bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        seen["path"] = request.url.path
        return httpx.Response(200, content=body)

    state = make_fake_auth_state(
        tmp_path, executor_id="ex-stream", epoch=4, jwt="jwt-stream",
        hmac_seed=_HMAC_SEED,
    )
    sub_id = uuid.uuid4()
    tok = uuid.uuid4()
    async with ControllerClient(
        base_url="http://test", auth_state=state,
        _transport=httpx.MockTransport(handler),
    ) as c:
        async with c.stream_hf(
            subtask_id=sub_id, assignment_token=tok,
            range_header="bytes=0-1023",
        ) as resp:
            chunks = [chunk async for chunk in resp.aiter_bytes(8)]

    assert b"".join(chunks) == body
    assert seen["path"] == f"/api/v1/hf-proxy/subtask/{sub_id}"
    assert seen["headers"]["authorization"] == "Bearer jwt-stream"
    assert seen["headers"]["x-executor-epoch"] == "4"
    assert seen["headers"]["x-assignment-token"] == str(tok)
    assert seen["headers"]["range"] == "bytes=0-1023"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/executor/test_client.py::test_stream_hf_attaches_auth_and_token_headers -v`
Expected: FAIL — `AttributeError: 'ControllerClient' object has no attribute 'stream_hf'`.

- [ ] **Step 3: Add `assignment_token` to `Assignment`**

In `src/dlw/executor/types.py`, change the `Assignment` dataclass body to insert `assignment_token` right after `task_id`:

```python
@dataclass(frozen=True)
class Assignment:
    """Slim payload passed from runner to downloader."""
    subtask_id: uuid.UUID
    task_id: uuid.UUID
    assignment_token: uuid.UUID
    repo_id: str
    revision: str
    filename: str
    file_size: int | None
    expected_sha256: str | None
    storage_config: StorageConfig
```

- [ ] **Step 4: Add `stream_hf` to `ControllerClient`**

In `src/dlw/executor/client.py`, add `asynccontextmanager` + `AsyncIterator` to the imports — change:

```python
import secrets
import time
import uuid
from typing import Any, Self
```

to:

```python
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Self
```

Then add the `stream_hf` method at the end of the `ControllerClient` class (after `report`). The `-> AsyncIterator[httpx.Response]` return annotation is required — `pyproject.toml` sets `[tool.mypy] strict = true`, so an unannotated method fails `no-untyped-def`:

```python
    @asynccontextmanager
    async def stream_hf(
        self,
        *,
        subtask_id: uuid.UUID,
        assignment_token: uuid.UUID,
        range_header: str | None = None,
    ) -> AsyncIterator[httpx.Response]:
        """W3b: stream a file from HF via the controller reverse-proxy. Yields
        the httpx streaming Response — callers consume resp.aiter_bytes() and
        check resp.status_code, exactly as they did with a direct HF GET.

        Unlike heartbeat/poll/report, this does NOT call raise_for_status() —
        callers MUST inspect resp.status_code themselves (the downloaders do)."""
        headers = {
            **self._auth_headers(),
            "X-Assignment-Token": str(assignment_token),
        }
        if range_header:
            headers["Range"] = range_header
        async with self._make_client() as client:
            async with client.stream(
                "GET",
                f"/api/v1/hf-proxy/subtask/{subtask_id}",
                headers=headers,
            ) as resp:
                yield resp
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/executor/test_client.py -v`
Expected: PASS (all `test_client.py` cases, including the new one).

- [ ] **Step 6: Commit**

```bash
git add src/dlw/executor/types.py src/dlw/executor/client.py tests/executor/test_client.py
git commit -m "feat(executor): Assignment.assignment_token + ControllerClient.stream_hf (W3b)"
```

---

### Task 5: Rewire `HfS3StreamDownloader` to the controller proxy

**Files:**
- Modify: `tests/conftest.py` (add `make_fake_controller_client`)
- Modify: `tests/executor/test_downloader.py`
- Modify: `src/dlw/executor/downloader.py`

- [ ] **Step 1: Add the `make_fake_controller_client` test double to conftest**

Append to `tests/conftest.py`:

```python
def make_fake_controller_client(hf_handler):
    """W3b test double for ControllerClient — its stream_hf() routes through an
    httpx.MockTransport(hf_handler) instead of a real controller proxy. Lets
    downloader tests simulate HF responses without a running controller.
    hf_handler is a Callable[[httpx.Request], httpx.Response]."""
    import httpx as _httpx
    from contextlib import asynccontextmanager as _acm

    class _FakeControllerClient:
        def __init__(self):
            self._transport = _httpx.MockTransport(hf_handler)

        @_acm
        async def stream_hf(self, *, subtask_id, assignment_token,
                            range_header=None):
            headers = {"X-Assignment-Token": str(assignment_token)}
            if range_header:
                headers["Range"] = range_header
            async with _httpx.AsyncClient(
                transport=self._transport, base_url="http://fake-controller",
            ) as client:
                async with client.stream(
                    "GET", f"/api/v1/hf-proxy/subtask/{subtask_id}",
                    headers=headers,
                ) as resp:
                    yield resp

    return _FakeControllerClient()
```

- [ ] **Step 2: Migrate `tests/executor/test_downloader.py`**

Replace the imports block (lines 1-22) — change the `from dlw.executor.downloader import (...)` line and add the conftest import:

Replace:

```python
from dlw.executor.config import ExecutorSettings
from dlw.executor.downloader import (
    Assignment,
    DownloadResult,
    HfS3StreamDownloader,
    StorageConfig,
)
```

with:

```python
from dlw.executor.config import ExecutorSettings
from dlw.executor.downloader import (
    Assignment,
    DownloadResult,
    HfS3StreamDownloader,
    StorageConfig,
)
from tests.conftest import make_fake_controller_client
```

Replace the `_assignment` helper (lines 32-41) — add `assignment_token`:

```python
def _assignment(*, repo_id="o/r", revision="a" * 40, filename="config.json",
                key_prefix="phase1/", bucket="b") -> Assignment:
    import uuid as _uuid
    return Assignment(
        subtask_id=_uuid.uuid4(),
        task_id=_uuid.uuid4(),
        assignment_token=_uuid.uuid4(),
        repo_id=repo_id, revision=revision, filename=filename,
        file_size=4096, expected_sha256=None,
        storage_config=StorageConfig(bucket=bucket, key_prefix=key_prefix),
    )
```

Add a shared no-op client helper right after `_assignment` (the three `test_compose_key_*` tests never stream, but the constructor now requires `client`):

```python
def _noop_client():
    return make_fake_controller_client(
        lambda request: httpx.Response(200, content=b"")
    )
```

Update the three `test_compose_key_*` tests — replace each `HfS3StreamDownloader(settings=_settings())` with `HfS3StreamDownloader(settings=_settings(), client=_noop_client())`. There are three occurrences (lines 45, 52, 59).

Replace the `_make_hf_transport` helper (lines 81-85) with a handler-returning version:

```python
def _hf_handler(body_bytes: bytes):
    """Returns an httpx handler that streams body_bytes for any request."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body_bytes)
    return handler
```

Now migrate the seven streaming tests. The pattern is: each currently does

```python
    d = HfS3StreamDownloader(settings=settings)
    monkeypatch.setattr(d, "_make_http_client",
        lambda: httpx.AsyncClient(transport=<TRANSPORT>, ...))
```

Replace each with a single line:

```python
    d = HfS3StreamDownloader(settings=settings,
                             client=make_fake_controller_client(<HANDLER>))
```

Apply per test:

`test_downloader_streams_hf_to_s3_full_pipeline` — replace lines 100-106 with:

```python
    d = HfS3StreamDownloader(settings=settings,
                             client=make_fake_controller_client(_hf_handler(body)))
```

`test_downloader_small_file_single_part` — replace lines 133-137 with:

```python
    d = HfS3StreamDownloader(settings=settings,
                             client=make_fake_controller_client(_hf_handler(body)))
```

`test_downloader_exact_5mb_yields_one_part` — replace lines 159-163 with:

```python
    d = HfS3StreamDownloader(settings=settings,
                             client=make_fake_controller_client(_hf_handler(body)))
```

`test_downloader_404_fails_fast_no_multipart` — this test defines its own `handler` (returns 404) and builds a `transport`. Replace lines 179-188 (the `def handler` ... through the `monkeypatch.setattr`) with:

```python
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"not found")

    settings = ExecutorSettings(id="host-w4-worker-x", bearer_token="t")
    d = HfS3StreamDownloader(settings=settings,
                             client=make_fake_controller_client(handler))
```

`test_downloader_aborts_multipart_on_mid_stream_drop` — keep the `streaming_body` async generator and `handler` (lines 215-219); replace lines 221-228 (`transport = ...` through `monkeypatch.setattr`) with:

```python
    settings = ExecutorSettings(id="host-w4-worker-mid", bearer_token="t")
    d = HfS3StreamDownloader(settings=settings,
                             client=make_fake_controller_client(handler))
```

`test_downloader_handles_zero_byte_file` — keep `def handler` returning empty 200 (lines 254-255); replace lines 256-263 (`transport = ...` through `monkeypatch.setattr`) with:

```python
    settings = ExecutorSettings(id="host-w4-worker-zero", bearer_token="t")
    d = HfS3StreamDownloader(settings=settings,
                             client=make_fake_controller_client(handler))
```

`test_downloader_retries_transient_5xx` — keep the `call_count` closure + `handler` (lines 284-291); replace lines 293-298 (`settings = ...` through `monkeypatch.setattr`) with:

```python
    settings = ExecutorSettings(id="host-w4-worker-r", bearer_token="t")
    d = HfS3StreamDownloader(settings=settings,
                             client=make_fake_controller_client(handler))
```

After these edits, `monkeypatch` is no longer used by `test_downloader_404_fails_fast_no_multipart`, `test_downloader_aborts_multipart_on_mid_stream_drop`, `test_downloader_handles_zero_byte_file`, and `test_downloader_retries_transient_5xx` for the HF seam — but each still uses `monkeypatch` via the `s3_bucket` fixture (which calls `monkeypatch.setenv`), so the `monkeypatch` parameter stays in every signature. Leave the signatures unchanged.

- [ ] **Step 3: Run `test_downloader.py` to verify it fails**

Run: `uv run pytest tests/executor/test_downloader.py -v`
Expected: FAIL — `TypeError: HfS3StreamDownloader.__init__() got an unexpected keyword argument 'client'`.

- [ ] **Step 4: Rewire `HfS3StreamDownloader`**

In `src/dlw/executor/downloader.py`:

Replace the imports block (lines 12-32):

```python
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

import httpx

from dlw.executor._io import (
    _HTTP_CHUNK_BYTES,
    _TRANSIENT_RETRY,
    compose_key as _compose_key_io,
    make_http_client,
    make_s3_client,
    upload_part as _upload_part_io,
)
from dlw.executor.config import ExecutorSettings
from dlw.executor.types import Assignment, DownloadResult  # re-exported for callers
from dlw.schemas.storage import StorageConfig
```

with:

```python
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

from dlw.executor._io import (
    _HTTP_CHUNK_BYTES,
    _TRANSIENT_RETRY,
    compose_key as _compose_key_io,
    make_s3_client,
    upload_part as _upload_part_io,
)
from dlw.executor.client import ControllerClient
from dlw.executor.config import ExecutorSettings
from dlw.executor.types import Assignment, DownloadResult  # re-exported for callers
from dlw.schemas.storage import StorageConfig
```

(`import httpx` and `make_http_client` are removed — neither is used after the rewire.)

Replace the `__init__` + `_make_http_client` block (lines 41-52):

```python
    def __init__(self, *, settings: ExecutorSettings) -> None:
        self._s = settings

    def _compose_key(self, a: Assignment) -> str:
        return _compose_key_io(a)

    def _make_s3_client(self, cfg: StorageConfig) -> Any:
        return make_s3_client(self._s, cfg)

    def _make_http_client(self) -> httpx.AsyncClient:
        """Test seam — overridden in unit tests via monkeypatch."""
        return make_http_client(self._s)
```

with:

```python
    def __init__(self, *, settings: ExecutorSettings,
                 client: ControllerClient) -> None:
        self._s = settings
        self._controller = client

    def _compose_key(self, a: Assignment) -> str:
        return _compose_key_io(a)

    def _make_s3_client(self, cfg: StorageConfig) -> Any:
        return make_s3_client(self._s, cfg)
```

Replace the HF-fetch portion of `_download_once` (lines 61-83) — change:

```python
    async def _download_once(self, *, assignment: Assignment) -> DownloadResult:
        url = (f"{self._s.hf_endpoint.rstrip('/')}/{assignment.repo_id}"
               f"/resolve/{assignment.revision}/{assignment.filename}")
        s3 = self._make_s3_client(assignment.storage_config)
        bucket = assignment.storage_config.bucket
        key = self._compose_key(assignment)
        part_size = self._s.multipart_part_size_bytes

        headers: dict[str, str] = {}
        if self._s.hf_token:
            headers["Authorization"] = f"Bearer {self._s.hf_token}"

        upload_id: str | None = None
        sha = hashlib.sha256()
        bytes_total = 0
        parts: list[dict[str, Any]] = []
        buf = bytearray()
        part_no = 1

        try:
            async with self._make_http_client() as hc:
                async with hc.stream("GET", url, headers=headers) as resp:
                    resp.raise_for_status()
```

to:

```python
    async def _download_once(self, *, assignment: Assignment) -> DownloadResult:
        s3 = self._make_s3_client(assignment.storage_config)
        bucket = assignment.storage_config.bucket
        key = self._compose_key(assignment)
        part_size = self._s.multipart_part_size_bytes

        upload_id: str | None = None
        sha = hashlib.sha256()
        bytes_total = 0
        parts: list[dict[str, Any]] = []
        buf = bytearray()
        part_no = 1

        try:
            async with self._controller.stream_hf(
                subtask_id=assignment.subtask_id,
                assignment_token=assignment.assignment_token,
            ) as resp:
                resp.raise_for_status()
```

The rest of the method body (the `upload_id = await asyncio.to_thread(...)` line onward) stays exactly as-is. Note this removes one level of `async with` nesting (the old `async with self._make_http_client() as hc:` wrapper is gone) — re-indent the body that was under `async with hc.stream(...)` so it now sits directly under `async with self._controller.stream_hf(...)`. The indentation depth is unchanged because one `async with` replaced two.

- [ ] **Step 5: Run `test_downloader.py` to verify it passes**

Run: `uv run pytest tests/executor/test_downloader.py -v`
Expected: PASS (all 10 cases).

- [ ] **Step 6: Commit**

```bash
git add tests/conftest.py tests/executor/test_downloader.py src/dlw/executor/downloader.py
git commit -m "feat(executor): HfS3StreamDownloader fetches via controller proxy (W3b)"
```

---

### Task 6: Rewire `DirectOffsetDownloader` + `_resolve_size` range probe

**Files:**
- Modify: `tests/executor/test_chunk_downloader.py`
- Modify: `src/dlw/executor/chunk_downloader.py`

- [ ] **Step 1: Migrate + extend `tests/executor/test_chunk_downloader.py`**

Replace the second imports block (lines 43-56) — add the conftest import:

```python
import asyncio
import errno
import hashlib
import uuid
from typing import Any

import boto3
import httpx
from moto import mock_aws

from dlw.executor.chunk_downloader import DirectOffsetDownloader
from dlw.executor.config import ExecutorSettings
from dlw.executor.types import Assignment
from dlw.schemas.storage import StorageConfig
from tests.conftest import make_fake_controller_client
```

Replace the `_mock_transport_for_synthetic` helper (lines 65-78) with a handler-returning version:

```python
def _synthetic_hf_handler():
    """httpx handler: HTTP 206 Partial Content reading the Range header."""
    def handler(request: httpx.Request) -> httpx.Response:
        rng = request.headers.get("Range", "")
        assert rng.startswith("bytes="), f"unexpected Range header: {rng!r}"
        a, b = rng.removeprefix("bytes=").split("-")
        start, end = int(a), int(b)
        body = _SYNTHETIC[start:end + 1]
        return httpx.Response(
            status_code=206,
            content=body,
            headers={"Content-Length": str(len(body))},
        )
    return handler
```

Replace the `chunk_settings` fixture (lines 81-92) — drop the now-deleted `hf_endpoint` kwarg:

```python
@pytest.fixture
def chunk_settings(tmp_path) -> ExecutorSettings:
    return ExecutorSettings(
        id="ex-chunk-test",
        bearer_token="t",
        chunk_size_bytes=_CHUNK_SIZE,
        chunk_concurrency=2,
        parts_dir_path=str(tmp_path / "parts"),
        s3_region="us-east-1",
        s3_endpoint_url=None,
    )
```

In `test_pass1_pass2_happy_path_with_moto` — replace lines 96-104 (the `from dlw.executor import _io as _io_mod` ... through the `monkeypatch.setattr`) with nothing (delete them), and replace the `Assignment(...)` block (lines 112-121) + the `DirectOffsetDownloader(settings=chunk_settings)` line (127) so the function reads:

```python
async def test_pass1_pass2_happy_path_with_moto(chunk_settings) -> None:
    """Full pipeline: 4 chunks via the fake controller proxy -> moto multipart
    -> sha256 matches."""
    storage_config = StorageConfig(
        bucket="test-bucket", region="us-east-1", endpoint_url=None,
        access_key_id="dummy", secret_access_key="dummy",
        key_prefix="phase1",
    )

    sub_id = uuid.uuid4()
    a = Assignment(
        subtask_id=sub_id,
        task_id=uuid.uuid4(),
        assignment_token=uuid.uuid4(),
        repo_id="owner/repo",
        revision="b" * 40,
        filename="model.bin",
        file_size=_FILE_SIZE,
        expected_sha256=None,
        storage_config=storage_config,
    )

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-bucket")

        d = DirectOffsetDownloader(
            settings=chunk_settings,
            client=make_fake_controller_client(_synthetic_hf_handler()),
        )
        result = await d.download(assignment=a)

    assert result.bytes_written == _FILE_SIZE
    assert result.actual_sha256 == _EXPECTED_SHA
    assert result.s3_key.endswith("model.bin")

    from dlw.executor.parts_dir import parts_dir_for
    assert not parts_dir_for(chunk_settings.parts_dir_path, sub_id).exists()
```

(Note: the `monkeypatch` parameter is dropped from this test's signature — it no longer monkeypatches anything.)

In `test_pass1_enospc_raises_disk_full_and_leaks_parts` — keep the `_NoSpaceWriter` + the `monkeypatch.setattr(cd_mod.DirectOffsetDownloader, "_open_writer", ...)` (that monkeypatch stays — it injects the ENOSPC writer). Replace the HF-transport monkeypatch (lines 144-149, `transport = _mock_transport_for_synthetic()` through the `monkeypatch.setattr(_io_mod, "make_http_client", ...)`) with nothing (delete them); also delete the now-unused `from dlw.executor import _io as _io_mod` import on line 143. Then replace the `Assignment(...)` block (lines 169-178) and the `DirectOffsetDownloader(settings=chunk_settings)` line (180) so the function reads:

```python
async def test_pass1_enospc_raises_disk_full_and_leaks_parts(
    chunk_settings, monkeypatch,
) -> None:
    """Inject ENOSPC into pass-1 chunk write -> DiskFullError + parts NOT cleaned."""
    from dlw.executor import chunk_downloader as cd_mod

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
        assignment_token=uuid.uuid4(),
        repo_id="owner/repo",
        revision="b" * 40,
        filename="model.bin",
        file_size=_FILE_SIZE,
        expected_sha256=None,
        storage_config=storage_config,
    )

    d = DirectOffsetDownloader(
        settings=chunk_settings,
        client=make_fake_controller_client(_synthetic_hf_handler()),
    )
    from dlw.executor.chunk_downloader import DiskFullError
    with pytest.raises(DiskFullError):
        await d.download(assignment=a)

    from dlw.executor.parts_dir import parts_dir_for
    assert parts_dir_for(chunk_settings.parts_dir_path, sub_id).exists()
```

Append two new `_resolve_size` tests at the end of the file:

```python
async def test_resolve_size_via_range_probe(chunk_settings) -> None:
    """When file_size is None, _resolve_size does a bytes=0-0 probe and reads
    Content-Range to recover the total size."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Range") == "bytes=0-0"
        return httpx.Response(
            206, content=b"\x00",
            headers={"Content-Range": "bytes 0-0/123456", "Content-Length": "1"},
        )

    d = DirectOffsetDownloader(
        settings=chunk_settings,
        client=make_fake_controller_client(handler),
    )
    a = Assignment(
        subtask_id=uuid.uuid4(), task_id=uuid.uuid4(),
        assignment_token=uuid.uuid4(),
        repo_id="o/r", revision="b" * 40, filename="big.bin",
        file_size=None, expected_sha256=None,
        storage_config=StorageConfig(
            bucket="b", region="us-east-1", endpoint_url=None,
            key_prefix="p",
        ),
    )
    resolved = await d._resolve_size(a)
    assert resolved.file_size == 123456


async def test_resolve_size_falls_back_to_content_length(chunk_settings) -> None:
    """If HF answers a 200 (no Content-Range), _resolve_size uses Content-Length."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"\x00" * 10,
            headers={"Content-Length": "777"},
        )

    d = DirectOffsetDownloader(
        settings=chunk_settings,
        client=make_fake_controller_client(handler),
    )
    a = Assignment(
        subtask_id=uuid.uuid4(), task_id=uuid.uuid4(),
        assignment_token=uuid.uuid4(),
        repo_id="o/r", revision="b" * 40, filename="big.bin",
        file_size=None, expected_sha256=None,
        storage_config=StorageConfig(
            bucket="b", region="us-east-1", endpoint_url=None,
            key_prefix="p",
        ),
    )
    resolved = await d._resolve_size(a)
    assert resolved.file_size == 777


async def test_resolve_size_raises_when_no_headers(chunk_settings) -> None:
    """If neither Content-Range nor Content-Length is present, raise RuntimeError."""
    def handler(request: httpx.Request) -> httpx.Response:
        # Use stream= to prevent httpx from auto-injecting Content-Length.
        return httpx.Response(200, stream=httpx.ByteStream(b"\x00"))  # no size headers

    d = DirectOffsetDownloader(
        settings=chunk_settings,
        client=make_fake_controller_client(handler),
    )
    a = Assignment(
        subtask_id=uuid.uuid4(), task_id=uuid.uuid4(),
        assignment_token=uuid.uuid4(),
        repo_id="o/r", revision="b" * 40, filename="big.bin",
        file_size=None, expected_sha256=None,
        storage_config=StorageConfig(
            bucket="b", region="us-east-1", endpoint_url=None,
            key_prefix="p",
        ),
    )
    with pytest.raises(RuntimeError, match="unresolvable"):
        await d._resolve_size(a)
```

- [ ] **Step 2: Run `test_chunk_downloader.py` to verify it fails**

Run: `uv run pytest tests/executor/test_chunk_downloader.py -v`
Expected: FAIL — `TypeError: DirectOffsetDownloader.__init__() got an unexpected keyword argument 'client'`.

- [ ] **Step 3: Rewire `DirectOffsetDownloader`**

In `src/dlw/executor/chunk_downloader.py`:

Replace the imports block (lines 6-30):

```python
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

from dlw.executor import _io as _io_mod
from dlw.executor._io import (
    _HTTP_CHUNK_BYTES,
    _TRANSIENT_RETRY,
    compose_key,
    make_s3_client,
    upload_part,
)
from dlw.executor.config import ExecutorSettings
from dlw.executor.parts_dir import cleanup_parts_dir, parts_dir_for
from dlw.executor.types import Assignment, DownloadResult
```

with:

```python
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

from dlw.executor._io import (
    _HTTP_CHUNK_BYTES,
    _TRANSIENT_RETRY,
    compose_key,
    make_s3_client,
    upload_part,
)
from dlw.executor.client import ControllerClient
from dlw.executor.config import ExecutorSettings
from dlw.executor.parts_dir import cleanup_parts_dir, parts_dir_for
from dlw.executor.types import Assignment, DownloadResult
```

(`import httpx` and `from dlw.executor import _io as _io_mod` are removed — neither is used after the rewire.)

Replace `__init__` (lines 60-63):

```python
    def __init__(self, *, settings: ExecutorSettings) -> None:
        self._s = settings
        assert settings.chunk_size_bytes >= 5 * 1024 * 1024, \
            f"chunk_size_bytes ({settings.chunk_size_bytes}) < 5 MiB"
```

with:

```python
    def __init__(self, *, settings: ExecutorSettings,
                 client: ControllerClient) -> None:
        self._s = settings
        self._controller = client
        assert settings.chunk_size_bytes >= 5 * 1024 * 1024, \
            f"chunk_size_bytes ({settings.chunk_size_bytes}) < 5 MiB"
```

Replace `_resolve_size` (lines 70-82):

```python
    async def _resolve_size(self, a: Assignment) -> Assignment:
        url = (f"{self._s.hf_endpoint.rstrip('/')}/{a.repo_id}"
               f"/resolve/{a.revision}/{a.filename}")
        headers: dict[str, str] = {}
        if self._s.hf_token:
            headers["Authorization"] = f"Bearer {self._s.hf_token}"
        async with _io_mod.make_http_client(self._s) as hc:
            resp = await hc.head(url, headers=headers)
            resp.raise_for_status()
        cl = resp.headers.get("Content-Length")
        if cl is None:
            raise RuntimeError("file_size unresolvable: HEAD returned no Content-Length")
        return dataclasses.replace(a, file_size=int(cl))
```

with:

```python
    async def _resolve_size(self, a: Assignment) -> Assignment:
        """W3b: the proxy is GET-only, so probe size with a bytes=0-0 range
        request and read Content-Range (`bytes 0-0/<total>`). Fall back to
        Content-Length if HF answered a full 200 instead of a 206."""
        async with self._controller.stream_hf(
            subtask_id=a.subtask_id,
            assignment_token=a.assignment_token,
            range_header="bytes=0-0",
        ) as resp:
            resp.raise_for_status()
            content_range = resp.headers.get("Content-Range")
            content_length = resp.headers.get("Content-Length")
        if content_range and "/" in content_range:
            total = content_range.rsplit("/", 1)[1].strip()
            if total.isdigit():
                return dataclasses.replace(a, file_size=int(total))
        # Content-Length fallback: only correct when HF answered a full 200
        # (then it is the total size). A well-behaved 206 always carries
        # Content-Range, handled above.
        if content_length is None:
            raise RuntimeError(
                "file_size unresolvable: no Content-Range or Content-Length"
            )
        return dataclasses.replace(a, file_size=int(content_length))
```

Replace `_pass1_parallel` (lines 101-109):

```python
    async def _pass1_parallel(
        self, a: Assignment, plans: list[ChunkPlan], dest_dir: Path,
    ) -> None:
        sem = asyncio.Semaphore(self._s.chunk_concurrency)
        async with _io_mod.make_http_client(self._s) as hc:
            async def one(plan: ChunkPlan) -> None:
                async with sem:
                    await self._download_one_chunk(hc, a, plan, dest_dir)
            await asyncio.gather(*(one(p) for p in plans))
```

with:

```python
    async def _pass1_parallel(
        self, a: Assignment, plans: list[ChunkPlan], dest_dir: Path,
    ) -> None:
        sem = asyncio.Semaphore(self._s.chunk_concurrency)

        async def one(plan: ChunkPlan) -> None:
            async with sem:
                await self._download_one_chunk(a, plan, dest_dir)

        await asyncio.gather(*(one(p) for p in plans))
```

Replace `_download_one_chunk` (lines 111-136):

```python
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
                raise
```

with:

```python
    @_TRANSIENT_RETRY
    async def _download_one_chunk(
        self, a: Assignment, plan: ChunkPlan, dest_dir: Path,
    ) -> None:
        range_header = f"bytes={plan.offset}-{plan.offset + plan.length - 1}"
        async with self._controller.stream_hf(
            subtask_id=a.subtask_id,
            assignment_token=a.assignment_token,
            range_header=range_header,
        ) as resp:
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
                raise
```

- [ ] **Step 4: Run `test_chunk_downloader.py` to verify it passes**

Run: `uv run pytest tests/executor/test_chunk_downloader.py -v`
Expected: PASS (all cases — the 4 `plan_chunks`/`DiskFullError` unit tests, the 2 migrated pipeline tests, and the 2 new `_resolve_size` tests).

- [ ] **Step 5: Commit**

```bash
git add tests/executor/test_chunk_downloader.py src/dlw/executor/chunk_downloader.py
git commit -m "feat(executor): DirectOffsetDownloader fetches via controller proxy (W3b)"
```

---

### Task 7: Thread `assignment_token` through the runner, wire the CLI, delete executor HF config

**Files:**
- Modify: `src/dlw/executor/runner.py:203-212`
- Modify: `src/dlw/executor/cli.py:38-47`
- Modify: `src/dlw/executor/config.py:35-37`
- Modify: `src/dlw/executor/_io.py:54-59`
- Modify: `tests/executor/test_config.py` — two pre-existing tests (`test_w4_defaults`, `test_w4_env_overrides`) assert on the now-deleted `hf_endpoint`/`hf_token` fields; drop those HF-specific `setenv`/`assert` lines (keep the S3 field coverage).
- Modify: `tests/executor/test_runner.py` — in `test_runner_passes_assignment_with_repo_and_storage`, add `assert isinstance(a.assignment_token, uuid.UUID)` to directly verify the runner threads `assignment_token` into the `Assignment` it builds.

- [ ] **Step 1: Thread `assignment_token` into the `Assignment` the runner builds**

In `src/dlw/executor/runner.py`, in `_execute_subtask`, change the `Assignment(...)` construction (lines 203-212) — add `assignment_token`:

```python
            assignment = Assignment(
                subtask_id=sub_id,
                task_id=uuid.UUID(subtask["task_id"]),
                assignment_token=assignment_token,
                repo_id=repo_id,
                revision=revision,
                filename=subtask["filename"],
                file_size=subtask.get("file_size"),
                expected_sha256=subtask.get("expected_sha256"),
                storage_config=StorageConfig(**storage_config),
            )
```

(`assignment_token` is already a parameter of `_execute_subtask` — no signature change needed.)

- [ ] **Step 2: Wire the CLI to pass the client into both downloaders**

In `src/dlw/executor/cli.py`, change `_async_main` (lines 38-47):

```python
    settings = ExecutorSettings()
    # W3a: the client starts without an AuthState; ExecutorRunner.run() does
    # load_or_register and then calls client.update_auth() before any request.
    client = ControllerClient(base_url=settings.controller_url)
    stream = HfS3StreamDownloader(settings=settings)
    chunk = DirectOffsetDownloader(settings=settings)
    runner = ExecutorRunner(
        settings=settings, client=client,
        stream_downloader=stream, chunk_downloader=chunk,
    )
```

to:

```python
    settings = ExecutorSettings()
    # W3a: the client starts without an AuthState; ExecutorRunner.run() does
    # load_or_register and then calls client.update_auth() before any request.
    # W3b: both downloaders fetch HF bytes through the same client's reverse
    # proxy (stream_hf) — the executor never holds the HF token.
    client = ControllerClient(base_url=settings.controller_url)
    stream = HfS3StreamDownloader(settings=settings, client=client)
    chunk = DirectOffsetDownloader(settings=settings, client=client)
    runner = ExecutorRunner(
        settings=settings, client=client,
        stream_downloader=stream, chunk_downloader=chunk,
    )
```

- [ ] **Step 3: Delete the HF fields from `ExecutorSettings`**

In `src/dlw/executor/config.py`, delete the W4 HF Hub block (lines 35-37):

```python
    # Phase 1 W4 — HF Hub
    hf_endpoint: str = Field(default="https://huggingface.co")
    hf_token: str | None = Field(default=None)
```

(Leave the `# Phase 1 W4 — S3 / S3-compatible` block and everything else intact.)

- [ ] **Step 4: Delete the now-unused `make_http_client` from `_io.py`**

In `src/dlw/executor/_io.py`, delete the `make_http_client` function (lines 54-59):

```python
def make_http_client(settings: ExecutorSettings) -> httpx.AsyncClient:
    """Test seam — overridable via monkeypatch."""
    return httpx.AsyncClient(
        timeout=settings.download_timeout_seconds,
        follow_redirects=True,
    )
```

Leave `import httpx` in `_io.py` — it is still used by `_is_transient_http`.

- [ ] **Step 5: Verify no remaining `Assignment(` / downloader construction sites were missed**

Run: `git grep -n "Assignment(" -- src/ ; git grep -n "HfS3StreamDownloader(\|DirectOffsetDownloader(\|make_http_client\|_make_http_client" -- src/`
Expected: the only `Assignment(` in `src/` is `src/dlw/executor/runner.py` (now updated); no `make_http_client` / `_make_http_client` references remain anywhere in `src/`; the only downloader constructions are in `src/dlw/executor/cli.py` (now updated).

- [ ] **Step 6: Run the executor + e2e-affected suites to verify nothing regressed**

Run: `uv run pytest tests/executor/ -v`
Expected: PASS — `test_runner.py` is unaffected (it builds `Assignment` only via the real runner, which now supplies `assignment_token`; its downloaders are `MagicMock`s). `test_client.py`, `test_downloader.py`, `test_chunk_downloader.py` all pass.

- [ ] **Step 7: Commit**

```bash
git add src/dlw/executor/runner.py src/dlw/executor/cli.py src/dlw/executor/config.py src/dlw/executor/_io.py
git commit -m "feat(executor): wire proxy client end-to-end, delete ExecutorSettings HF fields (W3b)"
```

---

## Milestone 3 — Integration: lint, e2e, OpenAPI, docs, PR

### Task 8: `check_no_hf_token_in_executor` invariant lint

**Files:**
- Modify: `tools/lint_invariants.py`
- Create: `tests/tools/__init__.py`, `tests/tools/test_lint_no_hf_token.py`

- [ ] **Step 1: Write the failing self-test**

Create `tests/tools/__init__.py` (empty file).

Create `tests/tools/test_lint_no_hf_token.py`:

```python
"""Self-test for tools/lint_invariants.py::check_no_hf_token_in_executor (W3b)."""
from __future__ import annotations

import functools
import importlib.util
from pathlib import Path

_LINT_PATH = Path(__file__).resolve().parents[2] / "tools" / "lint_invariants.py"


@functools.lru_cache(maxsize=None)
def _load_lint():
    spec = importlib.util.spec_from_file_location("lint_invariants", _LINT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_lint_flags_hf_token_in_executor(tmp_path, monkeypatch) -> None:
    lint = _load_lint()
    exec_dir = tmp_path / "src" / "dlw" / "executor"
    exec_dir.mkdir(parents=True)
    (exec_dir / "bad.py").write_text(
        "hf_token = 'leaked-secret'\n", encoding="utf-8",
    )
    monkeypatch.setattr(lint, "ROOT", tmp_path)
    errors = lint.check_no_hf_token_in_executor()
    assert any("bad.py" in e for e in errors), errors


def test_lint_passes_clean_executor(tmp_path, monkeypatch) -> None:
    lint = _load_lint()
    exec_dir = tmp_path / "src" / "dlw" / "executor"
    exec_dir.mkdir(parents=True)
    (exec_dir / "ok.py").write_text(
        "# hf_token mentioned in a comment is fine\nx = 1\n", encoding="utf-8",
    )
    monkeypatch.setattr(lint, "ROOT", tmp_path)
    assert lint.check_no_hf_token_in_executor() == []


def test_lint_flags_hf_token_in_string_literal(tmp_path, monkeypatch) -> None:
    """The scan is full-text by design — a string literal mentioning the
    forbidden identifier in executor code is flagged too (only whole
    comment lines are exempt)."""
    lint = _load_lint()
    exec_dir = tmp_path / "src" / "dlw" / "executor"
    exec_dir.mkdir(parents=True)
    (exec_dir / "guard.py").write_text(
        'raise ValueError("hf_token must not reach executor")\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(lint, "ROOT", tmp_path)
    assert lint.check_no_hf_token_in_executor() != []
```

- [ ] **Step 2: Run the self-test to verify it fails**

Run: `uv run pytest tests/tools/test_lint_no_hf_token.py -v`
Expected: FAIL — `AttributeError: module 'lint_invariants' has no attribute 'check_no_hf_token_in_executor'`.

- [ ] **Step 3: Add the lint check**

In `tools/lint_invariants.py`, add this function right after `check_no_bearer_on_executor_routes` (after line 225, before `def main()`):

```python
def check_no_hf_token_in_executor() -> list[str]:
    """W3b §3.11: INVARIANT 2 — the tenant HF token must never reach an
    executor. Forbid the `hf_token` / `hf_endpoint` identifiers anywhere in
    src/dlw/executor/. After W3b, HF access goes exclusively through the
    controller's reverse proxy.

    Only whole comment lines (first non-whitespace char is `#`) are exempt —
    string literals and trailing comments containing the identifiers are
    flagged too (this is a full-text scan, by design)."""
    errors: list[str] = []
    exec_dir = ROOT / "src" / "dlw" / "executor"
    if not exec_dir.exists():
        return []
    for py in sorted(exec_dir.rglob("*.py")):  # sorted for deterministic CI output
        try:
            text = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            errors.append(f"Cannot read {py.relative_to(ROOT)}: {e}")
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if line.strip().startswith("#"):
                continue
            if "hf_token" in line or "hf_endpoint" in line:
                errors.append(
                    f"{py.relative_to(ROOT)}:{lineno}: "
                    f"'hf_token'/'hf_endpoint' forbidden in the executor package "
                    f"(INVARIANT 2 — HF access goes through the controller proxy)"
                )
    return errors
```

Then in `main()`, add it to the `failures.extend(...)` block — change:

```python
    failures.extend(check_executor_status_domain())
    failures.extend(check_subtask_status_domain())
    failures.extend(check_task_status_domain())
    failures.extend(check_d10_host_affinity_test_owner())
    failures.extend(check_no_bearer_on_executor_routes())
```

to:

```python
    failures.extend(check_executor_status_domain())
    failures.extend(check_subtask_status_domain())
    failures.extend(check_task_status_domain())
    failures.extend(check_d10_host_affinity_test_owner())
    failures.extend(check_no_bearer_on_executor_routes())
    failures.extend(check_no_hf_token_in_executor())
```

- [ ] **Step 4: Run the self-test to verify it passes**

Run: `uv run pytest tests/tools/test_lint_no_hf_token.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the real lint against the production tree**

Run: `uv run python tools/lint_invariants.py`
Expected: exit code 0, prints `OK: ...` — the new check finds zero `hf_token`/`hf_endpoint` references in `src/dlw/executor/` because Tasks 5/6/7 removed them all. If it reports a violation, fix the offending line in the executor package before continuing.

- [ ] **Step 6: Commit**

```bash
git add tools/lint_invariants.py tests/tools/__init__.py tests/tools/test_lint_no_hf_token.py
git commit -m "feat(lint): check_no_hf_token_in_executor — lock INVARIANT 2 (W3b)"
```

---

### Task 9: e2e rewire, OpenAPI, operator runbook, PR

**Files:**
- Modify: `tests/e2e/test_executor_e2e.py:181-187`
- Modify: `api/openapi.yaml`
- Modify: `docs/operator/executor-runbook.md`

- [ ] **Step 1: Rewire the e2e — HF MockTransport moves to the controller side**

In `tests/e2e/test_executor_e2e.py`, the executor downloader currently injects the HF transport directly into the downloader's `_make_http_client`. After W3b, the executor fetches through the controller proxy, so the HF MockTransport must be installed on the **controller's** HF client factory instead.

Replace this block (lines 181-187):

```python
            downloader = HfS3StreamDownloader(settings=settings)
            # Inject the HF transport into the downloader's http client factory
            downloader._make_http_client = lambda: httpx.AsyncClient(
                transport=hf_transport,
                timeout=settings.download_timeout_seconds,
                follow_redirects=True,
            )
```

with:

```python
            downloader = HfS3StreamDownloader(
                settings=settings, client=executor_client,
            )
            # W3b: the executor fetches HF bytes through the controller's
            # reverse proxy. Install the HF MockTransport on the controller's
            # HF client factory (not the downloader).
            import dlw.api.hf_proxy as _hf_proxy_mod
            monkeypatch.setattr(
                _hf_proxy_mod, "_make_hf_client",
                lambda timeout_seconds: httpx.AsyncClient(
                    transport=hf_transport, follow_redirects=True,
                ),
            )
```

(`executor_client` is the `ControllerClient` already constructed earlier in the test with `_transport=asgi_transport` + the `X-Client-Cert-PEM` bypass header — `stream_hf` rides on it straight to the ASGI controller. `monkeypatch` is already a parameter of `test_e2e_hf_to_s3_full_pipeline`.)

Also update the module docstring at the top of `tests/e2e/test_executor_e2e.py` so it reflects the W3b seam migration (the HF MockTransport now sits on the controller's `dlw.api.hf_proxy._make_hf_client` seam, not the executor downloader):

```python
"""E2E: real controller + real ExecutorRunner — full HF→S3 happy path.

W4 rewrite: replaces MockDownloader with HfS3StreamDownloader.
W3b update: the HF MockTransport is installed on the controller's
`dlw.api.hf_proxy._make_hf_client` seam — the executor fetches through the
controller reverse-proxy, not directly from HF. S3 served by moto[s3]
in-process. No Docker required.
"""
```

- [ ] **Step 2: Run the e2e to verify it passes**

Run: `uv run pytest tests/e2e/test_executor_e2e.py -v`
Expected: PASS — the runner downloads both files (`config.json`, `model.safetensors`) HF→controller-proxy→executor→S3, the task ends `succeeded`, both objects land in moto S3.

- [ ] **Step 3: Document the endpoint in OpenAPI**

In `api/openapi.yaml`, add the `/hf-proxy/subtask/{subtaskId}` path under `paths:` (match the file's existing indentation and `tags`/security style — model it on the existing executor routes like `/executors/{executorId}/poll`):

```yaml
  /api/v1/hf-proxy/subtask/{subtaskId}:
    get:
      tags: [executors]
      summary: HF reverse proxy — stream a subtask's file from HF
      description: >-
        Controller-side reverse proxy. Authenticates the executor (mTLS + JWT),
        verifies subtask ownership (assignment_token + epoch fence), reconstructs
        the HF URL from the subtask row, injects the tenant HF token, and streams
        the bytes back. The executor never holds the HF token (INVARIANT 2).
      operationId: hfProxySubtask
      security:
        - executorMtls: []
          executorJwt: []
      parameters:
        - name: subtaskId
          in: path
          required: true
          schema:
            type: string
            format: uuid
        - name: X-Assignment-Token
          in: header
          required: true
          schema:
            type: string
            format: uuid
        - name: Range
          in: header
          required: false
          schema:
            type: string
      responses:
        '200':
          description: Full file stream.
          content:
            application/octet-stream:
              schema:
                type: string
                format: binary
        '206':
          description: Partial content (Range request).
          content:
            application/octet-stream:
              schema:
                type: string
                format: binary
        '401':
          description: Missing or invalid mTLS / executor JWT.
        '403':
          description: NOT_YOUR_SUBTASK — authenticated executor does not own this subtask.
        '404':
          description: Subtask not found.
        '409':
          description: STALE_ASSIGNMENT or EPOCH_MISMATCH (fence-token mismatch).
        '429':
          description: Forwarded from HF — rate limited.
        '503':
          description: Forwarded from HF — upstream unavailable.
```

If `api/openapi.yaml` does not already define `executorMtls` / `executorJwt` security schemes, reuse whatever scheme names the existing W3a executor routes (`/executors/{executorId}/poll`, `/executors/{executorId}/heartbeat`) use — copy their `security:` block verbatim rather than inventing new scheme names.

- [ ] **Step 4: Verify the OpenAPI spec lints clean**

Run: `npx --yes @stoplight/spectral-cli lint api/openapi.yaml`
Expected: no errors (warnings about descriptions/examples are acceptable if the rest of the file already has them; there must be zero new **errors**). If spectral is configured via a project ruleset, the CI command in `.github/workflows/` is authoritative — run that exact command instead.

- [ ] **Step 5: Update the operator runbook**

In `docs/operator/executor-runbook.md`, add a short subsection noting the W3b change. Place it near the existing executor-configuration / environment-variable section:

```markdown
## W3b — HF access via the controller reverse proxy

As of Phase 2 W3b, executors no longer talk to huggingface.co directly. All
HF file downloads flow through the controller's reverse proxy
(`GET /api/v1/hf-proxy/subtask/{id}`), which injects the tenant HF token
server-side (INVARIANT 2 — the token never leaves the controller).

**Removed executor environment variables** (delete them from `.env.executor`
and any deployment manifests — they are now ignored):

- `DLW_EXECUTOR_HF_TOKEN`
- `DLW_EXECUTOR_HF_ENDPOINT`

**Controller environment variables:**

- `DLW_HF_TOKEN` — the tenant HF token (already used by the controller for
  repo-metadata enumeration; W3b also uses it for the download proxy).
- `DLW_HF_ENDPOINT` — defaults to `https://huggingface.co`.
- `DLW_HF_PROXY_TIMEOUT_SECONDS` — per-request timeout for the proxy's HF
  fetch (default 300, range 10–3600).

**Operational tradeoff:** download bandwidth now flows through the controller
rather than executor→HF directly. For the internal beta this is acceptable;
global rate-limit coordination and an executor-local credential pool are
Phase 3 items.
```

- [ ] **Step 6: Run the full test suite + all lints**

Run: `uv run pytest -q`
Expected: PASS — entire suite green.

Run: `uv run python tools/lint_invariants.py`
Expected: exit 0, `OK: ...`.

- [ ] **Step 7: Commit**

```bash
git add tests/e2e/test_executor_e2e.py api/openapi.yaml docs/operator/executor-runbook.md
git commit -m "test(e2e)+docs: e2e through controller proxy, OpenAPI + operator runbook (W3b)"
```

- [ ] **Step 8: Push and open the PR**

```bash
git push -u origin feat/phase-2-w3b-hf-reverse-proxy
gh pr create --title "Phase 2 W3b — HF reverse proxy (SEC-02 / INVARIANT 2)" --body "$(cat <<'EOF'
## Summary
- New controller-side `GET /api/v1/hf-proxy/subtask/{id}` reverse proxy: mTLS+JWT auth, ownership + assignment_token + epoch fence verification, HF URL reconstruction, tenant-token injection, streaming passthrough with a header allowlist.
- Executor downloaders (`HfS3StreamDownloader`, `DirectOffsetDownloader`) now fetch HF bytes through `ControllerClient.stream_hf` instead of calling huggingface.co directly; `ExecutorSettings.hf_token`/`hf_endpoint` deleted.
- New `check_no_hf_token_in_executor` invariant lint locks INVARIANT 2 for the executor package. Zero schema changes / no alembic migration.

## Test plan
- [ ] `uv run pytest tests/api/test_hf_proxy.py -v` — 9 proxy cases (streaming, token injection, URL reconstruction, Range, 429, 401, 404, 403 NOT_YOUR_SUBTASK, 409 STALE_ASSIGNMENT / EPOCH_MISMATCH)
- [ ] `uv run pytest tests/executor/ -v` — client `stream_hf`, both downloaders rewired, `_resolve_size` range probe, runner unaffected
- [ ] `uv run pytest tests/e2e/test_executor_e2e.py -v` — full HF→controller-proxy→executor→S3 path
- [ ] `uv run python tools/lint_invariants.py` — `check_no_hf_token_in_executor` passes on the production tree
- [ ] `uv run pytest -q` — full suite green

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

**1. Spec coverage** — every spec section maps to a task:
- Spec §1 Goal (close SEC-02 for the download path) → Tasks 2, 3, 5, 6, 7 collectively.
- Spec §3.1 `hf_proxy.py` router + `_make_hf_client` seam → Task 2 (+ verification chain Task 3).
- Spec §3.2 verification chain (4 checks) → Task 2 (404) + Task 3 (403/409/409).
- Spec §3.3 `main.py` router include → Task 2 Step 4.
- Spec §3.4 `Settings.hf_proxy_timeout_seconds` → Task 1.
- Spec §3.5 `ControllerClient.stream_hf` → Task 4.
- Spec §3.6 `Assignment.assignment_token` → Task 4.
- Spec §3.7 `HfS3StreamDownloader` rewire → Task 5.
- Spec §3.8 `DirectOffsetDownloader` rewire + `_resolve_size` probe → Task 6.
- Spec §3.9 runner + cli wiring → Task 7 Steps 1-2.
- Spec §3.10 delete `ExecutorSettings` HF fields → Task 7 Step 3.
- Spec §3.11 `check_no_hf_token_in_executor` lint → Task 8.
- Spec §4 zero schema changes → no alembic task exists (correct — nothing to add).
- Spec §5 wire format / config surface → Tasks 1, 2, 4, 7 + OpenAPI Task 9 Step 3.
- Spec §6 error matrix → covered by Task 2/3 tests + the downloaders' existing fail-closed behaviour (unchanged).
- Spec §7 testing strategy → Tasks 2, 3, 4, 6, 8 (new cases) + Tasks 5, 6, 9 (migrations). `make_fake_controller_client` conftest helper → Task 5 Step 1.
- Spec §8 acceptance criteria → all line items map to a task above.
- Spec §9 phasing (M1/M2/M3) → the three milestones here.
- `_io.py` `make_http_client` removal (spec §3.7 "verify during implementation") → Task 7 Step 4.

**2. Placeholder scan** — no "TBD"/"TODO"/"handle edge cases"/"similar to Task N". Every code step shows complete code or a precise before/after block. The one judgement call left to the implementer (Task 9 Step 3: matching existing OpenAPI security-scheme names) is explicit and bounded — it says copy the W3a executor routes' block verbatim.

**3. Type consistency** — `Assignment` field is `assignment_token: uuid.UUID` everywhere (types.py, runner, all test constructions, both downloaders read `assignment.assignment_token`). `ControllerClient.stream_hf(*, subtask_id, assignment_token, range_header=None)` — the signature in Task 4 matches every call site: `HfS3StreamDownloader._download_once` (Task 5, no `range_header`), `DirectOffsetDownloader._download_one_chunk` (Task 6, `range_header=`), `DirectOffsetDownloader._resolve_size` (Task 6, `range_header="bytes=0-0"`), `make_fake_controller_client._FakeControllerClient.stream_hf` (Task 5, same kwargs). The proxy's `_make_hf_client(timeout_seconds: int)` seam — monkeypatched consistently in `tests/api/test_hf_proxy.py` (Task 2), `tests/e2e/test_executor_e2e.py` (Task 9) with the matching one-arg signature. Both downloader `__init__` signatures are `(*, settings, client)` — matching the `cli.py` wiring (Task 7) and all test constructions (Tasks 5, 6).
