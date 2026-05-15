# Phase 2 Week 3b — HF Reverse Proxy Design

> **Status:** Draft (brainstormed 2026-05-14).
> **Companion plan:** `docs/superpowers/plans/2026-05-14-phase-2-w3b-hf-reverse-proxy.md` (to be written by writing-plans skill after spec approval).
> **Roadmap source:** `docs/v2.0/08-mvp-roadmap.md` §2.6 — Phase 2 Week 3 Day 4 (HF reverse-proxy).
> **Companion split (W3a / W3c):** W3a (mTLS + JWT + HMAC) merged — PR #12. W3c (active/standby + chaos drill) is a separate spec/plan.
> **Security source:** `docs/v2.0/04-security-and-tenancy.md` §3.1 (HF Token reverse proxy, SEC-02) + INVARIANT 2.

---

## 1. Goal & Non-Goals

### 1.1 Goal

Close SEC-02 / INVARIANT 2 for the executor download path: the tenant-level HF token must never reach an executor. W3b adds a controller-side reverse proxy — `GET /api/v1/hf-proxy/subtask/{subtask_id}` — that:

1. Authenticates the executor via the W3a mTLS → JWT dependency chain.
2. Verifies the executor actually owns the subtask: the `X-Assignment-Token` header matches `subtask.assignment_token` (W1 fence), `subtask.executor_id` matches the authenticated executor (confused-deputy guard), and `subtask.executor_epoch` matches the authenticated executor's epoch (W1 fence).
3. Reconstructs the HF URL from the subtask row (`subtask → DownloadTask.repo_id + .revision`, `subtask.filename`) — the executor never supplies a path, eliminating arbitrary-path SSRF surface.
4. Injects the controller's HF token (`Settings.hf_token`), follows HF's `302 → CDN` redirect server-side, and streams HF's response straight back as a `StreamingResponse`.

The executor's two downloaders (`HfS3StreamDownloader`, `DirectOffsetDownloader`) stop calling `huggingface.co` directly — they fetch through a new `ControllerClient.stream_hf` context manager. `ExecutorSettings.hf_token` and `ExecutorSettings.hf_endpoint` are deleted.

After W3b, the only place a tenant HF token exists is the controller process (`Settings.hf_token` env). Executors hold only their mTLS cert + JWT; they reach HF exclusively through the authenticated proxy.

### 1.2 Non-goals (deferred — explicit list)

