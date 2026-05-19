# UI-SP3 — Infrastructure & Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 2 additive read-only backend endpoints (audit search, executors list) + 4 new frontend pages (Executors, Audit, Quota, Settings) — all reusing SP1+SP2 conventions byte-faithfully.

**Architecture:** Backend = 2 new Python modules (`api/audit.py`, `api/executors_read.py`) — placed in NEW files because `tools/lint_invariants.py:check_no_bearer_on_executor_routes` forbids non-mTLS deps in the existing `api/executors.py`; matching tenant filters in service layer; openapi.yaml extends `ExecutorRead` additively + adds `listExecutors` path + `ExecutorListResponse` schema; `searchAuditLog` implements the already-declared shape exactly. Frontend = 4 pages, 3 composables (all wrap the single `useLiveResource` seam with the SP2-added `enabled` option), 4 visual components, 5 new i18n blocks at exact en/zh parity. Zero Alembic migration.

**Tech Stack:** FastAPI · SQLAlchemy 2 async · asyncpg · Pydantic v2 · pytest · OpenAPI 3.1 (spectral + swagger-cli) · Vue 3.5 `<script setup>` TS strict · Pinia · `@tanstack/vue-query` `^5.59` (lock-resolved 5.100.x) · axios · Element Plus `^2.8.4` (lock-resolved 2.13.x) · vue-i18n 9 · Vitest 2.1 + @vue/test-utils + happy-dom · pnpm.

---

## Conventions (apply to every task — same as SP2)

- **Branch:** `feat/ui-sp3-infra-governance` (created off `main` @ `de9573a`; spec committed `0571360`).
- **Bash cwd persists across calls.** Always `cd /d/download_weights && git …` for git, `cd /d/download_weights/frontend && pnpm …` for frontend.
- **Backend tenant gate**: `audit.py` uses a single-column `AuditLog.tenant_id == principal.tenant_id` filter (no parent resource); `executors_read.py` uses `(Executor.tenant_id == principal.tenant_id) OR (Executor.tenant_id.is_(None))`, **bypassed** for `principal.role == "system_admin"` or `principal.is_service`. Both routes use `Depends(require_perm("/api/v1/<path>*", "GET"))` + `Depends(_session)`.
- **Run commands**: `uv run pytest tests/api/test_<x>.py -v` (single file); `uv run pytest tests/ -q` (full); `npx --yes @stoplight/spectral-cli lint api/openapi.yaml --fail-severity=error`; `npx --yes @apidevtools/swagger-cli validate api/openapi.yaml`; `python tools/lint_invariants.py`; `python tools/lint_no_direct_status_write.py`.
- **Frontend run**: from `frontend/` — `pnpm test:unit`, `pnpm typecheck`, `pnpm lint:fix && pnpm lint`, `pnpm build`.
- **`noUncheckedIndexedAccess`** is on — guard every `arr[i]` with `?? fallback` / optional chaining / length check.
- **Frontend mock pattern** for refs (SP2 BLOCKER fix): `vi.hoisted` for plain holders, `vi.mock(path, async () => { const { ref } = await import('vue'); … })` to create real refs lazily.
- **i18n parity**: every key added to `en-US.json` MUST be added to `zh-CN.json` in the same task at the same nesting; `localeParity.spec.ts` will catch drift.
- **Static contract `/api/v2` vs runtime `/api/v1`**: pre-existing intentional split; spectral/swagger-cli lint the static doc only.

---

## File Structure

**Backend (create):**
- `src/dlw/schemas/audit.py` — `AuditEntryRead`, `AuditSearchResponse`.
- `src/dlw/schemas/executor_read.py` — `ExecutorRead`, `ExecutorListResponse`.
- `src/dlw/services/audit_query.py` — `search_audit_log()` + local cursor encode/decode helpers.
- `src/dlw/services/executors_read.py` — `list_executors_for_principal()`.
- `src/dlw/api/audit.py` — `GET /api/v1/audit/log` route module.
- `src/dlw/api/executors_read.py` — `GET /api/v1/executors` route module.
- `tests/api/test_audit_search.py`, `tests/api/test_executors_list.py`.

**Backend (modify):**
- `api/openapi.yaml` — add `/executors` path + `ExecutorListResponse` schema; extend the existing `ExecutorRead` schema additively with new optional/nullable fields.
- `src/dlw/main.py` — register the 2 new routers.

**Frontend (create):** `src/composables/{useExecutors,useAuditLog,useSystemHealth}.ts`; `src/components/infra/{ExecutorRow,AuditRow,QuotaCard,HealthPill}.vue`; `src/pages/{Executors,Audit,QuotaPage,Settings}.vue`; `tests/unit/{formatDateTime,formatTimeAgo,sp3Composables,ExecutorRow,AuditRow,QuotaCard,HealthPill,ExecutorsPage,AuditPage,QuotaSettingsPage}.spec.ts`.

**Frontend (modify):** `src/utils/format.ts` (append `formatDateTime`, `formatTimeAgo`); `src/api/types.ts` (append SP3 DTOs); `src/router/index.ts` (4 new routes); `src/nav/registry.ts` (4 new items); `src/locale/en-US.json` + `zh-CN.json` (5 new blocks).

**Docs (modify, M4):** `docs/operator/web-ui.md`.

---

# Milestone M1 — Backend: 2 additive read endpoints

### Task 1: OpenAPI contract — add `/executors` path + extend `ExecutorRead` + add `ExecutorListResponse`

**Files:**
- Modify: `api/openapi.yaml`

- [ ] **Step 1: Extend the existing `ExecutorRead` schema additively**

In `api/openapi.yaml`, locate the `ExecutorRead:` block (currently at line ~2499 with only `id/status/health_score/epoch`). Replace the **entire block** with:

```yaml
    ExecutorRead:
      type: object
      required: [id, status, health_score, epoch]
      description: |
        Returned by /join, /heartbeat, and the browser-facing /executors list.
        UI-SP3 extends this with optional/nullable fields needed by the
        Executors page; pre-existing consumers (mTLS /join, /heartbeat) are
        unaffected because all new fields are nullable / optional.
      properties:
        id:
          type: string
        status:
          type: string
          enum: [joining, healthy, degraded, suspect, faulty]
          description: Executor lifecycle state (W2a §6).
        health_score:
          type: integer
          minimum: 0
          maximum: 100
        epoch:
          type: integer
          minimum: 0
          description: Current epoch (fence token). Increments on every /join.
        host_id:
          type: string
          nullable: true
          description: Physical host identifier (groups co-located executors).
        tenant_id:
          type: integer
          format: int64
          nullable: true
          description: Owning tenant; null for shared-infra executors.
        last_heartbeat_at:
          type: string
          format: date-time
          nullable: true
        nic_speed_gbps:
          type: integer
          nullable: true
        disk_free_gb:
          type: integer
          format: int64
          nullable: true
        disk_total_gb:
          type: integer
          format: int64
          nullable: true
        created_at:
          type: string
          format: date-time
          nullable: true
```

- [ ] **Step 2: Add the new path + response schema**

Find the `# ========== Executors ==========` section header (around line 618). The first existing path is `/executors/register` — insert the new path **before** `register` (alphabetical-ish under the section header):

```yaml
  # ========== Executors ==========
  /executors:
    get:
      tags: [executors]
      summary: List executors visible to the principal (UI-SP3)
      operationId: listExecutors
      parameters:
        - in: query
          name: status
          schema:
            type: string
            enum: [joining, healthy, degraded, suspect, faulty]
      responses:
        '200':
          description: Executors visible to the principal
          content:
            application/json:
              schema: {$ref: '#/components/schemas/ExecutorListResponse'}

```

(Keep the existing `/executors/register` block intact directly below the new block.)

Then add the new response schema just before `StorageConfig:` (which currently follows `ExecutorRead`):

```yaml
    ExecutorListResponse:
      type: object
      required: [items]
      properties:
        items:
          type: array
          items: {$ref: '#/components/schemas/ExecutorRead'}

```

- [ ] **Step 3: Extend the audit search response with `next_cursor` (additive)**

In `api/openapi.yaml`, in the `/audit/log` GET block (lines 1266–1295), replace the response `schema:` block:

```yaml
        '200':
          description: Audit entries
          content:
            application/json:
              schema:
                type: object
                required: [items]
                properties:
                  items:
                    type: array
                    items: {$ref: '#/components/schemas/AuditEntry'}
                  next_cursor:
                    type: string
                    nullable: true
                    description: Opaque cursor for the next page; null when no more rows.
```

(Strictly additive — keeps the existing `items` shape, only adds the optional `next_cursor` so the static contract and the runtime DTO are consistent.)

- [ ] **Step 4: Extend `src/dlw/authz/policy.csv` with grants for the 2 new resources**

(Pre-review BLOCKER fix: without these, all tenant_admin tests would 403 because `make_app_with_state` builds the casbin enforcer from `policy.csv` and there are no matching rows for `/api/v1/audit*` or `/api/v1/executors*` yet.)

Append after the existing `tenant_viewer ... /api/v1/quota*` line (i.e. after line 7 of `policy.csv`, BEFORE the `g, role:* …` group rows):

```
p, role:tenant_admin, /api/v1/audit*, ^GET$, tenant_match
p, role:tenant_admin, /api/v1/executors*, ^GET$, tenant_match
p, role:tenant_operator, /api/v1/audit*, ^GET$, tenant_match
p, role:tenant_operator, /api/v1/executors*, ^GET$, tenant_match
p, role:tenant_viewer, /api/v1/audit*, ^GET$, tenant_match
p, role:tenant_viewer, /api/v1/executors*, ^GET$, tenant_match
```

- [ ] **Step 5: Validate the contract**