| Item | Where |
|---|---|
| `tenant_secrets` table / per-tenant HF token | **Phase 3** multi-tenant — Phase 2 single-tenant uses the controller `Settings.hf_token` env |
| Envelope encryption (AES-GCM DEK + KMS KEK) of the HF token | **Phase 3** |
| Global 429/5xx throttle coordination (`04 §3.1`'s "Controller 全局协调") + `source_throttle_state` | **Phase 3** — same split as W2b2. W3b passes 429/503 straight through; the executor's W2b2 `paused_external` handling catches them |
| `X-Repo-Commit` / commit-pin verification (`03 §10`) | **Phase 3** — revisions are already 40-char SHAs, so HF force-push conflicts can't happen |
| Executor-local OOB HF credential pool (`14 §3` — the documented INVARIANT-2 exception for bypassing per-user rate limits) | **v2.1** |
| Proxy-side caching / range coalescing / connection pooling | not needed for internal beta — per-request httpx client is correct + simple |
| HTTP/2 to HF (`httpx.AsyncClient(http2=True)` from `04 §3.1`) | optional polish — W3b uses HTTP/1.1 streaming; HTTP/2 is a Phase-3 perf tweak |
| `hf_metadata` (the controller's `/tasks` repo-file listing) | **unchanged** — it already runs controller-side; `Settings.hf_token` never leaves the controller process. W3b does not touch it |
| Active/standby controller + chaos drill | **W3c** |

---

## 2. Tech Stack Additions

**None.** `httpx` (already pinned) provides `AsyncClient(follow_redirects=True)` + streaming; `fastapi.responses.StreamingResponse` is stdlib-FastAPI. No new runtime deps, no new dev deps, no new CI jobs, **zero alembic migrations**.

---

## 3. Components

### 3.1 New: `src/dlw/api/hf_proxy.py`

A new FastAPI router. One endpoint:

```python
GET /api/v1/hf-proxy/subtask/{subtask_id}
```

```python
"""HF reverse-proxy — controller-side, injects the tenant HF token (SEC-02).

The executor never holds the HF token (INVARIANT 2). It calls this proxy
keyed by subtask_id; the controller verifies ownership (assignment_token +
epoch fence + confused-deputy guard), reconstructs the HF URL from the
subtask row, injects Settings.hf_token, follows HF's 302→CDN redirect
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
    if sub.executor_id != auth_ex.id:
        raise HTTPException(
            status_code=403,
            detail={"code": "NOT_YOUR_SUBTASK",
                    "subtask_executor": sub.executor_id,
                    "authenticated": auth_ex.id},
        )
    if str(sub.assignment_token) != x_assignment_token:
        raise HTTPException(status_code=409, detail={"code": "STALE_ASSIGNMENT"})
    if sub.executor_epoch != auth_ex.epoch:
        raise HTTPException(
            status_code=409,
            detail={"code": "EPOCH_MISMATCH",
                    "expected": sub.executor_epoch, "got": auth_ex.epoch},
        )

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

    hf_client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=settings.hf_proxy_timeout_seconds,
    )
    hf_req = hf_client.build_request("GET", hf_url, headers=hf_headers)
    hf_resp = await hf_client.send(hf_req, stream=True)

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

Implementation notes:

- `hf_client.send(..., stream=True)` is awaited eagerly so a connection error or 4xx surfaces *before* the `StreamingResponse` is constructed. The body generator runs only while the response is being sent.
- **Header allowlist** — only `content-length / content-range / content-type / accept-ranges / etag` reach the executor. HF's `Authorization` echo, `Set-Cookie`, `X-Repo-Commit` (Phase-3 commit-pin concern), and internal headers are dropped.
- **Status passthrough** — HF's 200/206/403/404/429/503 is forwarded verbatim. The executor's W2b1 tenacity (`_TRANSIENT_RETRY`) retries 5xx; W2b2's `paused_external` catches 429/503; 403/404 → the executor's generic failure path.
- **Cleanup in `finally`** — the generator's `finally` closes the HF response + client when the executor finishes or disconnects (FastAPI cancels the generator on client disconnect → `GeneratorExit` → `finally` runs). No leaked connections.
- The `hf_client` is created per-request — the proxy is stateless. Phase 3 may pool a module-level client.

### 3.2 Verification chain (security crux)

The endpoint runs four checks fail-fast before touching HF:

| # | Check | Failure | Why it matters |
|---|---|---|---|
| 1 | subtask exists | 404 | basic |
| 2 | `sub.executor_id == auth_ex.id` | 403 `NOT_YOUR_SUBTASK` | confused-deputy guard — `require_executor_jwt` only proves "a registered executor", not "owns this subtask". Without (2), executor-A could pull executor-B's file. |
| 3 | `str(sub.assignment_token) == X-Assignment-Token` | 409 `STALE_ASSIGNMENT` | a reclaimed executor still holds the old token; the row's token was cleared/reassigned. Also covers `assignment_token = NULL` (pending subtask): `str(None) == "None"` won't match a real token. |
| 4 | `sub.executor_epoch == auth_ex.epoch` | 409 `EPOCH_MISMATCH` | a re-registered executor (new epoch) can't proxy-fetch under work claimed by its old incarnation. |

### 3.3 Modified: `src/dlw/main.py`

`create_app()` includes the new router (one line, alongside the existing routers):

```python
    from dlw.api.hf_proxy import router as hf_proxy_router
    app.include_router(hf_proxy_router)
```

### 3.4 Modified: `src/dlw/config.py`

`Settings` gains one field:

```python
    # Phase 2 W3b — HF reverse-proxy
    hf_proxy_timeout_seconds: int = Field(default=300, ge=10, le=3600)
```

`Settings.hf_token` + `Settings.hf_endpoint` already exist and are unchanged — the proxy reads them.

### 3.5 New: `ControllerClient.stream_hf` in `src/dlw/executor/client.py`

```python
    @asynccontextmanager
    async def stream_hf(
        self,
        *,
        subtask_id: uuid.UUID,
        assignment_token: uuid.UUID,
        range_header: str | None = None,
    ):
        """W3b: stream a file from HF via the controller proxy. Yields the
        httpx streaming Response — callers consume resp.aiter_bytes() and
        check resp.status_code, exactly as they did with a direct HF GET."""
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

`from contextlib import asynccontextmanager` + `import uuid` are added to `client.py`. `_make_client()` (W3a) already builds the mTLS + JWT httpx client; `stream_hf` reuses it. When a `_transport` is injected (tests), `_make_client` short-circuits to the MockTransport — `stream_hf` works under both real-mTLS and test-transport modes.

### 3.6 Modified: `src/dlw/executor/types.py` (`Assignment`)

`Assignment` gains one field:

```python
@dataclass(frozen=True)
class Assignment:
    subtask_id: uuid.UUID
    task_id: uuid.UUID
    assignment_token: uuid.UUID        # NEW (W3b) — needed for the proxy call
    repo_id: str
    revision: str
    filename: str
    file_size: int | None
    expected_sha256: str | None
    storage_config: StorageConfig
```

`repo_id` / `revision` / `filename` are retained — the executor still uses them for S3 key composition (`_compose_key` / `compose_key`). They are no longer used to build an HF URL on the executor (the proxy reconstructs that controller-side).

### 3.7 Modified: `src/dlw/executor/downloader.py` (`HfS3StreamDownloader`)

- `HfS3StreamDownloader.__init__` gains a `client: ControllerClient` parameter, stored as `self._controller`.
- `_download_once` replaces the HF-URL construction + `_make_http_client()` GET with a `stream_hf` call:

```python
        async with self._controller.stream_hf(
            subtask_id=assignment.subtask_id,
            assignment_token=assignment.assignment_token,
        ) as resp:
            resp.raise_for_status()
            # ... existing: create_multipart_upload, aiter_bytes loop,
            #     upload_part, complete_multipart_upload — UNCHANGED ...
```

The S3 side (`make_s3_client`, `upload_part`, the multipart logic, the 0-byte-file `put_object` path) is **entirely unchanged**. Only the byte *source* moves from "direct HF GET" to "controller proxy stream". The downloader no longer calls `make_http_client()` for the HF leg (it may still need it removed entirely from the file if nothing else uses it — verify during implementation).

### 3.8 Modified: `src/dlw/executor/chunk_downloader.py` (`DirectOffsetDownloader`)

- `DirectOffsetDownloader.__init__` gains `client: ControllerClient`, stored as `self._controller`.
- **`_download_one_chunk`** replaces the direct HF `Range` GET with:

```python
    @_TRANSIENT_RETRY
    async def _download_one_chunk(self, a, plan, dest_dir):
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
                        # ... existing ENOSPC catch — UNCHANGED ...
```

  The `_pass1_parallel` `make_http_client()` context wrapper is removed — each `_download_one_chunk` opens its own `stream_hf`.

- **`_resolve_size`** — the W2b1 version does a `HEAD` against the HF URL. The proxy is `GET`-only (a download proxy). `_resolve_size` instead does a 1-byte probe via `stream_hf(range_header="bytes=0-0")` and reads `Content-Range` (`bytes 0-0/<total>`) for the total size. If `Content-Range` is absent (HF returned a full 200 instead of 206), fall back to the response's `Content-Length`. The probe body is discarded either way. This keeps the proxy `GET`-only — no `HEAD` route.

- The S3-side `_pass2_upload` (multipart upload, sequential SHA256) is **unchanged**.

### 3.9 Modified: `src/dlw/executor/runner.py` + `src/dlw/executor/cli.py`

- `runner._execute_subtask` builds the `Assignment` — it already receives `assignment_token` from the `/poll` response; add it to the `Assignment(...)` constructor call.
- `cli.py` builds the `ControllerClient` first, then passes it into both downloader constructors: `HfS3StreamDownloader(settings=settings, client=client)` + `DirectOffsetDownloader(settings=settings, client=client)`. The same `client` instance the `ExecutorRunner` uses.

### 3.10 Modified: `src/dlw/executor/config.py`

Delete `hf_token` and `hf_endpoint` from `ExecutorSettings`. After W3b the executor has no HF knowledge — it only knows the controller URL. The `s3_*` settings stay (the executor still uploads to S3 directly; that is W3b-unrelated).

> `pydantic-settings` is configured with `extra="ignore"` — a deployment env that still sets `DLW_EXECUTOR_HF_TOKEN` won't error, the value is just unused. The operator runbook (§5) notes it should be removed.

### 3.11 Modified: `tools/lint_invariants.py`

New helper `check_no_hf_token_in_executor` — line-scans every `.py` under `src/dlw/executor/`; fails if the identifier `hf_token` or `hf_endpoint` appears (outside comments). This locks INVARIANT 2 for the executor package: a future change re-introducing direct HF access is caught by CI.

```python
def check_no_hf_token_in_executor() -> list[str]:
    """W3b: INVARIANT 2 — the tenant HF token must never reach an executor.
    Forbid `hf_token` / `hf_endpoint` identifiers anywhere in src/dlw/executor/."""
    errors: list[str] = []
    exec_dir = ROOT / "src" / "dlw" / "executor"
    if not exec_dir.exists():
        return []
    for py in exec_dir.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "hf_token" in line or "hf_endpoint" in line:
                errors.append(
                    f"{py.relative_to(ROOT)}:{lineno}: "
                    f"'hf_token'/'hf_endpoint' forbidden in executor package "
                    f"(INVARIANT 2 — HF access goes through the controller proxy)"
                )
    return errors
```

Wired into `main()` next to the W3a `check_no_bearer_on_executor_routes`.

---

## 4. Schema Changes

**None.** No new table, no new column, no alembic migration. The proxy reads `Settings.hf_token` (controller env) + existing `FileSubTask` / `DownloadTask` columns (`repo_id`, `revision`, `filename`, `assignment_token`, `executor_id`, `executor_epoch` — all present since W1). `tenant_secrets` is Phase 3.

W3b is the first Phase-2 sub-week since W2b1 with no alembic migration.

---

## 5. Wire Format Changes

### 5.1 New endpoint `GET /api/v1/hf-proxy/subtask/{subtask_id}`

| Aspect | Value |
|---|---|
| Auth | mTLS (client cert) + `Authorization: Bearer <executor JWT>` — the W3a chain |
| Request headers | `X-Assignment-Token` (required); `Range` (optional, forwarded to HF) |
| Response 200 / 206 | streamed file bytes; `Content-Length` / `Content-Range` / `Content-Type` / `Accept-Ranges` / `ETag` forwarded |
| 401 | missing/invalid mTLS or JWT (from `require_executor_jwt`) |
| 403 | `NOT_YOUR_SUBTASK` — authenticated executor ≠ subtask's executor |
| 404 | subtask not found |
| 409 | `STALE_ASSIGNMENT` (token mismatch) or `EPOCH_MISMATCH` |
| 429 / 503 | forwarded from HF — the executor maps these to `paused_external` (W2b2) |
| 5xx | forwarded from HF — the executor's tenacity retries |

### 5.2 Executor → controller link (existing endpoints)

No change to `/register`, `/renew`, `/heartbeat`, `/poll`, `/report`. `stream_hf` adds the `X-Assignment-Token` header on top of the W3a auth headers — no new endpoint shape.

### 5.3 Config surface

- **Controller** `Settings`: gains `hf_proxy_timeout_seconds` (env `DLW_HF_PROXY_TIMEOUT_SECONDS`, default 300).
- **Executor** `ExecutorSettings`: `hf_token` (`DLW_EXECUTOR_HF_TOKEN`) and `hf_endpoint` (`DLW_EXECUTOR_HF_ENDPOINT`) are **removed**.

### 5.4 OpenAPI

`api/openapi.yaml` gains the `/hf-proxy/subtask/{subtaskId}` GET operation (tag `executors`, mTLS+JWT security, `X-Assignment-Token` + `Range` parameters, `200`/`206` streamed binary response, `403`/`404`/`409` error responses). No new schema components — the response is a binary stream.

---

## 6. Error Handling Matrix

| Situation | Behaviour |
|---|---|
| missing mTLS / JWT | 401 from `require_executor_jwt` (W3a) — before the handler runs |
| subtask not found | 404 `subtask not found` |
| authenticated executor ≠ subtask's executor | 403 `NOT_YOUR_SUBTASK` |
| `X-Assignment-Token` ≠ `subtask.assignment_token` (incl. NULL token on a pending subtask) | 409 `STALE_ASSIGNMENT` |
| executor epoch ≠ subtask's epoch | 409 `EPOCH_MISMATCH` |
| HF returns 200/206 | streamed through with allowlisted headers |
| HF returns 403/404 (private repo / missing file) | forwarded — the executor's generic failure path reports `failed` |
| HF returns 429/503 | forwarded — the executor's W2b2 `paused_external` handling catches it |
| HF returns 5xx | forwarded — the executor's W2b1 `_TRANSIENT_RETRY` retries (3 attempts) |
| HF connection error before any bytes | `hf_client.send(stream=True)` raises → FastAPI maps to 500 (or the executor's tenacity retries the proxy call) |
| HF drops mid-stream after 200 | the `_body()` generator's `aiter_bytes` raises → `finally` cleans up → the executor sees a truncated body. `DirectOffsetDownloader` asserts `len(body) == plan.length` for non-final chunks → chunk fails → tenacity retries. `HfS3StreamDownloader` → fewer bytes → the W4 sha256 verify gate flips the subtask to `failed`. Both fail-closed. |
| executor disconnects mid-stream | FastAPI cancels the `_body()` generator → `GeneratorExit` → `finally` closes the HF response + client. No leak. |
| `_resolve_size` probe: HF returns 200 (no `Content-Range`) instead of 206 | fall back to the response's `Content-Length`; the probe body is discarded |
| `Settings.hf_token` is empty (dev) | the proxy sends no `Authorization` header — HF serves public repos fine, private repos 403 (forwarded) |

---

## 7. Testing Strategy

### 7.1 Unit + integration (~13 new cases)

| # | File | Case | What it asserts |
|---|---|---|---|
| 1 | `tests/api/test_hf_proxy.py` | `test_proxy_streams_file_with_token_injected` | register executor + claim subtask; mock HF (asserts it received `Authorization: Bearer <Settings.hf_token>`); proxy returns 200 + correct body |
| 2 | same | `test_proxy_forwards_range_header` | executor sends `Range: bytes=0-1023`; mock HF asserts it received the Range, returns 206 + `Content-Range`; proxy forwards 206 + header |
| 3 | same | `test_proxy_rejects_unauthenticated` | no mTLS/JWT → 401 |
| 4 | same | `test_proxy_rejects_not_your_subtask` | executor-A authenticated, requests executor-B's subtask → 403 `NOT_YOUR_SUBTASK` |
| 5 | same | `test_proxy_rejects_stale_assignment_token` | `X-Assignment-Token` ≠ subtask row → 409 `STALE_ASSIGNMENT` |
| 6 | same | `test_proxy_rejects_epoch_mismatch` | executor epoch ≠ `subtask.executor_epoch` → 409 `EPOCH_MISMATCH` |
| 7 | same | `test_proxy_404_on_missing_subtask` | random subtask_id → 404 |
| 8 | same | `test_proxy_forwards_hf_429` | mock HF returns 429 → proxy forwards 429 |
| 9 | same | `test_proxy_reconstructs_url_from_subtask` | executor sends no path; assert the HF URL the proxy hit == `{endpoint}/{task.repo_id}/resolve/{task.revision}/{sub.filename}` |
| 10 | `tests/executor/test_client.py` | `test_stream_hf_attaches_auth_and_token_headers` | `stream_hf` request carries `Authorization` + `X-Executor-Epoch` + `X-Assignment-Token`; `Range` forwarded when given |
| 11 | `tests/executor/test_chunk_downloader.py` | `test_chunk_downloader_uses_controller_proxy` | `DirectOffsetDownloader` fetches via `client.stream_hf` (fake `ControllerClient` injected; asserts it was called, no direct HF) |
| 12 | `tests/executor/test_downloader.py` | `test_stream_downloader_uses_controller_proxy` | `HfS3StreamDownloader` likewise |
| 13 | `tests/tools/test_lint_no_hf_token.py` (or folded into the existing lint self-tests) | `test_lint_flags_hf_token_in_executor` | a fixture file containing `hf_token` makes `check_no_hf_token_in_executor` report an error |

### 7.2 Existing test migration

W3b changes the `Assignment` dataclass (+`assignment_token`) and both downloader constructors (+`client`):

| Test set | Change |
|---|---|
| `tests/executor/test_downloader.py` (W4) | `HfS3StreamDownloader(settings=)` → `(settings=, client=<fake>)`; `Assignment(...)` gains `assignment_token=`. The HF GET now goes through `client.stream_hf` — the test's HF MockTransport attaches to a **fake `ControllerClient`'s `stream_hf`**, not the downloader's `_make_http_client`. ~5-8 setup edits. |
| `tests/executor/test_chunk_downloader.py` (W2b1) | Same: `DirectOffsetDownloader(settings=, client=<fake>)`; the happy-path `_mock_transport_for_synthetic` moves from "attached to `_io.make_http_client`" to "attached to the fake `ControllerClient.stream_hf`". Pass-2 (S3 multipart) unchanged. |
| `tests/executor/test_runner*.py` (W3a) | `Assignment` gains a field — `runner._execute_subtask` adds `assignment_token=` to its `Assignment(...)` call. Runner tests already use `MagicMock` downloaders (which accept any kwargs), so the constructor-signature change doesn't break them. |
| `tests/e2e/test_executor_e2e.py` (W3a-migrated) | This e2e runs the real runner + real downloaders. The HF MockTransport now attaches to the **controller-side** proxy's httpx client (the `hf_proxy` router's HF leg), not the downloader. Moderate rewire — the executor↔controller link is the ASGI transport; the controller↔HF link is the MockTransport. |
| `tests/services/test_hf_metadata.py` (if present) | **unchanged** — `hf_metadata` is controller-side; W3b doesn't touch it. |

**New test infrastructure:** a `tests/conftest.py` helper `make_fake_controller_client(hf_handler)` — returns an object whose `stream_hf` is an `@asynccontextmanager` that internally uses `httpx.MockTransport(hf_handler)` and yields a streaming response. Shared by the two downloader test files.

### 7.3 Proxy endpoint test approach

`tests/api/test_hf_proxy.py` follows the W3a API-test pattern:
- `client` fixture: `create_app()` + `app.state.{ca,jwt_keypair,nonce_store,enrollment_token}` injected from `ephemeral_ca` + `DLW_TLS_TRUSTED_PROXY=1`.
- `Settings.hf_token` injected via `monkeypatch.setenv("DLW_HF_TOKEN", "test-hf-token-xyz")`.
- The HF upstream: the proxy's controller-side `httpx.AsyncClient` is monkeypatched (`monkeypatch.setattr("dlw.api.hf_proxy.httpx.AsyncClient", ...)`) to return a `MockTransport`-backed client whose handler asserts `Authorization: Bearer test-hf-token-xyz` (+ optional Range) and returns a synthetic body.
- Executor side: W3a's `register_test_executor` registers + a task is created → `/poll` claims a subtask → the test has `assignment_token`; then it hits the proxy with `executor_request_headers(reg)` + `X-Assignment-Token`.

### 7.4 Not tested

- A real HF endpoint — all `MockTransport`.
- A real multi-hop `302 → CDN` chain — the test asserts `follow_redirects=True` is set + the final body is correct; it does not exercise deep redirect chains.
- Large-file streaming back-pressure / memory — the design is O(64 KiB)-buffered; no perf baseline (P-004 is after W3).
- executor-disconnect-mid-stream cleanup — noted as Phase-3 hardening (hard to simulate cleanly with `ASGITransport`).
- Global 429 throttle coordination — Phase 3.

### 7.5 CI 12 checks expectations

| Check | W3b impact |
|---|---|
| pytest | +13 new; ~10-15 migrated W4/W2b1/e2e setups |
| Invariant + cross-ref lint | **+`check_no_hf_token_in_executor`** helper |
| OpenAPI lint | new `/hf-proxy/subtask/{subtaskId}` GET operation |
| Markdown lint | spec/plan cross-ref 04 §3.1 + INVARIANTS §2 |
| Other 8 | no change |

---

## 8. Acceptance Criteria

- [ ] `GET /api/v1/hf-proxy/subtask/{subtask_id}` endpoint: mTLS+JWT auth + the 4-check verification chain + URL reconstruction + streaming with `Settings.hf_token` injected + header allowlist.
- [ ] `ControllerClient.stream_hf` — `@asynccontextmanager` yielding the httpx streaming response, attaching auth + `X-Assignment-Token` (+ optional `Range`).
- [ ] `HfS3StreamDownloader` + `DirectOffsetDownloader` rewired to `client.stream_hf`; S3-side logic unchanged; `_resolve_size` uses a 1-byte range probe.
- [ ] `Assignment` dataclass gains `assignment_token`; the runner threads it in.
- [ ] `ExecutorSettings.hf_token` + `ExecutorSettings.hf_endpoint` deleted.
- [ ] `check_no_hf_token_in_executor` lint reports 0 on the production tree.
- [ ] ~13 new pytest cases pass; the migrated W4/W2b1/e2e setups pass; full suite green.
- [ ] OpenAPI: `/hf-proxy/subtask/{subtaskId}` GET documented; spectral clean.
- [ ] `docs/operator/` notes the removed `DLW_EXECUTOR_HF_TOKEN` / `DLW_EXECUTOR_HF_ENDPOINT` env + the bandwidth-through-controller tradeoff.
- [ ] No new runtime deps; no new CI jobs; **zero alembic migrations**.

---

## 9. Implementation Phasing (preview for plan)

The plan will be written by the writing-plans skill after spec approval. Expected milestone shape (3 milestones, ~7 tasks):

- **M1 — Proxy endpoint.** `src/dlw/api/hf_proxy.py` + `main.py` router include + `Settings.hf_proxy_timeout_seconds` + `tests/api/test_hf_proxy.py` (9 cases). Controller-side only; executors still call HF directly at this point (the proxy exists but is unused).
- **M2 — Executor rewiring.** `ControllerClient.stream_hf` + `Assignment.assignment_token` + both downloaders rewired + runner/cli wiring + `ExecutorSettings.hf_token`/`hf_endpoint` deleted + the `make_fake_controller_client` conftest helper + migrated downloader tests + 3 new executor tests.
- **M3 — e2e + lint + OpenAPI + PR.** `test_executor_e2e.py` rewire (HF MockTransport → controller-side) + `check_no_hf_token_in_executor` lint + the lint self-test + OpenAPI + operator runbook + PR.

Branch: `feat/phase-2-w3b-hf-reverse-proxy`. Branched off `main` at `1611d61` (PR #12 merge).

---

## 10. References

- Spec source: brainstormed 2026-05-14 (this document).
- Roadmap: `docs/v2.0/08-mvp-roadmap.md` §2.6 Phase 2 W3 Day 4.
- Security: `docs/v2.0/04-security-and-tenancy.md` §3.1 (HF Token reverse proxy, SEC-02).
- Invariants: `docs/v2.0/INVARIANTS.md` §A — INVARIANT 2 (tenant HF token never leaves the controller).
- Predecessor specs:
  - W3a: `docs/superpowers/specs/2026-05-14-phase-2-w3a-mtls-jwt-hmac-design.md` (mTLS + JWT — the auth chain `stream_hf` reuses)
  - W2b1: `docs/superpowers/specs/2026-05-13-phase-2-w2b1-chunk-level-downloader-design.md` (`DirectOffsetDownloader` — the chunk downloader being rewired)
  - W2b2: `docs/superpowers/specs/2026-05-14-phase-2-w2b2-cancel-and-paused-external-design.md` (`paused_external` — catches the forwarded 429/503)
- W3a PR (merged): https://github.com/l17728/modelpull/pull/12 (squash `1611d61`).