Run: `npx --yes @stoplight/spectral-cli lint api/openapi.yaml --fail-severity=error`
Expected: 0 errors (warnings allowed; the existing `oas3-unused-component ExecutorRead` warning should DISAPPEAR because it's now referenced by `ExecutorListResponse`).
Run: `npx --yes @apidevtools/swagger-cli validate api/openapi.yaml`
Expected: `api/openapi.yaml is valid`.

- [ ] **Step 6: Commit**

```bash
cd /d/download_weights && git add api/openapi.yaml src/dlw/authz/policy.csv && git commit -q -m "UI-SP3 M1: openapi (listExecutors+extend ExecutorRead+ExecutorListResponse+searchAuditLog next_cursor) + policy.csv grants for audit/executors"
```

---

### Task 2: Audit search — DTO + service + route + tests

**Files:**
- Create: `src/dlw/schemas/audit.py`, `src/dlw/services/audit_query.py`, `src/dlw/api/audit.py`, `tests/api/test_audit_search.py`
- Modify: `src/dlw/main.py` (register router)

- [ ] **Step 1: Write the failing test**

Create `tests/api/test_audit_search.py`:

```python
"""Tests for GET /api/v1/audit/log (UI-SP3, audit-derived)."""
from __future__ import annotations

import datetime as dt

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base
from tests.conftest import make_app_with_state, principal_headers

SECRET = "unit-secret"


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(Tenant(id=1, slug="default", display_name="Default"))
        session.add(Tenant(id=2, slug="other", display_name="Other"))
        await session.flush()
        session.add(Project(id=1, tenant_id=1, name="default"))
        session.add(User(id=1, tenant_id=1, oidc_subject="dev",
                         email="d@l", role="tenant_admin"))
        session.add(StorageBackend(id=1, tenant_id=1, name="default",
                                   backend_type="s3", config_encrypted=b""))
        await session.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _set_token(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def auth() -> dict[str, str]:
    return principal_headers(secret=SECRET, role="tenant_admin")


@pytest.fixture
async def client(ephemeral_ca):
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


async def _seed(engine, *, tenant_id: int, n: int, action: str = "task.note",
                base: dt.datetime | None = None):
    from dlw.db.models.audit import AuditLog
    base = base or dt.datetime(2026, 5, 20, 12, 0, 0, tzinfo=dt.UTC)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        for i in range(n):
            s.add(AuditLog(
                occurred_at=base + dt.timedelta(seconds=i),
                tenant_id=tenant_id, actor_user_id=1, action=action,
                resource_type="task", resource_id=f"r{i}",
                outcome="success", payload={"i": i}, self_hash="0" * 64))
        await s.commit()


@pytest.mark.slow
async def test_audit_unauthenticated_401(client: AsyncClient) -> None:
    r = await client.get("/api/v1/audit/log")
    assert r.status_code == 401


@pytest.mark.slow
async def test_audit_tenant_isolation(
    client: AsyncClient, auth, engine,
) -> None:
    await _seed(engine, tenant_id=2, n=2,
                base=dt.datetime(2026, 5, 20, 9, 0, tzinfo=dt.UTC))
    await _seed(engine, tenant_id=1, n=2,
                base=dt.datetime(2026, 5, 20, 10, 0, tzinfo=dt.UTC))
    r = await client.get("/api/v1/audit/log", headers=auth)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 2  # only tenant 1 rows
    assert all(item["tenant_id"] == 1 for item in items)


@pytest.mark.slow
async def test_audit_happy_filters_and_pagination(
    client: AsyncClient, auth, engine,
) -> None:
    await _seed(engine, tenant_id=1, n=3, action="task.created",
                base=dt.datetime(2026, 5, 20, 11, 0, tzinfo=dt.UTC))
    await _seed(engine, tenant_id=1, n=2, action="task.cancelled",
                base=dt.datetime(2026, 5, 20, 11, 30, tzinfo=dt.UTC))
    # default page — newest first
    r = await client.get("/api/v1/audit/log?limit=3", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    items = body["items"]
    assert len(items) == 3
    assert items[0]["action"] == "task.cancelled"
    assert "next_cursor" in body
    assert body["next_cursor"]
    # action filter
    r2 = await client.get("/api/v1/audit/log?action=task.created&limit=10",
                          headers=auth)
    assert r2.status_code == 200
    items2 = r2.json()["items"]
    assert len(items2) >= 3
    assert all(it["action"].startswith("task.created") for it in items2)
    # cursor pagination — next page doesn't repeat first item
    r3 = await client.get(
        f"/api/v1/audit/log?limit=3&cursor={body['next_cursor']}",
        headers=auth)
    assert r3.status_code == 200
    page2 = r3.json()["items"]
    assert len(page2) >= 1
    assert page2[0]["id"] != items[0]["id"]


@pytest.mark.slow
async def test_audit_actor_and_time_range_filters(
    client: AsyncClient, auth, engine,
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from dlw.db.models.audit import AuditLog
    factory = async_sessionmaker(engine, expire_on_commit=False)
    base = dt.datetime(2026, 5, 20, 13, 0, 0, tzinfo=dt.UTC)
    async with factory() as s:
        s.add(AuditLog(
            occurred_at=base, tenant_id=1, actor_user_id=42,
            action="user.login", resource_type="user", resource_id="42",
            outcome="success", payload={}, self_hash="0" * 64))
        s.add(AuditLog(
            occurred_at=base + dt.timedelta(hours=1), tenant_id=1,
            actor_user_id=99, action="user.login", resource_type="user",
            resource_id="99", outcome="success", payload={},
            self_hash="0" * 64))
        await s.commit()
    r = await client.get(
        "/api/v1/audit/log?actor_user_id=42&limit=10", headers=auth)
    items = r.json()["items"]
    assert all(it["actor_user_id"] == 42 for it in items)
    assert any(it["action"] == "user.login" for it in items)
    # time-range filter
    from_iso = (base + dt.timedelta(minutes=30)).isoformat()
    r2 = await client.get(
        f"/api/v1/audit/log?from={from_iso}&limit=10", headers=auth)
    items2 = r2.json()["items"]
    assert all(it["actor_user_id"] != 42 or
               dt.datetime.fromisoformat(it["occurred_at"]) >=
               base + dt.timedelta(minutes=30) for it in items2)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/api/test_audit_search.py -v`
Expected: FAIL — route 404 (not implemented).

- [ ] **Step 3: Create the DTO**

Create `src/dlw/schemas/audit.py`:

```python
"""UI-SP3 Audit-search read-only DTOs (additive)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class AuditEntryRead(BaseModel):
    """Mirrors api/openapi.yaml AuditEntry (`searchAuditLog` response item).

    Contract declares several fields as non-nullable that the ORM column
    permits NULL; we coerce None -> "" (or {}) to stay contract-faithful.
    """
    model_config = ConfigDict(from_attributes=True)

    id: int
    occurred_at: datetime
    tenant_id: int | None
    actor_user_id: int | None
    actor_ip: str  # contract non-nullable; coerced from None in router
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    payload: dict[str, Any]  # contract non-nullable; coerced {} in router
    trace_id: str  # contract non-nullable; coerced "" in router
    prev_hash: str | None
    self_hash: str


class AuditSearchResponse(BaseModel):
    """Response for GET /api/v1/audit/log.

    Matches the on-disk contract (`searchAuditLog`) which UI-SP3 extends in
    Task 1 Step 3 to include `next_cursor` (nullable) for client pagination.
    """
    items: list[AuditEntryRead]
    next_cursor: str | None = None
```

- [ ] **Step 4: Create the service**

Create `src/dlw/services/audit_query.py`:

```python
"""UI-SP3 audit-log search (read-only; tenant-scoped; cursor-paginated)."""
from __future__ import annotations

import base64
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.audit import AuditLog
from dlw.schemas.audit import AuditEntryRead


def _encode_cursor(occurred_at: datetime, row_id: int) -> str:
    raw = f"{occurred_at.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    ts_str, id_str = raw.rsplit("|", 1)
    return datetime.fromisoformat(ts_str), int(id_str)


async def search_audit_log(
    session: AsyncSession, tenant_id: int, *,
    actor_user_id: int | None,
    action_prefix: str | None,
    from_: datetime | None,
    to: datetime | None,
    cursor: str | None,
    limit: int,
) -> tuple[list[AuditEntryRead], str | None]:
    stmt = select(AuditLog).where(AuditLog.tenant_id == tenant_id)
    if actor_user_id is not None:
        stmt = stmt.where(AuditLog.actor_user_id == actor_user_id)
    if action_prefix:
        stmt = stmt.where(AuditLog.action.like(f"{action_prefix}%"))
    if from_ is not None:
        stmt = stmt.where(AuditLog.occurred_at >= from_)
    if to is not None:
        stmt = stmt.where(AuditLog.occurred_at <= to)
    stmt = stmt.order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
    if cursor:
        c_ts, c_id = _decode_cursor(cursor)
        stmt = stmt.where(or_(
            AuditLog.occurred_at < c_ts,
            and_(AuditLog.occurred_at == c_ts, AuditLog.id < c_id)))
    rows = (await session.execute(stmt.limit(limit + 1))).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        AuditEntryRead(
            id=r.id, occurred_at=r.occurred_at, tenant_id=r.tenant_id,
            actor_user_id=r.actor_user_id,
            actor_ip=str(r.actor_ip) if r.actor_ip is not None else "",
            action=r.action, resource_type=r.resource_type,
            resource_id=r.resource_id, outcome=r.outcome,
            payload=r.payload or {},
            trace_id=r.trace_id or "",
            prev_hash=r.prev_hash, self_hash=r.self_hash,
        )
        for r in rows
    ]
    next_cursor = (
        _encode_cursor(rows[-1].occurred_at, rows[-1].id)
        if has_more and rows else None)
    return items, next_cursor
```

- [ ] **Step 5: Create the route**

Create `src/dlw/api/audit.py`:

```python
"""GET /api/v1/audit/log — tenant-scoped audit search (UI-SP3)."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dlw.auth.principal import Principal
from dlw.authz.deps import require_perm
from dlw.db.session import get_engine
from dlw.schemas.audit import AuditSearchResponse
from dlw.services.audit_query import search_audit_log

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


async def _session():
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with factory() as session:
        yield session


@router.get("/log")
async def get_audit_log(
    actor_user_id: int | None = Query(default=None),
    action: str | None = Query(default=None, description="prefix match"),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(require_perm("/api/v1/audit*", "GET")),
    session: AsyncSession = Depends(_session),
) -> AuditSearchResponse:
    items, next_cursor = await search_audit_log(
        session, principal.tenant_id,
        actor_user_id=actor_user_id, action_prefix=action,
        from_=from_, to=to, cursor=cursor, limit=limit)
    return AuditSearchResponse(items=items, next_cursor=next_cursor)
```

- [ ] **Step 6: Register the router in `src/dlw/main.py`**

The existing pattern (verified `src/dlw/main.py:285-307`): every router is **lazy-imported inside `create_app()`** using `from dlw.api.X import router as X_router; app.include_router(X_router)`. The last existing line is `app.include_router(source_proxy_router)` at line 307.

Add **two lines** immediately after line 307 (inside `create_app()`, same 4-space indent):

```python
    from dlw.api.audit import router as audit_router
    app.include_router(audit_router)
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `uv run pytest tests/api/test_audit_search.py -v`
Expected: PASS (4 tests).

- [ ] **Step 8: Commit**

```bash
cd /d/download_weights && git add src/dlw/schemas/audit.py src/dlw/services/audit_query.py src/dlw/api/audit.py src/dlw/main.py tests/api/test_audit_search.py && git commit -q -m "UI-SP3 M1: audit-log search endpoint (tenant-scoped, cursor-paginated)"
```

---

### Task 3: Executors list — DTO + service + route + tests

**Files:**
- Create: `src/dlw/schemas/executor_read.py`, `src/dlw/services/executors_read.py`, `src/dlw/api/executors_read.py`, `tests/api/test_executors_list.py`
- Modify: `src/dlw/main.py` (register router)

- [ ] **Step 1: Write the failing test**

Create `tests/api/test_executors_list.py`:

```python
"""Tests for GET /api/v1/executors (UI-SP3)."""
from __future__ import annotations

import datetime as dt

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base
from tests.conftest import make_app_with_state, principal_headers

SECRET = "unit-secret"


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(Tenant(id=1, slug="default", display_name="Default"))
        session.add(Tenant(id=2, slug="other", display_name="Other"))
        await session.flush()
        session.add(Project(id=1, tenant_id=1, name="default"))
        session.add(User(id=1, tenant_id=1, oidc_subject="dev",
                         email="d@l", role="tenant_admin"))
        session.add(StorageBackend(id=1, tenant_id=1, name="default",
                                   backend_type="s3", config_encrypted=b""))
        await session.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _set_token(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def auth() -> dict[str, str]:
    return principal_headers(secret=SECRET, role="tenant_admin")


@pytest.fixture
async def client(ephemeral_ca):
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


async def _seed_executors(engine):
    from dlw.db.models.executor import Executor
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Executor(id="t1-w1", host_id="host-a", cert_fingerprint="fp1",
                       status="healthy", epoch=1, health_score=95,
                       tenant_id=1, nic_speed_gbps=10,
                       disk_free_gb=500, disk_total_gb=1000,
                       last_heartbeat_at=dt.datetime(
                           2026, 5, 20, 12, 0, tzinfo=dt.UTC)))
        s.add(Executor(id="t2-w1", host_id="host-b", cert_fingerprint="fp2",
                       status="degraded", epoch=2, health_score=60,
                       tenant_id=2))
        s.add(Executor(id="shared-1", host_id="host-c", cert_fingerprint="fp3",
                       status="healthy", epoch=1, health_score=100,
                       tenant_id=None))
        await s.commit()


@pytest.mark.slow
async def test_executors_unauthenticated_401(client: AsyncClient) -> None:
    r = await client.get("/api/v1/executors")
    assert r.status_code == 401


@pytest.mark.slow
async def test_executors_tenant_filter(
    client: AsyncClient, auth, engine,
) -> None:
    await _seed_executors(engine)
    r = await client.get("/api/v1/executors", headers=auth)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    ids = {it["id"] for it in items}
    # tenant_admin sees own-tenant (t1-w1) + shared-infra (shared-1)
    assert "t1-w1" in ids
    assert "shared-1" in ids
    assert "t2-w1" not in ids


@pytest.mark.slow
async def test_executors_status_filter(
    client: AsyncClient, auth, engine,
) -> None:
    await _seed_executors(engine)
    r = await client.get("/api/v1/executors?status=healthy", headers=auth)
    items = r.json()["items"]
    assert all(it["status"] == "healthy" for it in items)
    assert {it["id"] for it in items} >= {"t1-w1", "shared-1"}


@pytest.mark.slow
async def test_executors_system_admin_sees_all(
    client: AsyncClient, engine,
) -> None:
    await _seed_executors(engine)
    admin = principal_headers(secret=SECRET, role="system_admin",
                              user_id=0, tenant_id=1)
    r = await client.get("/api/v1/executors", headers=admin)
    items = r.json()["items"]
    ids = {it["id"] for it in items}
    assert {"t1-w1", "t2-w1", "shared-1"} <= ids
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/api/test_executors_list.py -v`
Expected: FAIL — route 404.

- [ ] **Step 3: Create the DTO**

Create `src/dlw/schemas/executor_read.py`:

```python
"""UI-SP3 Executor list read-only DTOs (additive)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ExecutorRead(BaseModel):
    """Browser-facing executor shape (matches openapi.yaml `ExecutorRead`)."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    health_score: int
    epoch: int
    host_id: str | None
    tenant_id: int | None
    last_heartbeat_at: datetime | None
    nic_speed_gbps: int | None
    disk_free_gb: int | None
    disk_total_gb: int | None
    created_at: datetime | None


class ExecutorListResponse(BaseModel):
    items: list[ExecutorRead]
```

- [ ] **Step 4: Create the service**

Create `src/dlw/services/executors_read.py`:

```python
"""UI-SP3 executors list (read-only, tenant-scoped or admin-wide)."""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.auth.principal import Principal
from dlw.db.models.executor import Executor

_VALID_STATUS = {"joining", "healthy", "degraded", "suspect", "faulty"}


def _is_admin(principal: Principal) -> bool:
    return (getattr(principal, "is_service", False)
            or principal.role == "system_admin")


async def list_executors_for_principal(
    session: AsyncSession, principal: Principal,
    status_filter: str | None,
) -> list[Executor]:
    stmt = select(Executor)
    if not _is_admin(principal):
        stmt = stmt.where(or_(
            Executor.tenant_id.is_(None),
            Executor.tenant_id == principal.tenant_id))
    if status_filter:
        if status_filter not in _VALID_STATUS:
            # FastAPI's `Query(enum=...)` validates at the boundary; this is
            # defence-in-depth for non-HTTP callers.
            return []
        stmt = stmt.where(Executor.status == status_filter)
    stmt = stmt.order_by(Executor.host_id.asc().nullslast(),
                         Executor.id.asc())
    return (await session.execute(stmt)).scalars().all()
```

- [ ] **Step 5: Create the route**

Create `src/dlw/api/executors_read.py`:

```python
"""GET /api/v1/executors — browser-facing executor list (UI-SP3).

NOT in src/dlw/api/executors.py because that file is mTLS-only per
tools/lint_invariants.py:check_no_bearer_on_executor_routes (it forbids
require_bearer-style auth there). This module uses require_perm.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dlw.auth.principal import Principal
from dlw.authz.deps import require_perm
from dlw.db.session import get_engine
from dlw.schemas.executor_read import ExecutorListResponse, ExecutorRead
from dlw.services.executors_read import list_executors_for_principal

router = APIRouter(prefix="/api/v1/executors", tags=["executors"])

_StatusLit = Literal["joining", "healthy", "degraded", "suspect", "faulty"]


async def _session():
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with factory() as session:
        yield session


@router.get("")
async def list_executors(
    status: _StatusLit | None = Query(default=None),
    principal: Principal = Depends(require_perm("/api/v1/executors*", "GET")),
    session: AsyncSession = Depends(_session),
) -> ExecutorListResponse:
    rows = await list_executors_for_principal(session, principal, status)
    return ExecutorListResponse(
        items=[ExecutorRead.model_validate(r) for r in rows])
```

- [ ] **Step 6: Register the router in `src/dlw/main.py`**

Add **two lines** immediately after the audit-router lines added in Task 2 (inside `create_app()`, same 4-space indent):

```python
    from dlw.api.executors_read import router as executors_read_router
    app.include_router(executors_read_router)
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `uv run pytest tests/api/test_executors_list.py -v`
Expected: PASS (4 tests).

- [ ] **Step 8: Commit**

```bash
cd /d/download_weights && git add src/dlw/schemas/executor_read.py src/dlw/services/executors_read.py src/dlw/api/executors_read.py src/dlw/main.py tests/api/test_executors_list.py && git commit -q -m "UI-SP3 M1: GET /executors browser-facing list (tenant filter; system_admin bypass)"
```

---

### Task 4: M1 gate — full backend suite + contract + invariant + status-write lint

**Files:** none.

- [ ] **Step 1: Full backend suite**

Run: `uv run pytest tests/ -q`
Expected: prior 439 + new 8 = 447, 0 failures.

- [ ] **Step 2: OpenAPI + invariant + status-write lint**

Run: `npx --yes @stoplight/spectral-cli lint api/openapi.yaml --fail-severity=error` → 0 errors. Run: `npx --yes @apidevtools/swagger-cli validate api/openapi.yaml` → valid. Run: `python tools/lint_invariants.py` → OK. Run: `python tools/lint_no_direct_status_write.py` → OK.

- [ ] **Step 3: Commit (only if fixups needed)**

```bash
cd /d/download_weights && git status -s
# If any fixups: git add -A && git commit -q -m "UI-SP3 M1 gate: backend suite + openapi + invariant green"
```

---

# Milestone M2 — Frontend foundation

### Task 5: API DTO types for the 2 endpoints

**Files:**
- Modify: `frontend/src/api/types.ts`
- Test: `frontend/tests/unit/sp3Types.spec.ts`

- [ ] **Step 1: Write the failing test**

Create `frontend/tests/unit/sp3Types.spec.ts`:

```ts
import { describe, expect, test } from 'vitest'
import type {
  ExecutorRead, ExecutorListResponse,
  AuditEntry, AuditSearchResponse,
  HealthActive,
} from '@/api/types'

describe('SP3 DTO types', () => {
  test('shapes compile', () => {
    const ex: ExecutorRead = {
      id: 'e', status: 'healthy', health_score: 100, epoch: 1,
      host_id: 'h', tenant_id: 1, last_heartbeat_at: null,
      nic_speed_gbps: 10, disk_free_gb: 100, disk_total_gb: 200,
      created_at: null,
    }
    const exList: ExecutorListResponse = { items: [ex] }
    const ent: AuditEntry = {
      id: 1, occurred_at: 'now', tenant_id: 1, actor_user_id: 1,
      actor_ip: '', action: 'task.note', resource_type: 'task',
      resource_id: 'r', outcome: 'success', payload: {}, trace_id: '',
      prev_hash: null, self_hash: 's',
    }
    const audit: AuditSearchResponse = { items: [ent], next_cursor: null }
    const h: HealthActive = { status: 'active', controller_state: 'active' }
    expect(exList.items[0]?.id).toBe('e')
    expect(audit.items[0]?.action).toBe('task.note')
    expect(h.controller_state).toBe('active')
  })
})
```

- [ ] **Step 2: Run to verify it fails**

`cd /d/download_weights/frontend && pnpm test:unit -- sp3Types`
Expected: FAIL — types not exported.

- [ ] **Step 3: Append to `frontend/src/api/types.ts`**

```ts

export interface ExecutorRead {
  id: string
  status: string
  health_score: number
  epoch: number
  host_id: string | null
  tenant_id: number | null
  last_heartbeat_at: string | null
  nic_speed_gbps: number | null
  disk_free_gb: number | null
  disk_total_gb: number | null
  created_at: string | null
}

export interface ExecutorListResponse {
  items: ExecutorRead[]
}

export interface AuditEntry {
  id: number
  occurred_at: string
  tenant_id: number | null
  actor_user_id: number | null
  actor_ip: string
  action: string
  resource_type: string
  resource_id: string | null
  outcome: string
  payload: Record<string, unknown>
  trace_id: string
  prev_hash: string | null
  self_hash: string
}

export interface AuditSearchResponse {
  items: AuditEntry[]
  next_cursor: string | null
}

export interface HealthActive {
  status: string
  controller_state: string
}
```

- [ ] **Step 4: Verify + lint + commit**

`cd /d/download_weights/frontend && pnpm test:unit -- sp3Types` → PASS. `pnpm typecheck` → 0 errors. `pnpm lint:fix && pnpm lint` → OK.
`cd /d/download_weights && git add frontend/src/api/types.ts frontend/tests/unit/sp3Types.spec.ts && git commit -q -m "UI-SP3 M2: SP3 DTO types (Executor / Audit / HealthActive)"`

---

### Task 6: Format utilities — `formatDateTime` + `formatTimeAgo`

**Files:**
- Modify: `frontend/src/utils/format.ts`
- Test: `frontend/tests/unit/formatDateTime.spec.ts`, `frontend/tests/unit/formatTimeAgo.spec.ts`

- [ ] **Step 1: Write the failing tests**

Create `frontend/tests/unit/formatDateTime.spec.ts`:

```ts
import { describe, expect, test } from 'vitest'
import { formatDateTime } from '@/utils/format'

describe('formatDateTime', () => {
  test('null → em-dash', () => {
    expect(formatDateTime(null)).toBe('—')
    expect(formatDateTime(undefined)).toBe('—')
  })
  test('valid ISO → locale string (not the raw ISO)', () => {
    const out = formatDateTime('2026-05-20T12:00:00Z')
    expect(out).not.toBe('—')
    expect(out).not.toBe('2026-05-20T12:00:00Z')
  })
  test('invalid → falls back to the input', () => {
    expect(formatDateTime('not-a-date')).toBe('not-a-date')
  })
})
```

Create `frontend/tests/unit/formatTimeAgo.spec.ts`:

```ts
import { describe, expect, test, vi, afterEach } from 'vitest'
import { formatTimeAgo } from '@/utils/format'

const NOW = new Date('2026-05-20T12:00:00Z').getTime()

describe('formatTimeAgo', () => {
  afterEach(() => { vi.useRealTimers() })
  test('null → —', () => {
    expect(formatTimeAgo(null)).toBe('—')
  })
  test('30s ago → "30s ago"', () => {
    vi.useFakeTimers().setSystemTime(NOW)
    expect(formatTimeAgo('2026-05-20T11:59:30Z')).toBe('30s ago')
  })
  test('5m ago → "5m ago"', () => {
    vi.useFakeTimers().setSystemTime(NOW)
    expect(formatTimeAgo('2026-05-20T11:55:00Z')).toBe('5m ago')
  })
  test('2h ago → "2h ago"', () => {
    vi.useFakeTimers().setSystemTime(NOW)
    expect(formatTimeAgo('2026-05-20T10:00:00Z')).toBe('2h ago')
  })
  test('3d ago → "3d ago"', () => {
    vi.useFakeTimers().setSystemTime(NOW)
    expect(formatTimeAgo('2026-05-17T12:00:00Z')).toBe('3d ago')
  })
  test('invalid → —', () => {
    expect(formatTimeAgo('not-a-date')).toBe('—')
  })
})
```

- [ ] **Step 2: Run to verify they fail**

`cd /d/download_weights/frontend && pnpm test:unit -- formatDateTime formatTimeAgo`
Expected: FAIL — functions not exported.

- [ ] **Step 3: Append to `frontend/src/utils/format.ts`**

```ts

export function formatDateTime(iso: string | null | undefined): string {
  if (iso === null || iso === undefined || iso === '') return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString()
}

export function formatTimeAgo(iso: string | null | undefined): string {
  if (iso === null || iso === undefined || iso === '') return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const secs = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000))
  if (secs < 60) return `${secs}s ago`
  const mins = Math.floor(secs / 60)
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}
```

- [ ] **Step 4: Verify + lint + commit**

`cd /d/download_weights/frontend && pnpm test:unit -- formatDateTime formatTimeAgo` → PASS. `pnpm typecheck` → 0. `pnpm lint:fix && pnpm lint` → OK.
`cd /d/download_weights && git add frontend/src/utils/format.ts frontend/tests/unit/formatDateTime.spec.ts frontend/tests/unit/formatTimeAgo.spec.ts && git commit -q -m "UI-SP3 M2: formatDateTime + formatTimeAgo utils"`

---

### Task 7: Composables — `useExecutors`, `useAuditLog`, `useSystemHealth`

**Files:**
- Create: `frontend/src/composables/{useExecutors,useAuditLog,useSystemHealth}.ts`
- Test: `frontend/tests/unit/sp3Composables.spec.ts`

- [ ] **Step 1: Write the failing test**

Create `frontend/tests/unit/sp3Composables.spec.ts`:

```ts
import { describe, expect, test, vi } from 'vitest'
import { ref } from 'vue'

const { get } = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('@/api/client', () => ({ client: { get } }))

const { captured } = vi.hoisted(() => ({
  captured: [] as Array<{ key: unknown; opts: unknown }>,
}))
vi.mock('@/composables/useLiveResource', () => ({
  useLiveResource: (key: unknown, fetcher: () => unknown, opts: unknown) => {
    captured.push({ key, opts })
    return { __fetcher: fetcher }
  },
}))

import { useExecutors } from '@/composables/useExecutors'
import { useAuditLog, fetchOlderAudit } from '@/composables/useAuditLog'
import { useSystemHealth } from '@/composables/useSystemHealth'

describe('SP3 live composables', () => {
  test('useExecutors wires key + status query + interval', async () => {
    captured.length = 0
    get.mockResolvedValueOnce({ data: { items: [] } })
    const status = ref<string | null>(null)
    const q = useExecutors(status) as unknown as {
      __fetcher: () => Promise<unknown>
    }
    const last = captured[captured.length - 1]
    expect(last?.key).toEqual(['executors', status])
    expect((last?.opts as { baseIntervalMs: number }).baseIntervalMs).toBe(5_000)
    await q.__fetcher()
    expect(get).toHaveBeenCalledWith('/api/v1/executors')
    captured.length = 0
    status.value = 'healthy'
    get.mockResolvedValueOnce({ data: { items: [] } })
    const q2 = useExecutors(status) as unknown as {
      __fetcher: () => Promise<unknown>
    }
    await q2.__fetcher()
    expect(get).toHaveBeenCalledWith('/api/v1/executors?status=healthy')
  })

  test('useAuditLog builds query from filters', async () => {
    captured.length = 0
    get.mockResolvedValueOnce(
      { data: { items: [], next_cursor: null } })
    const filters = {
      action: ref('task.'),
      actor: ref<number | null>(42),
      from: ref<string | null>(null),
      to: ref<string | null>(null),
    }
    const q = useAuditLog(filters) as unknown as {
      __fetcher: () => Promise<unknown>
    }
    await q.__fetcher()
    const call = get.mock.calls[get.mock.calls.length - 1] as
      [string, ...unknown[]] | undefined
    const url = call?.[0] ?? ''
    expect(url).toContain('/api/v1/audit/log')
    expect(url).toContain('limit=50')
    expect(url).toContain('action=task.')
    expect(url).toContain('actor_user_id=42')
  })

  test('fetchOlderAudit appends cursor', async () => {
    get.mockResolvedValueOnce(
      { data: { items: [], next_cursor: null } })
    await fetchOlderAudit({ action: 'x', actor: null, from: null, to: null },
                          'CURSOR')
    const call = get.mock.calls[get.mock.calls.length - 1] as
      [string, ...unknown[]] | undefined
    const url = call?.[0] ?? ''
    expect(url).toContain('cursor=CURSOR')
    expect(url).toContain('action=x')
  })

  test('useSystemHealth hits /health/active', async () => {
    captured.length = 0
    get.mockResolvedValueOnce(
      { data: { status: 'active', controller_state: 'active' } })
    const q = useSystemHealth() as unknown as {
      __fetcher: () => Promise<unknown>
    }
    await q.__fetcher()
    expect(get).toHaveBeenCalledWith('/health/active')
    const last = captured[captured.length - 1]
    expect((last?.opts as { baseIntervalMs: number }).baseIntervalMs).toBe(10_000)
  })
})
```

- [ ] **Step 2: Run to verify it fails**

`cd /d/download_weights/frontend && pnpm test:unit -- sp3Composables`
Expected: FAIL — modules not found.

- [ ] **Step 3: Create the three composables**

`frontend/src/composables/useExecutors.ts`:

```ts
import type { Ref } from 'vue'
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { ExecutorListResponse } from '@/api/types'

export function useExecutors(status: Ref<string | null>) {
  return useLiveResource<ExecutorListResponse>(
    ['executors', status],
    async () => {
      const q = status.value ? `?status=${encodeURIComponent(status.value)}` : ''
      return (await client.get<ExecutorListResponse>(
        `/api/v1/executors${q}`)).data
    },
    { baseIntervalMs: 5_000 },
  )
}
```

`frontend/src/composables/useAuditLog.ts`:

```ts
import type { Ref } from 'vue'
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { AuditSearchResponse } from '@/api/types'

export interface AuditFilters {
  action: Ref<string>
  actor: Ref<number | null>
  from: Ref<string | null>
  to: Ref<string | null>
}
export interface AuditFiltersPlain {
  action: string
  actor: number | null
  from: string | null
  to: string | null
}

function buildQuery(
  f: AuditFiltersPlain, cursor: string | null,
): string {
  const p = new URLSearchParams()
  p.set('limit', '50')
  if (f.action) p.set('action', f.action)
  if (f.actor !== null) p.set('actor_user_id', String(f.actor))
  if (f.from) p.set('from', f.from)
  if (f.to) p.set('to', f.to)
  if (cursor) p.set('cursor', cursor)
  return `/api/v1/audit/log?${p.toString()}`
}

export function useAuditLog(f: AuditFilters) {
  return useLiveResource<AuditSearchResponse>(
    ['audit', f.action, f.actor, f.from, f.to],
    async () => (await client.get<AuditSearchResponse>(buildQuery({
      action: f.action.value, actor: f.actor.value,
      from: f.from.value, to: f.to.value,
    }, null))).data,
    { baseIntervalMs: 10_000 },
  )
}

export async function fetchOlderAudit(
  f: AuditFiltersPlain, cursor: string,
): Promise<AuditSearchResponse> {
  return (await client.get<AuditSearchResponse>(buildQuery(f, cursor))).data
}
```

`frontend/src/composables/useSystemHealth.ts`:

```ts
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { HealthActive } from '@/api/types'

export function useSystemHealth() {
  return useLiveResource<HealthActive>(
    ['health-active'],
    async () => (await client.get<HealthActive>('/health/active')).data,
    { baseIntervalMs: 10_000 },
  )
}
```

- [ ] **Step 4: Verify + lint + commit**

`cd /d/download_weights/frontend && pnpm test:unit -- sp3Composables` → PASS (4 tests). `pnpm typecheck` → 0. `pnpm lint:fix && pnpm lint` → OK.
`cd /d/download_weights && git add frontend/src/composables/useExecutors.ts frontend/src/composables/useAuditLog.ts frontend/src/composables/useSystemHealth.ts frontend/tests/unit/sp3Composables.spec.ts && git commit -q -m "UI-SP3 M2: 3 live composables (executors / audit / health) on the useLiveResource seam"`

---

### Task 8: M2 gate

**Files:** none.

- [ ] `cd /d/download_weights/frontend && pnpm test:unit && pnpm typecheck && pnpm lint && pnpm build` → all green. If any fixups, commit with `UI-SP3 M2 gate: frontend foundation green`.

---

# Milestone M3 — Visual components

### Task 9: `HealthPill` + `ExecutorRow`

**Files:**
- Create: `frontend/src/components/infra/{HealthPill,ExecutorRow}.vue`
- Test: `frontend/tests/unit/{HealthPill,ExecutorRow}.spec.ts`

- [ ] **Step 1: Write the failing tests**

`frontend/tests/unit/HealthPill.spec.ts`:

```ts
import { describe, expect, test } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import ElementPlus from 'element-plus'
import HealthPill from '@/components/infra/HealthPill.vue'
import en from '@/locale/en-US.json'

const i18n = createI18n({
  legacy: false, locale: 'en-US', messages: { 'en-US': en },
})

describe('HealthPill', () => {
  test('active → success ElTag', () => {
    const w = mount(HealthPill, {
      props: { state: 'active' },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.findComponent({ name: 'ElTag' }).props('type')).toBe('success')
    expect(w.text()).toContain('active')
  })
  test('recovering → warning', () => {
    const w = mount(HealthPill, {
      props: { state: 'recovering' },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.findComponent({ name: 'ElTag' }).props('type')).toBe('warning')
  })
  test('standby → info', () => {
    const w = mount(HealthPill, {
      props: { state: 'standby' },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.findComponent({ name: 'ElTag' }).props('type')).toBe('info')
  })
  test('unknown → danger', () => {
    const w = mount(HealthPill, {
      props: { state: 'broken' },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.findComponent({ name: 'ElTag' }).props('type')).toBe('danger')
  })
})
```

`frontend/tests/unit/ExecutorRow.spec.ts`:

```ts
import { describe, expect, test, vi, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import ElementPlus from 'element-plus'
import ExecutorRow from '@/components/infra/ExecutorRow.vue'
import en from '@/locale/en-US.json'
import type { ExecutorRead } from '@/api/types'

const i18n = createI18n({
  legacy: false, locale: 'en-US', messages: { 'en-US': en },
})

const ex: ExecutorRead = {
  id: 'host-1-w1', status: 'healthy', health_score: 95, epoch: 1,
  host_id: 'host-1', tenant_id: 1,
  last_heartbeat_at: '2026-05-20T11:55:00Z',
  nic_speed_gbps: 10, disk_free_gb: 500, disk_total_gb: 1000,
  created_at: null,
}

describe('ExecutorRow', () => {
  afterEach(() => { vi.useRealTimers() })
  test('renders id, status badge, health, NIC, disk', () => {
    vi.useFakeTimers().setSystemTime(new Date('2026-05-20T12:00:00Z'))
    const w = mount(ExecutorRow, {
      props: { executor: ex },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.text()).toContain('host-1-w1')
    expect(w.text()).toContain('healthy')
    expect(w.text()).toContain('95')
    expect(w.text()).toContain('5m ago')
    expect(w.text()).toContain('10')
    expect(w.findComponent({ name: 'ElTag' }).exists()).toBe(true)
  })
  test('null fields → em-dash, no crash', () => {
    const w = mount(ExecutorRow, {
      props: {
        executor: {
          ...ex, last_heartbeat_at: null, nic_speed_gbps: null,
          disk_free_gb: null, disk_total_gb: null,
        },
      },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.text()).toContain('host-1-w1')
  })
})
```

- [ ] **Step 2: Run to verify they fail**

`cd /d/download_weights/frontend && pnpm test:unit -- HealthPill ExecutorRow`
Expected: FAIL — modules not found.

- [ ] **Step 3: Create the components**

`frontend/src/components/infra/HealthPill.vue`:

```vue
<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ state: string }>()
type ElTagType = 'success' | 'warning' | 'info' | 'danger'
const tagType = computed<ElTagType>(() => {
  if (props.state === 'active') return 'success'
  if (props.state === 'recovering') return 'warning'
  if (props.state === 'standby') return 'info'
  return 'danger'
})
</script>

<template>
  <el-tag
    :type="tagType"
    disable-transitions
    size="small"
  >
    {{ state }}
  </el-tag>
</template>
```

`frontend/src/components/infra/ExecutorRow.vue`:

```vue
<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { formatTimeAgo, formatBytes } from '@/utils/format'
import type { ExecutorRead } from '@/api/types'

const props = defineProps<{ executor: ExecutorRead }>()
const { t } = useI18n()

type ElTagType = 'success' | 'warning' | 'danger' | 'info'
const tagType = computed<ElTagType>(() => {
  switch (props.executor.status) {
    case 'healthy': return 'success'
    case 'degraded': return 'warning'
    case 'suspect': return 'warning'
    case 'faulty': return 'danger'
    default: return 'info'
  }
})

const diskPct = computed(() => {
  const free = props.executor.disk_free_gb
  const total = props.executor.disk_total_gb
  if (free === null || total === null || total <= 0) return null
  return Math.max(0, Math.min(100, Math.round((1 - free / total) * 100)))
})

function nicLabel(): string {
  const n = props.executor.nic_speed_gbps
  return n === null ? '—' : `${n} ${t('executors.gbps')}`
}
function diskLabel(): string {
  const free = props.executor.disk_free_gb
  const total = props.executor.disk_total_gb
  if (free === null || total === null) return '—'
  return `${formatBytes(free * 1024 ** 3)} / ${formatBytes(total * 1024 ** 3)}`
}
</script>

<template>
  <div class="exec-row">
    <span class="eid">{{ executor.id }}</span>
    <el-tag
      :type="tagType"
      size="small"
      disable-transitions
    >
      {{ executor.status }}
    </el-tag>
    <span class="m">{{ t('executors.health') }}: {{ executor.health_score }}</span>
    <span class="m">{{ t('executors.lastHeartbeat') }}:
      {{ formatTimeAgo(executor.last_heartbeat_at) }}</span>
    <span class="m">NIC: {{ nicLabel() }}</span>
    <span class="m">{{ t('executors.disk') }}: {{ diskLabel() }}
      <span
        v-if="diskPct !== null"
        class="pct"
      >({{ diskPct }}%)</span>
    </span>
  </div>
</template>

<style scoped lang="scss">
.exec-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  font-size: 13px;

  .eid { font-weight: 600; min-width: 160px; }
  .m { color: var(--el-text-color-regular); }
  .pct { color: var(--el-text-color-secondary); margin-left: 4px; }
}
</style>
```

- [ ] **Step 4: Verify + lint + commit**

`cd /d/download_weights/frontend && pnpm test:unit -- HealthPill ExecutorRow` → PASS. `pnpm typecheck` → 0. `pnpm lint:fix && pnpm lint` → OK.
`cd /d/download_weights && git add frontend/src/components/infra/HealthPill.vue frontend/src/components/infra/ExecutorRow.vue frontend/tests/unit/HealthPill.spec.ts frontend/tests/unit/ExecutorRow.spec.ts && git commit -q -m "UI-SP3 M3: HealthPill + ExecutorRow"`

---

### Task 10: `AuditRow`

**Files:**
- Create: `frontend/src/components/infra/AuditRow.vue`
- Test: `frontend/tests/unit/AuditRow.spec.ts`

- [ ] **Step 1: Write the failing test**

```ts
import { describe, expect, test } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import ElementPlus from 'element-plus'
import AuditRow from '@/components/infra/AuditRow.vue'
import en from '@/locale/en-US.json'

const i18n = createI18n({
  legacy: false, locale: 'en-US', messages: { 'en-US': en },
})

describe('AuditRow', () => {
  test('success outcome → success tag, action visible', () => {
    const w = mount(AuditRow, {
      props: {
        entry: {
          id: 1, occurred_at: '2026-05-20T12:00:00Z', tenant_id: 1,
          actor_user_id: 7, actor_ip: '10.0.0.1', action: 'task.created',
          resource_type: 'task', resource_id: 'abcdef1234567890abcdef',
          outcome: 'success', payload: {}, trace_id: 't1',
          prev_hash: null, self_hash: 's',
        },
      },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.text()).toContain('task.created')
    expect(w.text()).toContain('7')
    expect(w.findComponent({ name: 'ElTag' }).props('type')).toBe('success')
    expect(w.text()).toContain('abcdef1234567890') // first 16 chars at least
  })
  test('denied → danger', () => {
    const w = mount(AuditRow, {
      props: {
        entry: {
          id: 2, occurred_at: '2026-05-20T12:00:00Z', tenant_id: 1,
          actor_user_id: null, actor_ip: '', action: 'task.denied',
          resource_type: 'task', resource_id: null, outcome: 'denied',
          payload: {}, trace_id: '', prev_hash: null, self_hash: 's',
        },
      },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.findComponent({ name: 'ElTag' }).props('type')).toBe('danger')
    expect(w.text()).toContain('system')
  })
})
```

- [ ] **Step 2: Run to verify it fails**

`cd /d/download_weights/frontend && pnpm test:unit -- AuditRow`
Expected: FAIL — module not found.

- [ ] **Step 3: Create the component**

```vue
<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { formatDateTime } from '@/utils/format'
import type { AuditEntry } from '@/api/types'

const props = defineProps<{ entry: AuditEntry }>()
const { t } = useI18n()

type ElTagType = 'success' | 'warning' | 'danger' | 'info'
const tagType = computed<ElTagType>(() => {
  if (props.entry.outcome === 'success') return 'success'
  if (props.entry.outcome === 'denied') return 'danger'
  if (props.entry.outcome === 'error') return 'warning'
  return 'info'
})
const actorLabel = computed(() =>
  props.entry.actor_user_id === null
    ? t('audit.systemActor')
    : String(props.entry.actor_user_id))
const shortId = computed(() => {
  const r = props.entry.resource_id
  return r === null ? '—' : (r.length > 16 ? `${r.slice(0, 16)}…` : r)
})
</script>

<template>
  <div class="audit-row">
    <span class="ts">{{ formatDateTime(entry.occurred_at) }}</span>
    <el-tag
      :type="tagType"
      size="small"
      disable-transitions
    >
      {{ entry.outcome }}
    </el-tag>
    <span class="actor">{{ actorLabel }}</span>
    <span class="action">{{ entry.action }}</span>
    <span class="rtype">{{ entry.resource_type }}</span>
    <span
      class="rid"
      :title="entry.resource_id ?? ''"
    >{{ shortId }}</span>
  </div>
</template>

<style scoped lang="scss">
.audit-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
  font-size: 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);

  .ts {
    color: var(--el-text-color-secondary);
    min-width: 168px;
    font-variant-numeric: tabular-nums;
  }
  .actor { color: var(--el-text-color-regular); min-width: 60px; }
  .action { color: var(--el-text-color-primary); font-weight: 500; }
  .rtype { color: var(--el-text-color-regular); }
  .rid {
    color: var(--el-text-color-secondary);
    font-family: var(--el-font-family-monospace, monospace);
  }
}
</style>
```

- [ ] **Step 4: Verify + lint + commit**

`cd /d/download_weights/frontend && pnpm test:unit -- AuditRow` → PASS. `pnpm typecheck` → 0. `pnpm lint:fix && pnpm lint` → OK.
`cd /d/download_weights && git add frontend/src/components/infra/AuditRow.vue frontend/tests/unit/AuditRow.spec.ts && git commit -q -m "UI-SP3 M3: AuditRow"`

---

### Task 11: `QuotaCard`

**Files:**
- Create: `frontend/src/components/infra/QuotaCard.vue`
- Test: `frontend/tests/unit/QuotaCard.spec.ts`

- [ ] **Step 1: Write the failing test**

```ts
import { describe, expect, test } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import ElementPlus from 'element-plus'
import QuotaCard from '@/components/infra/QuotaCard.vue'
import en from '@/locale/en-US.json'

const i18n = createI18n({
  legacy: false, locale: 'en-US', messages: { 'en-US': en },
})

describe('QuotaCard', () => {
  test('renders label, formatted used/quota, percent', () => {
    const w = mount(QuotaCard, {
      props: { label: 'bytes', used: 1024 * 1024, quota: 2 * 1024 * 1024,
        format: 'bytes' },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.text()).toContain('1.0 MB')
    expect(w.text()).toContain('2.0 MB')
    expect(w.text()).toContain('50%')
  })
  test('over-threshold → warning chip', () => {
    const w = mount(QuotaCard, {
      props: { label: 'concurrent', used: 9, quota: 10, format: 'count' },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.text()).toContain('90%')
    // Pre-review BLOCKER fix: Task 11 (this component) runs BEFORE Task 12
    // adds the i18n keys, so we cannot deref `en.quotaPage.threshold.warn`
    // (would throw at test-collection time). Assert against the literal
    // i18n key path that vue-i18n returns on a missing key.
    expect(w.text()).toContain('quotaPage.threshold.warn')
  })
  test('over-cap → over chip + 100%', () => {
    const w = mount(QuotaCard, {
      props: { label: 'bytes', used: 200, quota: 100, format: 'bytes' },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.text()).toContain('100%')
    expect(w.text()).toContain('quotaPage.threshold.over')
  })
  test('zero quota → renders 0% (no NaN)', () => {
    const w = mount(QuotaCard, {
      props: { label: 'x', used: 0, quota: 0, format: 'count' },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.text()).toContain('0%')
  })
})
```

- [ ] **Step 2: Run to verify it fails**

`cd /d/download_weights/frontend && pnpm test:unit -- QuotaCard`
Expected: FAIL.

- [ ] **Step 3: Create the component**

```vue
<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { formatBytes } from '@/utils/format'

type Fmt = 'bytes' | 'gb' | 'count'
const props = defineProps<{
  label: string
  used: number
  quota: number
  format: Fmt
}>()
const { t } = useI18n()

const pct = computed(() => {
  if (props.quota <= 0) return 0
  return Math.min(100, Math.round((props.used / props.quota) * 100))
})
const showWarn = computed(() => pct.value >= 85 && pct.value < 100)
const showOver = computed(() => pct.value >= 100)
function fmt(n: number): string {
  if (props.format === 'bytes') return formatBytes(n)
  if (props.format === 'gb') return formatBytes(n * 1024 ** 3)
  return String(n)
}
</script>

<template>
  <div class="quota-card">
    <div class="head">
      <span class="lbl">{{ label }}</span>
      <span
        v-if="showOver"
        class="chip over"
      >{{ t('quotaPage.threshold.over') }}</span>
      <span
        v-else-if="showWarn"
        class="chip warn"
      >{{ t('quotaPage.threshold.warn') }}</span>
    </div>
    <div class="val">
      <span class="used">{{ fmt(used) }}</span>
      <span class="sep">/</span>
      <span class="quota">{{ fmt(quota) }}</span>
      <span class="pct">{{ pct }}%</span>
    </div>
    <el-progress
      :percentage="pct"
      :status="showOver ? 'exception' : (showWarn ? 'warning' : undefined)"
      :show-text="false"
    />
  </div>
</template>

<style scoped lang="scss">
.quota-card {
  padding: 16px;
  border-radius: 6px;
  background: var(--el-fill-color-lighter);

  .head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    .lbl { font-size: 13px; color: var(--el-text-color-regular); }
    .chip {
      font-size: 11px;
      padding: 2px 8px;
      border-radius: 10px;
      &.warn { background: var(--el-color-warning-light-8);
               color: var(--el-color-warning); }
      &.over { background: var(--el-color-danger-light-8);
               color: var(--el-color-danger); }
    }
  }
  .val {
    margin: 8px 0;
    display: flex;
    align-items: baseline;
    gap: 6px;
    .used { font-size: 20px; font-weight: 600;
            color: var(--el-text-color-primary); }
    .sep { color: var(--el-text-color-secondary); }
    .quota { color: var(--el-text-color-regular); }
    .pct {
      margin-left: auto;
      font-size: 13px;
      color: var(--el-text-color-regular);
    }
  }
}
</style>
```

- [ ] **Step 4: Verify + lint + commit**

`cd /d/download_weights/frontend && pnpm test:unit -- QuotaCard` → PASS. `pnpm typecheck` → 0. `pnpm lint:fix && pnpm lint` → OK.
`cd /d/download_weights && git add frontend/src/components/infra/QuotaCard.vue frontend/tests/unit/QuotaCard.spec.ts && git commit -q -m "UI-SP3 M3: QuotaCard"`

---

# Milestone M4 — Pages + i18n + nav + smoke + docs

### Task 12: i18n — add 5 blocks to both locales (parity)

**Files:**
- Modify: `frontend/src/locale/en-US.json`, `frontend/src/locale/zh-CN.json`

- [ ] **Step 1: Append to `en-US.json` `nav` block**

Replace the existing `"nav"` block contents with:

```json
  "nav": { "dashboard": "Overview", "tasks": "Tasks", "createTask": "New task",
    "executors": "Executors", "audit": "Audit log",
    "quota": "Quota", "settings": "Settings" },
```

(Add the 4 new keys after `createTask`, keeping the existing 3.)

- [ ] **Step 2: Append the 4 new top-level blocks to `en-US.json`**

After the `"errors"` block (the last top-level block, near the end of the file), add a comma and:

```json
  "executors": {
    "heading": "Executors", "empty": "No executors visible",
    "filterStatus": "Status", "all": "All",
    "joining": "joining", "healthy": "healthy", "degraded": "degraded",
    "suspect": "suspect", "faulty": "faulty",
    "health": "Health", "lastHeartbeat": "Heartbeat",
    "disk": "Disk", "gbps": "Gbps",
    "host": "Host", "tenant": "Tenant", "shared": "shared infra"
  },
  "audit": {
    "heading": "Audit log", "empty": "No audit entries",
    "filterAction": "Action prefix", "filterActor": "Actor user id",
    "filterFrom": "From", "filterTo": "To", "reset": "Reset filters",
    "loadOlder": "Load older", "systemActor": "system"
  },
  "quotaPage": {
    "heading": "Quota", "byteUsage": "Bytes this month",
    "storageUsage": "Storage", "concurrentUsage": "Concurrent tasks",
    "threshold": { "warn": "WARN", "over": "OVER" }
  },
  "settings": {
    "heading": "Settings", "profile": "Profile", "preferences": "Preferences",
    "system": "System",
    "principal": { "user": "User", "tenant": "Tenant", "role": "Role",
      "projects": "Projects", "serviceToken": "Service token" },
    "theme": "Theme", "themeLight": "Light", "themeDark": "Dark",
    "localeLabel": "Language", "controllerState": "Controller state"
  }
```

- [ ] **Step 3: Mirror exactly in `zh-CN.json` (same nesting, same keys)**

Same structure with Chinese strings:

```json
  "nav": { "dashboard": "概览", "tasks": "任务", "createTask": "新建任务",
    "executors": "执行节点", "audit": "审计日志",
    "quota": "配额", "settings": "设置" },
```

And the 4 new top-level blocks:

```json
  "executors": {
    "heading": "执行节点", "empty": "无可见执行节点",
    "filterStatus": "状态", "all": "全部",
    "joining": "加入中", "healthy": "健康", "degraded": "降级",
    "suspect": "可疑", "faulty": "故障",
    "health": "健康分", "lastHeartbeat": "心跳",
    "disk": "磁盘", "gbps": "Gbps",
    "host": "主机", "tenant": "租户", "shared": "共享基础设施"
  },
  "audit": {
    "heading": "审计日志", "empty": "暂无审计记录",
    "filterAction": "动作前缀", "filterActor": "操作者 user id",
    "filterFrom": "起始时间", "filterTo": "结束时间", "reset": "重置筛选",
    "loadOlder": "加载更早", "systemActor": "系统"
  },
  "quotaPage": {
    "heading": "配额", "byteUsage": "本月流量",
    "storageUsage": "存储", "concurrentUsage": "并发任务",
    "threshold": { "warn": "警告", "over": "超限" }
  },
  "settings": {
    "heading": "设置", "profile": "个人资料", "preferences": "偏好设置",
    "system": "系统",
    "principal": { "user": "用户", "tenant": "租户", "role": "角色",
      "projects": "项目", "serviceToken": "服务 token" },
    "theme": "主题", "themeLight": "浅色", "themeDark": "深色",
    "localeLabel": "语言", "controllerState": "控制器状态"
  }
```

- [ ] **Step 4: Verify parity + commit**

`cd /d/download_weights/frontend && pnpm test:unit -- localeParity` → PASS (key sets identical). `pnpm typecheck` → 0. `pnpm lint:fix && pnpm lint` → OK.
`cd /d/download_weights && git add frontend/src/locale/en-US.json frontend/src/locale/zh-CN.json && git commit -q -m "UI-SP3 M4: i18n — nav + executors + audit + quotaPage + settings (en/zh parity)"`

---

### Task 13: Executors page

**Files:**
- Create: `frontend/src/pages/Executors.vue`, `frontend/tests/unit/ExecutorsPage.spec.ts`

- [ ] **Step 1: Write the failing test**

```ts
import { beforeEach, describe, expect, test, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { createI18n } from 'vue-i18n'
import en from '@/locale/en-US.json'

const { execData } = vi.hoisted(() => ({
  execData: { value: null as unknown },
}))

vi.mock('@/composables/useExecutors', async () => {
  const { ref } = await import('vue')
  return {
    useExecutors: () => ({
      data: ref(execData.value), isLoading: ref(false),
      isError: ref(false), error: ref(null),
    }),
  }
})

const i18n = createI18n({
  legacy: false, locale: 'en-US', messages: { 'en-US': en },
})
function mountPage() {
  return import('@/pages/Executors.vue').then((m) =>
    mount(m.default, { global: { plugins: [ElementPlus, i18n] } }))
}

describe('Executors page', () => {
  beforeEach(() => { setActivePinia(createPinia()); execData.value = null })
  test('no data → empty', async () => {
    const w = await mountPage()
    await flushPromises()
    expect(w.findComponent({ name: 'EmptyState' }).exists()).toBe(true)
  })
  test('data present → host-grouped rows render', async () => {
    execData.value = {
      items: [
        { id: 'h1-w1', status: 'healthy', health_score: 95, epoch: 1,
          host_id: 'h1', tenant_id: 1, last_heartbeat_at: null,
          nic_speed_gbps: 10, disk_free_gb: null, disk_total_gb: null,
          created_at: null },
        { id: 'h1-w2', status: 'degraded', health_score: 60, epoch: 1,
          host_id: 'h1', tenant_id: 1, last_heartbeat_at: null,
          nic_speed_gbps: 10, disk_free_gb: null, disk_total_gb: null,
          created_at: null },
        { id: 'h2-w1', status: 'healthy', health_score: 100, epoch: 1,
          host_id: 'h2', tenant_id: null, last_heartbeat_at: null,
          nic_speed_gbps: null, disk_free_gb: null, disk_total_gb: null,
          created_at: null },
      ],
    }
    const w = await mountPage()
    await flushPromises()
    expect(w.findAllComponents({ name: 'ExecutorRow' }).length).toBe(3)
    expect(w.text()).toContain('h1')
    expect(w.text()).toContain('h2')
  })
})
```

- [ ] **Step 2: Run to verify it fails**

`cd /d/download_weights/frontend && pnpm test:unit -- ExecutorsPage`
Expected: FAIL.

- [ ] **Step 3: Create the page**

`frontend/src/pages/Executors.vue`:

```vue
<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import DataBoundary from '@/components/DataBoundary.vue'
import ExecutorRow from '@/components/infra/ExecutorRow.vue'
import { useExecutors } from '@/composables/useExecutors'
import type { ExecutorRead } from '@/api/types'

const { t } = useI18n()
const statusFilter = ref<string | null>(null)
const { data, isLoading, isError } = useExecutors(statusFilter)

const grouped = computed<Array<{ host: string; items: ExecutorRead[] }>>(() => {
  const items = data.value?.items ?? []
  const by: Record<string, ExecutorRead[]> = {}
  for (const e of items) {
    const k = e.host_id ?? '—'
    if (!by[k]) by[k] = []
    by[k].push(e)
  }
  return Object.keys(by).sort().map((h) => ({
    host: h, items: by[h] ?? [],
  }))
})
</script>

<template>
  <div class="page-container">
    <h2>{{ t('executors.heading') }}</h2>

    <div class="bar">
      <el-select
        v-model="statusFilter"
        :placeholder="t('executors.filterStatus')"
        clearable
        size="small"
        style="width: 180px"
      >
        <el-option
          v-for="s in ['joining','healthy','degraded','suspect','faulty']"
          :key="s"
          :value="s"
          :label="t(`executors.${s}`)"
        />
      </el-select>
    </div>

    <DataBoundary
      :loading="isLoading"
      :error="isError"
      :is-empty="grouped.length === 0"
      :empty-message="t('executors.empty')"
      style="margin-top: 16px"
    >
      <div
        v-for="g in grouped"
        :key="g.host"
        class="host-group"
      >
        <div class="host-hdr">
          <span class="hk">{{ t('executors.host') }}</span>
          <span class="hv">{{ g.host }}</span>
          <span class="hn">({{ g.items.length }})</span>
        </div>
        <ExecutorRow
          v-for="e in g.items"
          :key="e.id"
          :executor="e"
        />
      </div>
    </DataBoundary>
  </div>
</template>

<style scoped lang="scss">
.bar { margin-top: 16px; }
.host-group {
  margin-top: 16px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  overflow: hidden;
}
.host-hdr {
  padding: 8px 12px;
  background: var(--el-fill-color-lighter);
  display: flex;
  gap: 8px;
  align-items: center;
  font-size: 13px;
  .hk { color: var(--el-text-color-secondary); }
  .hv { font-weight: 600; }
  .hn { color: var(--el-text-color-secondary); }
}
</style>
```

- [ ] **Step 4: Verify + lint + commit**

`cd /d/download_weights/frontend && pnpm test:unit -- ExecutorsPage` → PASS. `pnpm typecheck` → 0. `pnpm lint:fix && pnpm lint` → OK.
`cd /d/download_weights && git add frontend/src/pages/Executors.vue frontend/tests/unit/ExecutorsPage.spec.ts && git commit -q -m "UI-SP3 M4: Executors page (host-grouped, status filter)"`

---

### Task 14: Audit page

**Files:**
- Create: `frontend/src/pages/Audit.vue`, `frontend/tests/unit/AuditPage.spec.ts`

- [ ] **Step 1: Write the failing test**

```ts
import { beforeEach, describe, expect, test, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { createI18n } from 'vue-i18n'
import en from '@/locale/en-US.json'

const { auditData } = vi.hoisted(() => ({
  auditData: { value: null as unknown },
}))

vi.mock('@/composables/useAuditLog', async () => {
  const { ref } = await import('vue')
  return {
    useAuditLog: () => ({
      data: ref(auditData.value), isLoading: ref(false),
      isError: ref(false), error: ref(null),
    }),
    fetchOlderAudit: vi.fn(),
  }
})

const i18n = createI18n({
  legacy: false, locale: 'en-US', messages: { 'en-US': en },
})
function mountPage() {
  return import('@/pages/Audit.vue').then((m) =>
    mount(m.default, { global: { plugins: [ElementPlus, i18n] } }))
}

describe('Audit page', () => {
  beforeEach(() => { setActivePinia(createPinia()); auditData.value = null })
  test('no data → empty', async () => {
    const w = await mountPage()
    await flushPromises()
    expect(w.findComponent({ name: 'EmptyState' }).exists()).toBe(true)
  })
  test('data present → audit rows render + load older shown', async () => {
    auditData.value = {
      items: [
        { id: 1, occurred_at: '2026-05-20T12:00:00Z', tenant_id: 1,
          actor_user_id: 7, actor_ip: '', action: 'task.created',
          resource_type: 'task', resource_id: 'r', outcome: 'success',
          payload: {}, trace_id: '', prev_hash: null, self_hash: 's' },
      ],
      next_cursor: 'NEXT',
    }
    const w = await mountPage()
    await flushPromises()
    expect(w.findAllComponents({ name: 'AuditRow' }).length).toBe(1)
    expect(w.text()).toContain(en.audit.loadOlder)
  })
})
```

- [ ] **Step 2: Run to verify it fails**

`cd /d/download_weights/frontend && pnpm test:unit -- AuditPage`
Expected: FAIL.

- [ ] **Step 3: Create the page**

`frontend/src/pages/Audit.vue`:

```vue
<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import DataBoundary from '@/components/DataBoundary.vue'
import AuditRow from '@/components/infra/AuditRow.vue'
import { useAuditLog, fetchOlderAudit } from '@/composables/useAuditLog'
import type { AuditEntry } from '@/api/types'

const { t } = useI18n()
const action = ref('')
const actor = ref<number | null>(null)
const from = ref<string | null>(null)
const to = ref<string | null>(null)

const { data, isLoading, isError } = useAuditLog({ action, actor, from, to })
const older = ref<AuditEntry[]>([])
const all = computed<AuditEntry[]>(() =>
  [...(data.value?.items ?? []), ...older.value])
const nextCursor = computed(() => data.value?.next_cursor ?? null)
const loadingOlder = ref(false)

async function loadOlder() {
  if (!nextCursor.value) return
  loadingOlder.value = true
  try {
    const page = await fetchOlderAudit({
      action: action.value, actor: actor.value,
      from: from.value, to: to.value,
    }, nextCursor.value)
    older.value = [...older.value, ...page.items]
  } finally {
    loadingOlder.value = false
  }
}

function reset() {
  action.value = ''
  actor.value = null
  from.value = null
  to.value = null
  older.value = []
}
</script>

<template>
  <div class="page-container">
    <h2>{{ t('audit.heading') }}</h2>
    <div class="bar">
      <el-input
        v-model="action"
        :placeholder="t('audit.filterAction')"
        size="small"
        style="width: 200px"
        clearable
      />
      <el-input-number
        v-model="actor"
        :placeholder="t('audit.filterActor')"
        size="small"
        :min="1"
        :step="1"
        :precision="0"
        controls-position="right"
        style="width: 180px"
      />
      <el-date-picker
        v-model="from"
        type="datetime"
        :placeholder="t('audit.filterFrom')"
        size="small"
        value-format="YYYY-MM-DDTHH:mm:ss[Z]"
      />
      <el-date-picker
        v-model="to"
        type="datetime"
        :placeholder="t('audit.filterTo')"
        size="small"
        value-format="YYYY-MM-DDTHH:mm:ss[Z]"
      />
      <el-button
        size="small"
        @click="reset"
      >
        {{ t('audit.reset') }}
      </el-button>
    </div>

    <DataBoundary
      :loading="isLoading"
      :error="isError"
      :is-empty="all.length === 0"
      :empty-message="t('audit.empty')"
      style="margin-top: 16px"
    >
      <AuditRow
        v-for="(e, i) in all"
        :key="`${e.id}-${i}`"
        :entry="e"
      />
      <div
        v-if="nextCursor"
        class="load-older"
      >
        <el-button
          :loading="loadingOlder"
          @click="loadOlder"
        >
          {{ t('audit.loadOlder') }}
        </el-button>
      </div>
    </DataBoundary>
  </div>
</template>

<style scoped lang="scss">
.bar {
  margin-top: 16px;
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  align-items: center;
}
.load-older {
  text-align: center;
  margin-top: 12px;
}
</style>
```

- [ ] **Step 4: Verify + lint + commit**

`cd /d/download_weights/frontend && pnpm test:unit -- AuditPage` → PASS. `pnpm typecheck` → 0. `pnpm lint:fix && pnpm lint` → OK.
`cd /d/download_weights && git add frontend/src/pages/Audit.vue frontend/tests/unit/AuditPage.spec.ts && git commit -q -m "UI-SP3 M4: Audit page (filters + cursor-paginated)"`

---

### Task 15: Quota + Settings pages + router/nav additions

**Files:**
- Create: `frontend/src/pages/QuotaPage.vue`, `frontend/src/pages/Settings.vue`
- Modify: `frontend/src/router/index.ts`, `frontend/src/nav/registry.ts`
- Test: `frontend/tests/unit/QuotaSettingsPage.spec.ts`

- [ ] **Step 1: Write the failing test**

```ts
import { beforeEach, describe, expect, test, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { createI18n } from 'vue-i18n'
import en from '@/locale/en-US.json'

const { quotaData, healthData } = vi.hoisted(() => ({
  quotaData: { value: null as unknown },
  healthData: { value: null as unknown },
}))

vi.mock('@/composables/useQuota', async () => {
  const { ref } = await import('vue')
  return {
    useQuota: () => ({
      data: ref(quotaData.value), isLoading: ref(false),
      isError: ref(false), error: ref(null),
    }),
  }
})
vi.mock('@/composables/useSystemHealth', async () => {
  const { ref } = await import('vue')
  return {
    useSystemHealth: () => ({
      data: ref(healthData.value), isLoading: ref(false),
      isError: ref(false), error: ref(null),
    }),
  }
})

const b64 = (o: unknown) => btoa(JSON.stringify(o)).replace(/=+$/, '')
const i18n = createI18n({
  legacy: false, locale: 'en-US', messages: { 'en-US': en },
})

describe('Quota + Settings pages', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    quotaData.value = null
    healthData.value = null
  })
  test('QuotaPage with data → 3 QuotaCards', async () => {
    quotaData.value = {
      tenant_id: 1, bytes_used_month: 1024, bytes_quota_month: 2048,
      storage_gb_used: 1, storage_gb_quota: 10,
      concurrent_tasks: 1, concurrent_quota: 5,
    }
    const m = await import('@/pages/QuotaPage.vue')
    const w = mount(m.default, {
      global: { plugins: [ElementPlus, i18n] },
    })
    await flushPromises()
    expect(w.findAllComponents({ name: 'QuotaCard' }).length).toBe(3)
  })
  test('QuotaPage no data → empty', async () => {
    const m = await import('@/pages/QuotaPage.vue')
    const w = mount(m.default, {
      global: { plugins: [ElementPlus, i18n] },
    })
    await flushPromises()
    expect(w.findComponent({ name: 'EmptyState' }).exists()).toBe(true)
  })
  test('Settings shows principal + system state', async () => {
    const { useAuthStore } = await import('@/stores/auth')
    useAuthStore().login(`h.${b64({ sub: '7', tid: 3, role: 'tenant_admin',
      pids: [9, 11] })}.s`)
    healthData.value = { status: 'active', controller_state: 'active' }
    const m = await import('@/pages/Settings.vue')
    const w = mount(m.default, {
      global: { plugins: [ElementPlus, i18n] },
    })
    await flushPromises()
    expect(w.text()).toContain('7') // user id
    expect(w.text()).toContain('3') // tenant id
    expect(w.text()).toContain('tenant_admin')
    expect(w.findComponent({ name: 'HealthPill' }).exists()).toBe(true)
  })
})
```

- [ ] **Step 2: Run to verify it fails**

`cd /d/download_weights/frontend && pnpm test:unit -- QuotaSettingsPage`
Expected: FAIL.

- [ ] **Step 3: Create `QuotaPage.vue`**

```vue
<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import DataBoundary from '@/components/DataBoundary.vue'
import QuotaCard from '@/components/infra/QuotaCard.vue'
import { useQuota } from '@/composables/useQuota'

const { t } = useI18n()
const { data, isLoading, isError } = useQuota()
</script>

<template>
  <div class="page-container">
    <h2>{{ t('quotaPage.heading') }}</h2>
    <DataBoundary
      :loading="isLoading"
      :error="isError"
      :is-empty="!data"
      :empty-message="t('errors.service_unavailable')"
      style="margin-top: 16px"
    >
      <template v-if="data">
        <div class="grid">
          <QuotaCard
            :label="t('quotaPage.byteUsage')"
            :used="data.bytes_used_month"
            :quota="data.bytes_quota_month"
            format="bytes"
          />
          <QuotaCard
            :label="t('quotaPage.storageUsage')"
            :used="data.storage_gb_used"
            :quota="data.storage_gb_quota"
            format="gb"
          />
          <QuotaCard
            :label="t('quotaPage.concurrentUsage')"
            :used="data.concurrent_tasks"
            :quota="data.concurrent_quota"
            format="count"
          />
        </div>
      </template>
    </DataBoundary>
  </div>
</template>

<style scoped lang="scss">
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 16px;
  margin-top: 16px;
}
</style>
```

- [ ] **Step 4: Create `Settings.vue`**

```vue
<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { useSessionStore } from '@/stores/session'
import { useUiStore } from '@/stores/ui'
import { useSystemHealth } from '@/composables/useSystemHealth'
import HealthPill from '@/components/infra/HealthPill.vue'

const { t } = useI18n()
const session = useSessionStore()
const ui = useUiStore()
const { data: health } = useSystemHealth()
</script>

<template>
  <div class="page-container">
    <h2>{{ t('settings.heading') }}</h2>

    <el-card class="card">
      <h3>{{ t('settings.profile') }}</h3>
      <el-descriptions
        :column="1"
        size="small"
        border
      >
        <el-descriptions-item :label="t('settings.principal.user')">
          {{ session.principal?.userId ?? '—' }}
        </el-descriptions-item>
        <el-descriptions-item :label="t('settings.principal.tenant')">
          {{ session.principal?.tenantId ?? '—' }}
        </el-descriptions-item>
        <el-descriptions-item :label="t('settings.principal.role')">
          {{ session.principal?.role ?? '—' }}
        </el-descriptions-item>
        <el-descriptions-item :label="t('settings.principal.projects')">
          {{ (session.principal?.projectIds ?? []).join(', ') || '—' }}
        </el-descriptions-item>
        <el-descriptions-item :label="t('settings.principal.serviceToken')">
          {{ session.isServiceToken ? 'yes' : 'no' }}
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-card class="card">
      <h3>{{ t('settings.preferences') }}</h3>
      <div class="row">
        <span class="lbl">{{ t('settings.theme') }}</span>
        <el-switch
          :model-value="ui.theme === 'dark'"
          :active-text="t('settings.themeDark')"
          :inactive-text="t('settings.themeLight')"
          @change="ui.toggleTheme()"
        />
      </div>
      <div class="row">
        <span class="lbl">{{ t('settings.localeLabel') }}</span>
        <el-radio-group
          :model-value="ui.locale"
          @update:model-value="(v: string | number | boolean | undefined) =>
            ui.setLocale(String(v) as 'en-US' | 'zh-CN')"
        >
          <el-radio value="en-US">
            English
          </el-radio>
          <el-radio value="zh-CN">
            中文
          </el-radio>
        </el-radio-group>
      </div>
    </el-card>

    <el-card class="card">
      <h3>{{ t('settings.system') }}</h3>
      <div class="row">
        <span class="lbl">{{ t('settings.controllerState') }}</span>
        <HealthPill :state="health?.controller_state ?? 'unknown'" />
      </div>
    </el-card>
  </div>
</template>

<style scoped lang="scss">
.card { margin-top: 16px; }
.row {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-top: 8px;
  .lbl { min-width: 140px; color: var(--el-text-color-regular); }
}
</style>
```

- [ ] **Step 5: Add 4 routes to `frontend/src/router/index.ts`**

After the existing `taskDetail` route (the one with `path: '/tasks/:id'`) and **before** the catch-all `'/:pathMatch(.*)*'`, insert:

```ts
  {
    path: '/executors', name: 'executors',
    component: () => import('@/pages/Executors.vue'),
  },
  {
    path: '/audit', name: 'audit',
    component: () => import('@/pages/Audit.vue'),
  },
  {
    path: '/quota', name: 'quota',
    component: () => import('@/pages/QuotaPage.vue'),
  },
  {
    path: '/settings', name: 'settings',
    component: () => import('@/pages/Settings.vue'),
  },
```

- [ ] **Step 6: Add 4 nav items to `frontend/src/nav/registry.ts`**

Replace the `NAV_ITEMS` constant with:

```ts
export const NAV_ITEMS: NavItem[] = [
  { route: 'dashboard', labelKey: 'nav.dashboard', icon: 'Odometer' },
  { route: 'taskList', labelKey: 'nav.tasks', icon: 'List' },
  { route: 'taskCreate', labelKey: 'nav.createTask', icon: 'Plus' },
  { route: 'executors', labelKey: 'nav.executors', icon: 'Monitor' },
  { route: 'audit', labelKey: 'nav.audit', icon: 'Document' },
  { route: 'quota', labelKey: 'nav.quota', icon: 'DataLine' },
  { route: 'settings', labelKey: 'nav.settings', icon: 'Setting' },
]
```

- [ ] **Step 7: Verify + lint + commit**

`cd /d/download_weights/frontend && pnpm test:unit -- QuotaSettingsPage` → PASS. `pnpm typecheck` → 0. `pnpm lint:fix && pnpm lint` → OK.
`cd /d/download_weights && git add frontend/src/pages/QuotaPage.vue frontend/src/pages/Settings.vue frontend/src/router/index.ts frontend/src/nav/registry.ts frontend/tests/unit/QuotaSettingsPage.spec.ts && git commit -q -m "UI-SP3 M4: Quota + Settings pages + router/nav additions"`

---

### Task 16: M4 full gate + headed Playwright smoke + docs

**Files:** `docs/operator/web-ui.md` (modify).

- [ ] **Step 1: Full backend suite**

`uv run pytest tests/ -q` → 0 failures (prior 439 + 8 new).

- [ ] **Step 2: Full frontend gate**

`cd /d/download_weights/frontend && pnpm test:unit && pnpm typecheck && pnpm lint && pnpm build` → all green.

- [ ] **Step 3: OpenAPI + invariant + status-write lint**

```bash
npx --yes @stoplight/spectral-cli lint api/openapi.yaml --fail-severity=error
npx --yes @apidevtools/swagger-cli validate api/openapi.yaml
python tools/lint_invariants.py
python tools/lint_no_direct_status_write.py
```
All exit 0.

- [ ] **Step 4: Headed Playwright smoke**

Per SP2-codified recipe (record in memory): start a fresh ephemeral controller on `:8011` (current SP3 code, same dev DB, plain HTTP):

```bash
(DLW_AUTH_DEV_MODE=true DLW_SYSTEM_ADMIN_TOKEN=local-admin-token \
 DLW_ENROLLMENT_TOKEN=local-enroll-token \
 DLW_SYSTEM_JWT_SECRET=dev-system-jwt-change-me \
 DLW_CONTROLLER_HOSTNAME=localhost \
 nohup uv run uvicorn dlw.main:create_app --factory \
 --host 127.0.0.1 --port 8011 \
 > .run/logs/controller-8011-sp3.log 2>&1 &)
```

Wait for `curl -s -o /dev/null -w '%{http_code}' http://localhost:8011/health/live` = 200.

Restart Vite proxying to `:8011`:

```bash
PID=$(netstat -ano | grep ":5173" | grep LISTENING | head -1 | awk '{print $NF}')
[ -n "$PID" ] && taskkill //F //PID $PID
cd /d/download_weights/frontend
# Then launch with Bash run_in_background:true:
#   DLW_API_PROXY=http://localhost:8011 pnpm dev > /tmp/vite-sp3.log 2>&1
# Wait until http://localhost:5173 returns 200.
```

Mint a tenant token:

```bash
uv run python -c "from dlw.auth.principal import issue_system_jwt; \
 print(issue_system_jwt(secret='dev-system-jwt-change-me', user_id=1, \
 tenant_id=1, role='tenant_admin', project_ids=[]))" > .run/sp3-token.txt
```

Create `.run/pw/sp3-smoke.mjs`:

```js
import { chromium } from 'playwright'
import { readFileSync } from 'node:fs'

const TOKEN = readFileSync('.run/sp3-token.txt', 'utf8').trim()
const errors = []
const b = await chromium.launch({ headless: false })
const pg = await b.newPage()
pg.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
pg.on('pageerror', (e) => errors.push(String(e)))

await pg.goto('http://localhost:5173/login')
await pg.fill('input', TOKEN)
await pg.click('button[type="submit"]')
await pg.waitForURL('**/')

for (const [name, path] of [
  ['executors', '/executors'],
  ['audit', '/audit'],
  ['quota', '/quota'],
  ['settings', '/settings'],
]) {
  await pg.goto(`http://localhost:5173${path}`)
  await pg.waitForTimeout(1500)
  await pg.screenshot({ path: `.run/pw/sp3-${name}.png` })
}

await b.close()
if (errors.length) {
  console.log('SP3 smoke: console/page errors:\n' + errors.join('\n'))
  process.exit(1)
}
console.log('SP3 smoke OK')
```

Run: `node .run/pw/sp3-smoke.mjs` → prints `SP3 smoke OK`. Record the outcome in the task notes (does not block on local-stack issues — note explicitly).

- [ ] **Step 5: Append docs to `docs/operator/web-ui.md`**

After the SP2 section, append:

```markdown

## UI-SP3 — Infrastructure & Governance

Four new pages backed by **two additive read-only endpoints** (zero migration):

- `GET /api/v1/audit/log` — tenant-scoped audit search (filters: action prefix,
  actor_user_id, from/to time range; cursor-paginated; matches the on-disk
  `searchAuditLog` contract).
- `GET /api/v1/executors` — browser-facing executor list (own-tenant +
  shared-infra view; `system_admin` sees all). Lives in a new module
  (`src/dlw/api/executors_read.py`) because the existing `api/executors.py` is
  mTLS-only per `tools/lint_invariants.py:check_no_bearer_on_executor_routes`.

Pages: **/executors** (host-grouped list + status filter), **/audit** (filterable +
cursor pagination), **/quota** (3 cards over the existing `/quota/current`),
**/settings** (frontend-only: principal info from `stores/session.ts`,
theme/locale from `stores/ui.ts`, controller state from `/health/active`).

**Known deferrals** (intentional, no backend support today): executor
drain/restart, metrics history, heartbeat history; HF-token rotation;
license-policy CRUD; source-driver registration; maintenance mode;
`/quota/usage` (declared but no backing tables); ML forecast; chargeback PDF;
real-time audit tail (UI-SP5).
```

- [ ] **Step 6: Commit (only if fixups needed; docs commit always)**

```bash
cd /d/download_weights && git add docs/operator/web-ui.md && git commit -q -m "UI-SP3 M4: operator docs for the infrastructure & governance pages"
```

---

## Self-Review

**1. Spec coverage:**
- §3.2 endpoints → Tasks 1-3 (audit search, executors list). ✓
- §3.3 service layer (`audit_query.py`, `executors_read.py`) → Tasks 2,3. ✓
- §3.4 DTOs (`schemas/audit.py`, `schemas/executor_read.py`) → Tasks 2,3. ✓
- §3.5 backend tests (happy + tenant isolation + unauth + filters/pagination) → Tasks 2,3. ✓
- §4.1 pages + DataBoundary → Tasks 13,14,15. ✓ §4.2 components → Tasks 9,10,11. ✓ §4.3 composables → Task 7. ✓
- §4.4 `formatDateTime`/`formatTimeAgo` → Task 6. ✓ §4.5 i18n parity → Task 12. ✓
- §4.6 frontend tests (pure + component + page) → Tasks 6,9-15. ✓
- §5 data flow / per-pane DataBoundary → Tasks 13-15. ✓ §6 milestones M1-M4 → tasks grouped with gates (Tasks 4,8,16). ✓
- §1 deferrals (drain/restart, HF token, /quota/usage, etc.) → not implemented, documented in Task 16 docs. ✓
- §7 risks: contract-faithful nullable coercion in DTOs (Task 2, AuditEntryRead); ExecutorRead extended additively (Task 1); tenant filter (Task 3 test asserts cross-tenant exclusion + system_admin bypass); response cursor handle (additive `next_cursor` on the contract — kept additive to support pagination UX). ✓

**2. Placeholder scan:** No "TBD/handle edge cases/similar to Task N". Every code step has complete code.

**3. Type consistency:**
- DTO field names identical across backend (`schemas/audit.py`, `schemas/executor_read.py`), OpenAPI (Task 1 extension), and frontend `types.ts` (Task 5): `AuditEntry/AuditSearchResponse/ExecutorRead/ExecutorListResponse/HealthActive`.
- Composable signatures consistent: `useExecutors(status: Ref<string|null>)`, `useAuditLog({action, actor, from, to})`, `useSystemHealth()` — match across Task 7 + page consumers (Tasks 13,14,15).
- `formatTimeAgo` / `formatDateTime` signatures match between Task 6 and consumers (ExecutorRow Task 9, AuditRow Task 10).
- Backend tenant filter clauses pinned: `AuditLog.tenant_id == principal.tenant_id` (single-column, no parent resource → no 404 path); `Executor.tenant_id IS NULL OR == principal.tenant_id` (with `system_admin`/`is_service` bypass).
- Router 4 new routes (`executors/audit/quota/settings`) align with nav 4 new items in Task 15.
- i18n keys added in Task 12 are referenced by Tasks 9-15 components/pages: `nav.{executors,audit,quota,settings}`, `executors.*`, `audit.*`, `quotaPage.*`, `settings.*`. Parity test in Task 12 catches any drift.
