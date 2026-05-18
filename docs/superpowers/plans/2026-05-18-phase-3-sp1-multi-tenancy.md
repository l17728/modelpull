# Phase 3 SP1 — Multi-Tenancy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the single-tenant PoC into a real multi-tenant platform — OIDC user auth + system-JWT, casbin RBAC, mandatory `tenant_id` scoping on every business query, and per-tenant quota.

**Architecture:** Dependency-layer scoping. A `Principal` is decoded from a system-JWT by `require_principal`; a `require_perm(obj, act)` casbin dependency authorizes; handlers read `principal.tenant_id` (no more hard-coded `_TENANT_ID=1`); a mandatory `tenant_filtered()` helper centralizes the `WHERE tenant_id=` filter; quota is a strong-consistent create check + append-only `usage_records` aggregated each minute into `quota_snapshots` (leader-gated). A `system_admin` service token and an `auth_dev_mode` keep the IdP-free path for tests/CLI.

**Tech Stack:** FastAPI deps, `authlib` (OIDC client, new), `casbin` (RBAC, new), `pyjwt[crypto]` (system-JWT HS256, reused), SQLAlchemy 2 async + asyncpg, alembic, structlog.

**Spec:** `docs/superpowers/specs/2026-05-18-phase-3-sp1-multi-tenancy-design.md`. **Branch:** `feat/phase-3-sp1-multi-tenancy` (created off `main`, spec committed `170a54d`).

**Conventions (match existing code):**
- Service-layer functions do NOT commit; the caller commits (see `dlw/fixtures.py`, scheduler).
- DB tests are marked `@pytest.mark.slow`. They use the session `engine` fixture + `Base.metadata.create_all`.
- API tests use `httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")`.
- `get_settings` is `lru_cache`d — tests env-monkeypatch then `get_settings.cache_clear()`.
- Settings env prefix is `DLW_`.
- New ORM models are registered in `src/dlw/db/models/__init__.py` (imports + `__all__`) — that module's import is what registers them with `Base.metadata`. **Never** add model imports to `src/dlw/db/base.py` (circular import; tables won't be created).
- Terminal-success status across the codebase is **`"succeeded"`** (NOT `"completed"`). Task terminal set = `{"succeeded","failed","cancelled"}`; subtask success = `"succeeded"` (verified: `scheduler.py:156,200,212`; `tools/lint_invariants.py` `VALID_TASK_STATUS`/`VALID_SUBTASK_STATUS`).
- New test dirs need an empty `__init__.py` (siblings `tests/api/ tests/services/ tests/e2e/` all have one; `tests/__init__.py` exists so `from tests.conftest import ...` works).

**Accurate CI gate (verified against `.github/workflows/ci.yml` — there is NO ruff/mypy/`code-vs-yaml`/`information_schema` CI job):**
- The aggregate `ci` job needs: `openapi, helm, shellcheck, markdown, yamllint, security, json, invariant_lint, pytest, frontend-lint, frontend-build`.
- **openapi**: `spectral lint api/openapi.yaml --fail-severity=error` (ruleset `extends: spectral:oas` with `oas3-unused-component: warn`, `operation-tag-defined: warn` — unused components only WARN) + `swagger-cli validate api/openapi.yaml` ($ref check). There is no FastAPI-schema↔yaml diff.
- **yamllint**: scans `deploy/ api/` with `.yamllint.yml` → **`api/openapi.yaml` edits must pass yamllint**.
- **markdown**: markdownlint globs only `docs/v2.0/*.md README.md CONTRIBUTING.md` + lychee link check on `docs/v2.0/**/*.md`+README. The SP1 spec/plan and `docs/operator/*` are NOT CI-markdown-linted (don't worry about them for CI, but keep them clean).
- **invariant_lint**: `python -m pytest tools/test_lint_invariants.py -v` + `python tools/lint_invariants.py` + `python tools/lint_no_direct_status_write.py`. `lint_invariants.py` AST-scans `src/dlw/api/tasks.py`, `services/task_service.py`, `services/scheduler.py` for any task `status` literal not in `VALID_TASK_STATUS` and any `status="..."` kwarg not in `VALID_SUBTASK_STATUS`. **Any code SP1 adds to those 3 files must only use valid status literals** (`"succeeded"` is valid; never introduce `"completed"`).
- **pytest**: `uv sync --all-groups` (uv pinned `0.11.9`, PG16 service on :5432) then `uv run pytest tests/ --cov=src/dlw`. New runtime deps (`authlib`, `casbin`) MUST be added to `pyproject.toml [project] dependencies` AND `uv lock` re-run and `uv.lock` committed, or `uv sync --all-groups` fails in CI.
- `ruff check`/`mypy` are LOCAL pre-commit quality gates only (not CI). Run them, but the PR is not gated on them. The casbin mypy-ignore override is optional/local.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/dlw/config.py` (modify) | new auth/quota settings; remove `bearer_token` |
| `src/dlw/auth/principal.py` (create) | `Principal`, `issue_system_jwt`, `require_principal`, service-token bypass |
| `src/dlw/auth/oidc.py` (create) | OIDC code↔token (real + dev mode), tenant-resolution rules |
| `src/dlw/api/auth.py` (create) | `/auth/login`, `/auth/callback`, `/auth/me` |
| `src/dlw/authz/model.conf`, `policy.csv` (create) | casbin RBAC model + base policy |
| `src/dlw/authz/enforcer.py` (create) | `build_enforcer`, `load_grants`, `require_perm` factory |
| `src/dlw/db/tenant_scope.py` (create) | `tenant_filtered()` mandatory query helper |
| `src/dlw/db/models/usage.py` (create) | `UsageRecord`, `QuotaSnapshot` |
| `src/dlw/db/models/casbin_rule.py` (create) | `CasbinRule` |
| `src/dlw/services/quota.py` (create) | strong-consistent check, `record_usage`, `aggregate_snapshots` |
| `src/dlw/services/audit.py` (create) | thin `write_audit()` helper over existing `AuditLog` |
| `src/dlw/api/quota.py` (create) | `GET /api/v1/quota/current` |
| `src/dlw/api/tasks.py` (modify) | drop constants/bearer; principal + scoped queries + quota gate |
| `src/dlw/main.py` (modify) | mount routers; casbin bootstrap; startup guard; leader-gated aggregator |
| `src/dlw/alembic/versions/<rev>_p3sp1_tenancy_quota.py` (create) | 3 tables + idempotent seed |
| `tests/conftest.py` (modify) | `principal_headers`/`service_headers` fixtures; extend `make_app_with_state` |
| `docs/operator/multi-tenancy.md` (create) | OIDC/service-token/dev-mode/breaking-change note |

---

# Milestone M1 — Identity Core

### Task 1: Config additions + remove `bearer_token`

**Files:**
- Modify: `src/dlw/config.py`
- Test: `tests/test_config.py` (extend)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_sp1_auth_settings_defaults(monkeypatch):
    from dlw.config import get_settings
    get_settings.cache_clear()
    s = get_settings()
    assert s.auth_dev_mode is False
    assert s.system_jwt_secret == "dev-system-jwt-change-me"
    assert s.system_admin_token == ""
    assert s.oidc_issuer == ""
    assert s.oidc_redirect_url.endswith("/api/v1/auth/callback")
    assert s.auth_tenant_rules_json == "[]"
    assert not hasattr(s, "bearer_token")
    get_settings.cache_clear()


def test_sp1_auth_settings_env_override(monkeypatch):
    from dlw.config import get_settings
    monkeypatch.setenv("DLW_AUTH_DEV_MODE", "true")
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", "s3cr3t")
    monkeypatch.setenv("DLW_SYSTEM_ADMIN_TOKEN", "svc-tok")
    get_settings.cache_clear()
    s = get_settings()
    assert s.auth_dev_mode is True
    assert s.system_jwt_secret == "s3cr3t"
    assert s.system_admin_token == "svc-tok"
    get_settings.cache_clear()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py::test_sp1_auth_settings_defaults -v`
Expected: FAIL — `Settings` has no `auth_dev_mode` / still has `bearer_token`.

- [ ] **Step 3: Implement**

In `src/dlw/config.py`, delete the `bearer_token` field + its comment (lines 24-25) and add after the W3c block (after `leader_poll_interval_seconds`):

```python
    # Phase 3 SP1 — multi-tenancy
    auth_dev_mode: bool = Field(default=False)
    system_jwt_secret: str = Field(default="dev-system-jwt-change-me")
    system_admin_token: str = Field(default="")
    oidc_issuer: str = Field(default="")
    oidc_client_id: str = Field(default="")
    oidc_client_secret: str = Field(default="")
    oidc_redirect_url: str = Field(
        default="http://localhost:8000/api/v1/auth/callback"
    )
    auth_tenant_rules_json: str = Field(default="[]")
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_config.py -v`
Expected: PASS (new cases + existing config cases still green).

- [ ] **Step 5: Commit**

```bash
git add src/dlw/config.py tests/test_config.py
git commit -m "feat(sp1): add multi-tenancy config; remove single bearer_token"
```

---

### Task 2: `Principal` + system-JWT + `require_principal`

**Files:**
- Create: `src/dlw/auth/principal.py`
- Test: `tests/auth/test_principal.py`

- [ ] **Step 1: Write the failing test**

Create `tests/auth/test_principal.py`:

```python
"""Principal / system-JWT decode tests (Phase 3 SP1)."""
from __future__ import annotations

import time
import types

import jwt as _pyjwt
import pytest
from fastapi import HTTPException

from dlw.auth.principal import (
    Principal,
    issue_system_jwt,
    require_principal,
)

SECRET = "unit-secret"


def _req(svc_token: str = ""):
    """Minimal stand-in for fastapi.Request exposing app.state.settings."""
    settings = types.SimpleNamespace(
        system_jwt_secret=SECRET, system_admin_token=svc_token
    )
    app = types.SimpleNamespace(state=types.SimpleNamespace(settings=settings))
    return types.SimpleNamespace(app=app)


async def test_issue_and_decode_roundtrip():
    tok = issue_system_jwt(secret=SECRET, user_id=7, tenant_id=3,
                           role="tenant_operator", project_ids=[1, 2])
    p = await require_principal(_req(), authorization=f"Bearer {tok}")
    assert p == Principal(user_id=7, tenant_id=3, role="tenant_operator",
                          project_ids=(1, 2), is_service=False)


async def test_missing_header_401():
    with pytest.raises(HTTPException) as e:
        await require_principal(_req(), authorization=None)
    assert e.value.status_code == 401


async def test_expired_token_401():
    tok = issue_system_jwt(secret=SECRET, user_id=1, tenant_id=1,
                           role="tenant_viewer", project_ids=[], ttl_seconds=-10)
    with pytest.raises(HTTPException) as e:
        await require_principal(_req(), authorization=f"Bearer {tok}")
    assert e.value.status_code == 401


async def test_forged_signature_401():
    tok = _pyjwt.encode(
        {"iss": "dlw-controller", "sub": "1", "tid": 1, "role": "x",
         "pids": [], "iat": int(time.time()), "exp": int(time.time()) + 60},
        "wrong-secret", algorithm="HS256")
    with pytest.raises(HTTPException) as e:
        await require_principal(_req(), authorization=f"Bearer {tok}")
    assert e.value.status_code == 401


async def test_wrong_issuer_401():
    tok = _pyjwt.encode(
        {"iss": "evil", "sub": "1", "tid": 1, "role": "x", "pids": [],
         "iat": int(time.time()), "exp": int(time.time()) + 60},
        SECRET, algorithm="HS256")
    with pytest.raises(HTTPException) as e:
        await require_principal(_req(), authorization=f"Bearer {tok}")
    assert e.value.status_code == 401


async def test_service_token_yields_service_principal():
    p = await require_principal(_req(svc_token="svc-xyz"),
                                authorization="Bearer svc-xyz")
    assert p.is_service is True
    assert p.role == "system_admin"
    assert p.tenant_id == 1
    assert p.user_id == 0
```

Also create empty `tests/auth/__init__.py` (siblings `tests/api/`, `tests/services/`, `tests/e2e/` all have one — this is required by the suite's package layout).

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/auth/test_principal.py -v`
Expected: FAIL — `dlw.auth.principal` does not exist.

- [ ] **Step 3: Implement**

Create `src/dlw/auth/principal.py`:

```python
"""Request principal decoded from the system-JWT (Phase 3 SP1).

The system-JWT is issued by /auth/callback after OIDC. It is HS256-signed
with settings.system_jwt_secret (shared across active/standby — both must
verify the same user tokens, so it's a config secret, not a per-instance
bootstrapped keypair like the executor EdDSA key)."""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

import jwt as _pyjwt
from fastapi import Header, HTTPException, Request, status

SYSTEM_JWT_ALG = "HS256"
SYSTEM_JWT_ISS = "dlw-controller"
SYSTEM_JWT_TTL_SECONDS = 3600


@dataclass(frozen=True)
class Principal:
    user_id: int
    tenant_id: int
    role: str
    project_ids: tuple[int, ...]
    is_service: bool = False


def issue_system_jwt(
    *,
    secret: str,
    user_id: int,
    tenant_id: int,
    role: str,
    project_ids: list[int],
    ttl_seconds: int = SYSTEM_JWT_TTL_SECONDS,
) -> str:
    now = int(time.time())
    return _pyjwt.encode(
        {
            "iss": SYSTEM_JWT_ISS,
            "sub": str(user_id),
            "tid": tenant_id,
            "role": role,
            "pids": project_ids,
            "iat": now,
            "exp": now + ttl_seconds,
        },
        secret,
        algorithm=SYSTEM_JWT_ALG,
    )


def _ct_eq(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


async def require_principal(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Principal:
    settings = request.app.state.settings
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.removeprefix("Bearer ").strip()

    svc = settings.system_admin_token
    if svc and _ct_eq(token, svc):
        return Principal(
            user_id=0, tenant_id=1, role="system_admin",
            project_ids=(), is_service=True,
        )
    try:
        claims = _pyjwt.decode(
            token,
            settings.system_jwt_secret,
            algorithms=[SYSTEM_JWT_ALG],
            issuer=SYSTEM_JWT_ISS,
            options={"require": ["sub", "tid", "role", "exp", "iss", "iat"]},
        )
    except _pyjwt.PyJWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid token: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e
    return Principal(
        user_id=int(claims["sub"]),
        tenant_id=int(claims["tid"]),
        role=str(claims["role"]),
        project_ids=tuple(int(x) for x in claims.get("pids", [])),
    )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/auth/test_principal.py -v`
Expected: PASS (6 cases).

- [ ] **Step 5: Commit**

```bash
git add src/dlw/auth/principal.py tests/auth/
git commit -m "feat(sp1): Principal + system-JWT + require_principal w/ service token"
```

---

### Task 3: OIDC client (dev + real) + tenant resolution

**Files:**
- Create: `src/dlw/auth/oidc.py`
- Test: `tests/auth/test_oidc.py`

Add `authlib>=1.3,<2.0` to `pyproject.toml` `[project] dependencies` in this task (alphabetical-ish placement near `asyncpg`/`alembic` is fine; keep the list valid TOML), then `uv sync` (or `pip install -e .`).

- [ ] **Step 1: Write the failing test**

Create `tests/auth/test_oidc.py`:

```python
"""OIDC dev-mode + tenant-resolution tests (Phase 3 SP1)."""
from __future__ import annotations

import pytest

from dlw.auth.oidc import (
    OidcClaims,
    TenantRule,
    exchange_code_dev,
    resolve_tenant,
)


def test_dev_exchange_parses_email_from_code():
    claims = exchange_code_dev("dev:alice@acme.com")
    assert claims == OidcClaims(sub="dev:alice@acme.com",
                                email="alice@acme.com", groups=())


def test_dev_exchange_rejects_non_dev_code():
    with pytest.raises(ValueError):
        exchange_code_dev("realcode123")


def test_resolve_tenant_email_domain_rule():
    rules = [TenantRule(match="email_domain", value="acme.com",
                        tenant_slug="acme", role="tenant_operator")]
    slug, role = resolve_tenant(
        OidcClaims(sub="s", email="bob@acme.com", groups=()), rules)
    assert (slug, role) == ("acme", "tenant_operator")


def test_resolve_tenant_group_rule():
    rules = [TenantRule(match="group", value="ml-eng",
                        tenant_slug="research", role="project_member")]
    slug, role = resolve_tenant(
        OidcClaims(sub="s", email="x@y.z", groups=("ml-eng",)), rules)
    assert (slug, role) == ("research", "project_member")


def test_resolve_tenant_no_match_returns_none():
    rules = [TenantRule(match="email_domain", value="acme.com",
                        tenant_slug="acme", role="tenant_viewer")]
    assert resolve_tenant(
        OidcClaims(sub="s", email="x@other.com", groups=()), rules) is None


def test_resolve_tenant_wildcard_rule():
    rules = [TenantRule(match="email_domain", value="*",
                        tenant_slug="default", role="tenant_operator")]
    slug, role = resolve_tenant(
        OidcClaims(sub="s", email="anyone@anywhere", groups=()), rules)
    assert slug == "default"


def test_parse_rules_from_json():
    from dlw.auth.oidc import parse_tenant_rules
    rules = parse_tenant_rules(
        '[{"match":"group","value":"g","tenant_slug":"t","role":"tenant_viewer"}]')
    assert rules == [TenantRule(match="group", value="g",
                                tenant_slug="t", role="tenant_viewer")]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/auth/test_oidc.py -v`
Expected: FAIL — `dlw.auth.oidc` missing.

- [ ] **Step 3: Implement**

Create `src/dlw/auth/oidc.py`:

```python
"""OIDC authorization-code exchange + tenant resolution (Phase 3 SP1).

Real mode uses Authlib against settings.oidc_issuer. Dev mode
(settings.auth_dev_mode) skips the network: the `code` is `dev:<email>`
and claims are synthesized — keeps CI hermetic (no live IdP), the same
philosophy as the Phase 2 enrollment-token / local-PG-no-Docker setup."""
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class OidcClaims:
    sub: str
    email: str
    groups: tuple[str, ...]


@dataclass(frozen=True)
class TenantRule:
    match: str        # "email_domain" | "group"
    value: str        # domain (or "*") | group name
    tenant_slug: str
    role: str


def parse_tenant_rules(raw: str) -> list[TenantRule]:
    data = json.loads(raw or "[]")
    return [
        TenantRule(
            match=d["match"], value=d["value"],
            tenant_slug=d["tenant_slug"], role=d["role"],
        )
        for d in data
    ]


def exchange_code_dev(code: str) -> OidcClaims:
    """Dev-mode code is 'dev:<email>'. Raises ValueError otherwise."""
    if not code.startswith("dev:"):
        raise ValueError("dev mode expects a 'dev:<email>' code")
    email = code.removeprefix("dev:")
    return OidcClaims(sub=code, email=email, groups=())


async def exchange_code_real(
    *, code: str, state: str, issuer: str, client_id: str,
    client_secret: str, redirect_url: str,
) -> OidcClaims:
    """Real OIDC: code->token, verify id_token via JWKS, return claims."""
    from authlib.integrations.httpx_client import AsyncOAuth2Client
    from authlib.jose import JsonWebToken
    import httpx

    async with httpx.AsyncClient(timeout=10) as http:
        meta = (await http.get(
            f"{issuer.rstrip('/')}/.well-known/openid-configuration")).json()
        oauth = AsyncOAuth2Client(
            client_id, client_secret, redirect_uri=redirect_url)
        token = await oauth.fetch_token(
            meta["token_endpoint"], code=code, state=state,
            grant_type="authorization_code")
        jwks = (await http.get(meta["jwks_uri"])).json()
    id_tok = token["id_token"]
    claims = JsonWebToken(["RS256", "ES256", "EdDSA"]).decode(
        id_tok, jwks)
    claims.validate()
    grp = claims.get("groups") or []
    return OidcClaims(
        sub=str(claims["sub"]),
        email=str(claims.get("email", "")),
        groups=tuple(grp),
    )


def resolve_tenant(
    claims: OidcClaims, rules: list[TenantRule]
) -> tuple[str, str] | None:
    """First matching rule wins. Returns (tenant_slug, role) or None."""
    domain = claims.email.split("@")[-1] if "@" in claims.email else ""
    for r in rules:
        if r.match == "email_domain" and r.value in ("*", domain):
            return r.tenant_slug, r.role
        if r.match == "group" and r.value in claims.groups:
            return r.tenant_slug, r.role
    return None
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/auth/test_oidc.py -v`
Expected: PASS (7 cases). `exchange_code_real` is covered in Task 4's callback test via a stubbed transport — not unit-tested here.

- [ ] **Step 5: Commit**

```bash
git add src/dlw/auth/oidc.py tests/auth/test_oidc.py pyproject.toml uv.lock
git commit -m "feat(sp1): OIDC code exchange (dev+real) + tenant resolution rules"
```

---

### Task 4: `api/auth.py` — login / callback / me

**Files:**
- Create: `src/dlw/api/auth.py`
- Modify: `src/dlw/main.py` (mount the router + set `app.state.settings`)
- Test: `tests/api/test_auth_endpoints.py`

- [ ] **Step 1: Write the failing test**

Create `tests/api/test_auth_endpoints.py`:

```python
"""auth router tests in dev mode (Phase 3 SP1)."""
from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Tenant(id=1, slug="default", display_name="Default"))
        await s.flush()
        s.add(Project(id=1, tenant_id=1, name="default"))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _dev_mode(monkeypatch):
    monkeypatch.setenv("DLW_AUTH_DEV_MODE", "true")
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", "unit-secret")
    monkeypatch.setenv(
        "DLW_AUTH_TENANT_RULES_JSON",
        '[{"match":"email_domain","value":"*","tenant_slug":"default",'
        '"role":"tenant_operator"}]')
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def client(ephemeral_ca):
    from tests.conftest import make_app_with_state
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


@pytest.mark.slow
async def test_login_dev_redirects_to_callback(client):
    r = await client.get("/api/v1/auth/login", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "/api/v1/auth/callback" in r.headers["location"]


@pytest.mark.slow
async def test_callback_dev_issues_system_jwt(client):
    # mirror the state cookie the login step set
    login = await client.get("/api/v1/auth/login", follow_redirects=False)
    loc = login.headers["location"]
    r = await client.get(loc, follow_redirects=False)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == 1
    assert body["role"] == "tenant_operator"
    assert body["system_jwt"]
    # /auth/me round-trips the principal
    me = await client.get("/api/v1/auth/me",
                          headers={"Authorization": f"Bearer {body['system_jwt']}"})
    assert me.status_code == 200
    assert me.json()["tenant_id"] == 1


@pytest.mark.slow
async def test_callback_unresolved_tenant_403(client, monkeypatch):
    monkeypatch.setenv("DLW_AUTH_TENANT_RULES_JSON", "[]")
    get_settings.cache_clear()
    login = await client.get("/api/v1/auth/login", follow_redirects=False)
    r = await client.get(login.headers["location"], follow_redirects=False)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "TENANT_UNRESOLVED"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/api/test_auth_endpoints.py -v`
Expected: FAIL — no `/api/v1/auth/*` routes; `app.state.settings` unset.

- [ ] **Step 3: Implement**

Create `src/dlw/api/auth.py`:

```python
"""OIDC login / callback / me (Phase 3 SP1)."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dlw.auth.oidc import (
    OidcClaims,
    exchange_code_dev,
    exchange_code_real,
    parse_tenant_rules,
    resolve_tenant,
)
from dlw.auth.principal import Principal, issue_system_jwt, require_principal
from dlw.config import get_settings
from dlw.db.models.tenant import Tenant, User
from dlw.db.session import get_engine
from dlw.services.audit import write_audit

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
_STATE_COOKIE = "dlw_oidc_state"


async def _session() -> AsyncSession:  # pragma: no cover - trivial
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with factory() as s:
        yield s


@router.get("/login")
async def login() -> Response:
    settings = get_settings()
    state = secrets.token_urlsafe(24)
    if settings.auth_dev_mode:
        loc = f"/api/v1/auth/callback?code=dev:dev@local&state={state}"
    else:
        from authlib.integrations.httpx_client import AsyncOAuth2Client
        import httpx
        async with httpx.AsyncClient(timeout=10) as http:
            meta = (await http.get(
                f"{settings.oidc_issuer.rstrip('/')}"
                "/.well-known/openid-configuration")).json()
        oauth = AsyncOAuth2Client(
            settings.oidc_client_id, settings.oidc_client_secret,
            redirect_uri=settings.oidc_redirect_url,
            scope="openid email profile groups")
        loc, _ = oauth.create_authorization_url(
            meta["authorization_endpoint"], state=state)
    resp = RedirectResponse(loc, status_code=307)
    resp.set_cookie(_STATE_COOKIE, state, httponly=True,
                    samesite="strict", secure=not settings.auth_dev_mode)
    return resp


@router.get("/callback")
async def callback(
    request: Request, code: str, state: str,
    session: AsyncSession = Depends(_session),
) -> Response:
    settings = get_settings()
    if request.cookies.get(_STATE_COOKIE) != state:
        raise HTTPException(400, detail="invalid state")

    try:
        if settings.auth_dev_mode:
            claims: OidcClaims = exchange_code_dev(code)
        else:
            claims = await exchange_code_real(
                code=code, state=state, issuer=settings.oidc_issuer,
                client_id=settings.oidc_client_id,
                client_secret=settings.oidc_client_secret,
                redirect_url=settings.oidc_redirect_url)
    except Exception as e:  # noqa: BLE001 - IdP/transport failures map to 503
        raise HTTPException(503, detail=f"oidc upstream error: {e}") from e

    user = (await session.execute(
        select(User).where(User.oidc_subject == claims.sub)
    )).scalar_one_or_none()

    if user is None:
        rules = parse_tenant_rules(settings.auth_tenant_rules_json)
        resolved = resolve_tenant(claims, rules)
        if resolved is None:
            await write_audit(session, action="login", resource_type="user",
                              resource_id=claims.sub, outcome="denied",
                              tenant_id=None, actor_user_id=None)
            await session.commit()
            raise HTTPException(
                403, detail={"code": "TENANT_UNRESOLVED",
                             "message": "no tenant rule matched"})
        slug, role = resolved
        tenant = (await session.execute(
            select(Tenant).where(Tenant.slug == slug)
        )).scalar_one_or_none()
        if tenant is None:
            raise HTTPException(
                403, detail={"code": "TENANT_UNRESOLVED",
                             "message": f"tenant '{slug}' not provisioned"})
        user = User(tenant_id=tenant.id, oidc_subject=claims.sub,
                    email=claims.email, role=role)
        session.add(user)
        await session.flush()
    else:
        user.email = claims.email or user.email

    token = issue_system_jwt(
        secret=settings.system_jwt_secret, user_id=user.id,
        tenant_id=user.tenant_id, role=user.role, project_ids=[])
    await write_audit(session, action="login", resource_type="user",
                      resource_id=str(user.id), outcome="success",
                      tenant_id=user.tenant_id, actor_user_id=user.id)
    await session.commit()
    return JSONResponse({
        "system_jwt": token, "expires_in": 3600,
        "tenant_id": user.tenant_id, "role": user.role,
    })


@router.get("/me")
async def me(principal: Principal = Depends(require_principal)) -> dict:
    return {
        "user_id": principal.user_id, "tenant_id": principal.tenant_id,
        "role": principal.role, "project_ids": list(principal.project_ids),
        "is_service": principal.is_service,
    }
```

In `src/dlw/main.py` `create_app()`, after `app.include_router(health_router)` add:

```python
    from dlw.config import get_settings as _gs2
    app.state.settings = _gs2()
    from dlw.api.auth import router as auth_router
    app.include_router(auth_router)
```

And in `tests/conftest.py` `make_app_with_state`, after `app = create_app()` add (so ASGI tests have settings on state):

```python
    from dlw.config import get_settings as _gs
    app.state.settings = _gs()
```

(Settings is `lru_cache`d and tests `cache_clear()` after env changes — calling it here picks up the test env.)

- [ ] **Step 4: Run tests**

Run: `pytest tests/api/test_auth_endpoints.py -v`
Expected: PASS (4 cases). `write_audit` is created in Task 8 — if running M1 strictly before M2, temporarily inline a no-op; **better:** reorder so Task 8's `services/audit.py` is created here. To keep tasks independent, create the minimal `src/dlw/services/audit.py` now (full version is identical — Task 8 just adds callers):

```python
"""Thin audit-log writer over the existing AuditLog model (Phase 3 SP1).
Phase 4 adds prev_hash/self_hash chaining; SP1 only records rows."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.audit import AuditLog


async def write_audit(
    session: AsyncSession, *, action: str, resource_type: str,
    resource_id: str | None, outcome: str, tenant_id: int | None,
    actor_user_id: int | None, payload: dict[str, Any] | None = None,
) -> None:
    body = json.dumps(
        {"action": action, "resource_id": resource_id, "outcome": outcome,
         "payload": payload}, sort_keys=True, default=str)
    session.add(AuditLog(
        tenant_id=tenant_id, actor_user_id=actor_user_id, action=action,
        resource_type=resource_type, resource_id=resource_id,
        outcome=outcome, payload=payload,
        self_hash=hashlib.sha256(body.encode()).hexdigest()))
```

- [ ] **Step 5: Commit**

```bash
git add src/dlw/api/auth.py src/dlw/services/audit.py src/dlw/main.py tests/conftest.py tests/api/test_auth_endpoints.py
git commit -m "feat(sp1): /auth login+callback+me (dev mode) + audit writer"
```

---

### Task 5: Models + alembic migration (3 tables + seed)

**Files:**
- Create: `src/dlw/db/models/usage.py`, `src/dlw/db/models/casbin_rule.py`
- Create: `src/dlw/alembic/versions/<rev>_p3sp1_tenancy_quota.py`
- Test: `tests/db/test_p3sp1_migration.py`

- [ ] **Step 1: Write the failing test**

Create empty `tests/db/__init__.py` (package marker, like sibling test dirs), then `tests/db/test_p3sp1_migration.py`:

```python
"""SP1 migration: 3 new tables + idempotent default-tenant/snapshot seed."""
from __future__ import annotations

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.slow


async def _tables(conn) -> set[str]:
    rows = await conn.execute(text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public'"))
    return {r[0] for r in rows}


async def test_upgrade_creates_tables_and_seeds(engine):
    from dlw.db.base import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # apply the migration's data-seed logic (tables already created by metadata;
    # we assert the seed effect: default tenant + a snapshot row)
    from dlw.alembic.versions import _p3sp1_seed  # helper module (see impl)
    async with engine.begin() as conn:
        names = await _tables(conn)
        assert {"usage_records", "quota_snapshots", "casbin_rule"} <= names
    from sqlalchemy.ext.asyncio import async_sessionmaker
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        await _p3sp1_seed.seed(s)
        await _p3sp1_seed.seed(s)  # idempotent: second call no-op
        await s.commit()
        from dlw.db.models.tenant import Tenant
        from dlw.db.models.usage import QuotaSnapshot
        from sqlalchemy import select
        t = (await s.execute(select(Tenant).where(Tenant.id == 1))).scalar_one()
        assert t.slug == "default"
        snap = (await s.execute(
            select(QuotaSnapshot).where(QuotaSnapshot.tenant_id == 1))
        ).scalar_one()
        assert snap.bytes_used_month == 0
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/db/test_p3sp1_migration.py -v`
Expected: FAIL — `dlw.db.models.usage` / `_p3sp1_seed` missing.

- [ ] **Step 3: Implement**

Create `src/dlw/db/models/usage.py`:

```python
"""Quota usage models (Phase 3 SP1; security §7.3)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from dlw.db.base import Base


class UsageRecord(Base):
    __tablename__ = "usage_records"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tenants.id"), nullable=False)
    project_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)


class QuotaSnapshot(Base):
    __tablename__ = "quota_snapshots"

    tenant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tenants.id"), primary_key=True)
    bytes_used_month: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False)
    storage_gb_used: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False)
    concurrent_tasks: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False)
    last_recomputed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
```

Create `src/dlw/db/models/casbin_rule.py`:

```python
"""casbin policy storage (Phase 3 SP1). Authz infrastructure table — not a
business/data table, so it intentionally has no tenant_id (like
alembic_version). NOTE: there is no CI information_schema tenant_id scan in
this repo (the Invariant-8 CI gate is the source AST lint
tools/lint_invariants.py, which does not inspect DB tables), so no allowlist
entry is needed — this comment is documentation only."""
from __future__ import annotations

from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from dlw.db.base import Base


class CasbinRule(Base):
    __tablename__ = "casbin_rule"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ptype: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    v0: Mapped[str | None] = mapped_column(String(256), nullable=True)
    v1: Mapped[str | None] = mapped_column(String(256), nullable=True)
    v2: Mapped[str | None] = mapped_column(String(256), nullable=True)
    v3: Mapped[str | None] = mapped_column(String(256), nullable=True)
    v4: Mapped[str | None] = mapped_column(String(256), nullable=True)
    v5: Mapped[str | None] = mapped_column(String(256), nullable=True)
```

Register both models in `src/dlw/db/models/__init__.py` (verified: that module's docstring is "Importing this module also registers them with Base.metadata"; it has explicit imports + an `__all__`). Add:

```python
from dlw.db.models.casbin_rule import CasbinRule
from dlw.db.models.usage import QuotaSnapshot, UsageRecord
```

and extend `__all__` with `"CasbinRule", "QuotaSnapshot", "UsageRecord"` (keep it sorted as the existing list is). Do NOT touch `src/dlw/db/base.py` (importing models there is a circular import and would not register the tables).

Create `src/dlw/alembic/versions/__init__.py` if absent (so the seed helper is importable), then `src/dlw/alembic/versions/_p3sp1_seed.py`:

```python
"""Idempotent data-seed shared by the SP1 migration and tests."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.tenant import Tenant
from dlw.db.models.usage import QuotaSnapshot


async def seed(session: AsyncSession) -> None:
    # Tenant.quota_*/is_active use Python-side default= (NOT server_default),
    # so a Core pg_insert MUST supply them explicitly or the NOT NULL fails.
    await session.execute(pg_insert(Tenant).values(
        id=1, slug="default", display_name="Default Tenant",
        quota_bytes_month=0, quota_concurrent=10, quota_storage_gb=1024,
        is_active=True,
    ).on_conflict_do_nothing(index_elements=["id"]))
    tenant_ids = (await session.execute(select(Tenant.id))).scalars().all()
    for tid in tenant_ids:
        await session.execute(pg_insert(QuotaSnapshot).values(
            tenant_id=tid,
        ).on_conflict_do_nothing(index_elements=["tenant_id"]))
```

Generate the migration: `alembic revision -m "p3sp1 tenancy quota"` then replace its body. `down_revision = "6f37b72630ce"`. `upgrade()` creates the 3 tables (mirror the model columns; `op.create_table(...)` + `op.create_index("idx_usage_tenant_metric_time", "usage_records", ["tenant_id","metric","occurred_at"])` + `op.create_index("idx_casbin_ptype","casbin_rule",["ptype"])`), then runs the seed via a synchronous connection:

```python
def upgrade() -> None:
    op.create_table(
        "usage_records",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(),
                  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("metric", sa.String(64), nullable=False),
        sa.Column("value", sa.BigInteger(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_usage_tenant_metric_time", "usage_records",
                    ["tenant_id", "metric", "occurred_at"])
    op.create_table(
        "quota_snapshots",
        sa.Column("tenant_id", sa.BigInteger(),
                  sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("bytes_used_month", sa.BigInteger(),
                  server_default="0", nullable=False),
        sa.Column("storage_gb_used", sa.BigInteger(),
                  server_default="0", nullable=False),
        sa.Column("concurrent_tasks", sa.Integer(),
                  server_default="0", nullable=False),
        sa.Column("last_recomputed_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "casbin_rule",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("ptype", sa.String(8), nullable=False),
        *[sa.Column(f"v{i}", sa.String(256), nullable=True)
          for i in range(6)],
    )
    op.create_index("idx_casbin_ptype", "casbin_rule", ["ptype"])
    conn = op.get_bind()
    # quota_*/is_active are Python-side default= only (no server_default) —
    # a raw INSERT MUST supply them or the NOT NULL constraint fails.
    conn.execute(sa.text(
        "INSERT INTO tenants (id, slug, display_name, quota_bytes_month, "
        "quota_concurrent, quota_storage_gb, is_active) "
        "VALUES (1, 'default', 'Default Tenant', 0, 10, 1024, true) "
        "ON CONFLICT (id) DO NOTHING"))
    conn.execute(sa.text(
        "INSERT INTO quota_snapshots (tenant_id) "
        "SELECT id FROM tenants ON CONFLICT (tenant_id) DO NOTHING"))


def downgrade() -> None:
    op.drop_index("idx_casbin_ptype", "casbin_rule")
    op.drop_table("casbin_rule")
    op.drop_table("quota_snapshots")
    op.drop_index("idx_usage_tenant_metric_time", "usage_records")
    op.drop_table("usage_records")
```

(Imports at top of the migration: `import sqlalchemy as sa`, `from alembic import op`, `from sqlalchemy.dialects import postgresql`.)

- [ ] **Step 4: Run tests**

Run: `pytest tests/db/test_p3sp1_migration.py -v`
Then verify the migration applies cleanly against a scratch DB:
Run: `alembic upgrade head && alembic downgrade -1 && alembic upgrade head`
Expected: PASS; alembic up/down/up clean.

- [ ] **Step 5: Commit**

```bash
git add src/dlw/db/models/usage.py src/dlw/db/models/casbin_rule.py src/dlw/db/models/__init__.py src/dlw/alembic/versions/ tests/db/__init__.py tests/db/test_p3sp1_migration.py
git commit -m "feat(sp1): UsageRecord/QuotaSnapshot/CasbinRule models + migration"
```

---

### Task 6: Startup guard (fail-closed on insecure prod config)

**Files:**
- Modify: `src/dlw/main.py`
- Test: `tests/test_startup_guard.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_startup_guard.py`:

```python
"""Startup guard refuses insecure prod config (Phase 3 SP1)."""
from __future__ import annotations

import pytest

from dlw.main import check_auth_startup_config


def _s(**kw):
    import types
    base = dict(auth_dev_mode=False,
                system_jwt_secret="dev-system-jwt-change-me",
                oidc_issuer="", auth_tenant_rules_json="[]")
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_dev_mode_allows_anything():
    check_auth_startup_config(_s(auth_dev_mode=True))  # no raise


def test_prod_insecure_jwt_secret_refused():
    with pytest.raises(RuntimeError, match="system_jwt_secret"):
        check_auth_startup_config(_s(system_jwt_secret="dev-system-jwt-change-me",
                                     oidc_issuer="https://idp"))


def test_prod_missing_issuer_refused():
    with pytest.raises(RuntimeError, match="oidc_issuer"):
        check_auth_startup_config(_s(system_jwt_secret="strong", oidc_issuer=""))


def test_prod_wildcard_rule_refused():
    with pytest.raises(RuntimeError, match="wildcard"):
        check_auth_startup_config(_s(
            system_jwt_secret="strong", oidc_issuer="https://idp",
            auth_tenant_rules_json='[{"match":"email_domain","value":"*",'
            '"tenant_slug":"default","role":"tenant_operator"}]'))


def test_prod_valid_config_ok():
    check_auth_startup_config(_s(
        system_jwt_secret="strong", oidc_issuer="https://idp",
        auth_tenant_rules_json='[{"match":"group","value":"g",'
        '"tenant_slug":"t","role":"tenant_viewer"}]'))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_startup_guard.py -v`
Expected: FAIL — `check_auth_startup_config` missing.

- [ ] **Step 3: Implement**

In `src/dlw/main.py` add (module level, before `lifespan`):

```python
def check_auth_startup_config(settings) -> None:
    """Fail closed: in non-dev mode, refuse insecure auth config."""
    if settings.auth_dev_mode:
        return
    if settings.system_jwt_secret == "dev-system-jwt-change-me":
        raise RuntimeError(
            "insecure system_jwt_secret in non-dev mode; set DLW_SYSTEM_JWT_SECRET")
    if not settings.oidc_issuer:
        raise RuntimeError("oidc_issuer required in non-dev mode")
    from dlw.auth.oidc import parse_tenant_rules
    for r in parse_tenant_rules(settings.auth_tenant_rules_json):
        if r.match == "email_domain" and r.value == "*":
            raise RuntimeError(
                "wildcard email_domain tenant rule forbidden in non-dev mode")
```

Call it inside `lifespan` right after `_settings = _gs()` (line ~41): `check_auth_startup_config(_settings)`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_startup_guard.py -v`
Expected: PASS (5 cases).

- [ ] **Step 5: Commit**

```bash
git add src/dlw/main.py tests/test_startup_guard.py
git commit -m "feat(sp1): fail-closed startup guard for insecure prod auth config"
```

---

# Milestone M2 — RBAC + Tenant Scoping

### Task 7: casbin model + policy + `build_enforcer`

**Files:**
- Create: `src/dlw/authz/__init__.py`, `src/dlw/authz/model.conf`, `src/dlw/authz/policy.csv`, `src/dlw/authz/enforcer.py`
- Test: `tests/authz/test_enforcer.py`

- [ ] **Step 1: Write the failing test**

Create `tests/authz/test_enforcer.py`:

Create empty `tests/authz/__init__.py`, then `tests/authz/test_enforcer.py`:

```python
"""casbin enforcer matrix — SP1 is tenant-scoped only (no project_match)."""
from __future__ import annotations

from dlw.authz.enforcer import build_enforcer


def _e():
    return build_enforcer(grants=[])


def _enforce(e, role, tenant, obj, act, rtenant):
    # request: sub, tenant, obj, act, rtenant
    return e.enforce(f"role:{role}", tenant, obj, act, rtenant)


def test_tenant_operator_can_post_tasks_same_tenant():
    assert _enforce(_e(), "tenant_operator", 1,
                    "/api/v1/tasks", "POST", 1) is True


def test_tenant_viewer_cannot_post_tasks():
    assert _enforce(_e(), "tenant_viewer", 1,
                    "/api/v1/tasks", "POST", 1) is False


def test_tenant_viewer_can_get_task_by_id():
    assert _enforce(_e(), "tenant_viewer", 1,
                    "/api/v1/tasks/abc", "GET", 1) is True


def test_cross_tenant_denied():
    assert _enforce(_e(), "tenant_operator", 1,
                    "/api/v1/tasks", "POST", 2) is False


def test_system_admin_any():
    assert _enforce(_e(), "system_admin", 1,
                    "/api/v1/anything", "DELETE", 99) is True


def test_anchored_act_regex_rejects_superstring():
    # regexMatch is unanchored (Go semantics); policy acts MUST be ^(...)$
    # so a bogus method like "POSTX" does NOT match the POST rule.
    assert _enforce(_e(), "tenant_operator", 1,
                    "/api/v1/tasks", "POSTX", 1) is False


def test_quota_get_allowed_tasks_path_not_confused():
    # keyMatch (not keyMatch2): trailing * matches to end; /api/v1/quota*
    # must NOT let a tasks-only viewer hit quota via path confusion.
    assert _enforce(_e(), "tenant_viewer", 1,
                    "/api/v1/quota/current", "GET", 1) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/authz/test_enforcer.py -v`
Expected: FAIL — `dlw.authz.enforcer` missing. Add `casbin>=1.36,<2.0` to `pyproject.toml [project] dependencies`, run `uv lock` then `uv sync --all-groups`, and `git add pyproject.toml uv.lock` (CI runs `uv sync --all-groups` and fails on a stale lock).

- [ ] **Step 3: Implement**

Create `src/dlw/authz/model.conf`:

SP1 is **tenant-scoped only**. Project-scoped RBAC (`project_member`/`project_owner`/`project_match`) is deferred: the OIDC callback issues `project_ids=[]` for every user in SP1 (no project membership source yet), and FastAPI cannot pass a per-row `rproject` through a dependency without loading the object — so project_match would be dead/broken. The matcher therefore covers only role→obj→act + tenant equality. (Spec §3.3/§1.3 updated to record this deferral.)

```ini
[request_definition]
r = sub, tenant, obj, act, rtenant

[policy_definition]
p = sub, obj, act, scope

[role_definition]
g = _, _

[policy_effect]
e = some(where (p.eft == allow))

[matchers]
m = g(r.sub, p.sub) && keyMatch(r.obj, p.obj) && regexMatch(r.act, p.act) && (p.scope == "any" || r.tenant == r.rtenant)
```

`keyMatch` (not `keyMatch2`): a trailing `*` matches to end-of-string (so `/api/v1/tasks*` matches `/api/v1/tasks` and `/api/v1/tasks/abc`; `/api/v1/*` matches anything under `/api/v1/`). `keyMatch2` is for `:param` segments and is the wrong matcher here.

Create `src/dlw/authz/policy.csv` — act regexes are **anchored** (`^(...)$`) because casbin `regexMatch` uses unanchored Go `regexp` semantics (an unanchored `(GET)|(POST)` would also match `POSTX`):

```csv
p, role:system_admin, /api/v1/*, ^(GET|POST|DELETE|PUT)$, any
p, role:tenant_admin, /api/v1/tasks*, ^(GET|POST|DELETE)$, tenant_match
p, role:tenant_admin, /api/v1/quota*, ^GET$, tenant_match
p, role:tenant_operator, /api/v1/tasks*, ^(GET|POST|DELETE)$, tenant_match
p, role:tenant_operator, /api/v1/quota*, ^GET$, tenant_match
p, role:tenant_viewer, /api/v1/tasks*, ^GET$, tenant_match
p, role:tenant_viewer, /api/v1/quota*, ^GET$, tenant_match
g, role:system_admin, role:system_admin
g, role:tenant_admin, role:tenant_admin
g, role:tenant_operator, role:tenant_operator
g, role:tenant_viewer, role:tenant_viewer
```

Create `src/dlw/authz/__init__.py` (empty). Create `src/dlw/authz/enforcer.py`:

```python
"""casbin RBAC enforcer (Phase 3 SP1, tenant-scoped only).

The matcher handles role->obj->act + tenant equality. Project-scoped roles
are deferred (see Task 7 note); require_perm uses enforcer.enforce() -> bool
(no enforce_ex / no scope post-check in SP1)."""
from __future__ import annotations

from pathlib import Path

import casbin
from casbin.persist.adapters import StringAdapter

_DIR = Path(__file__).parent
_MODEL = str(_DIR / "model.conf")


def _base_policy_csv() -> str:
    return (_DIR / "policy.csv").read_text(encoding="utf-8")


def build_enforcer(*, grants: list[tuple[str, str]]) -> casbin.Enforcer:
    """Build an enforcer from model.conf + policy.csv, plus per-subject
    grants (loaded from the DB casbin_rule table at bootstrap; SP1 has
    none, but the wiring is the extension point for a later sub-project)."""
    lines = _base_policy_csv()
    for sub, role in grants:
        lines += f"\ng, {sub}, {role}"
    adapter = StringAdapter(lines)
    return casbin.Enforcer(_MODEL, adapter)


async def load_grants(session) -> list[tuple[str, str]]:
    """Load `g` (subject->role) rows from casbin_rule (empty in SP1)."""
    from sqlalchemy import select

    from dlw.db.models.casbin_rule import CasbinRule

    rows = (await session.execute(
        select(CasbinRule).where(CasbinRule.ptype == "g")
    )).scalars().all()
    return [(r.v0, r.v1) for r in rows if r.v0 and r.v1]
```

`load_grants` is unused by SP1 routes but is exercised by Task 10's `make_app_with_state` extension (`build_enforcer(grants=[])`); keep it for the extension point. (Optional local-only: if `mypy` flags casbin as untyped add a `[[tool.mypy.overrides]]` block for `module = "casbin.*"` with `ignore_missing_imports = true` — mypy is not a CI gate, so this is not required for the PR.)

- [ ] **Step 4: Install casbin + run the enforcer tests for real**

casbin behavior (keyMatch/regexMatch/`enforce` arity) must be verified by running, not asserted blind.
Run: `uv lock && uv sync --all-groups`
Run: `pytest tests/authz/test_enforcer.py -v`
Expected: PASS (7 cases). If `keyMatch`/anchoring behaves unexpectedly against the installed casbin version, adjust `model.conf`/`policy.csv` until all 7 pass (do not weaken the cross-tenant or anchored-regex assertions).

- [ ] **Step 5: Commit**

```bash
git add src/dlw/authz/ tests/authz/ pyproject.toml uv.lock
git commit -m "feat(sp1): casbin RBAC model + base policy + build_enforcer"
```

---

### Task 8: `require_perm` dependency factory + audit on deny

**Files:**
- Create: `src/dlw/authz/deps.py`
- Modify: `src/dlw/services/audit.py` (already created in Task 4 — no change needed; just used here)
- Test: `tests/authz/test_require_perm.py`

- [ ] **Step 1: Write the failing test**

Create `tests/authz/test_require_perm.py`:

Create `tests/authz/test_require_perm.py`:

```python
"""require_perm dependency: allow / deny (tenant-scoped only, SP1)."""
from __future__ import annotations

import types

import pytest
from fastapi import HTTPException

from dlw.auth.principal import Principal
from dlw.authz.deps import require_perm


class _FakeSession:
    def add(self, *_): ...
    async def commit(self): ...


def _req():
    from dlw.authz.enforcer import build_enforcer
    app = types.SimpleNamespace(state=types.SimpleNamespace(
        casbin=build_enforcer(grants=[])))
    return types.SimpleNamespace(app=app)


async def test_allows_operator_same_tenant():
    dep = require_perm("/api/v1/tasks*", "POST")
    p = Principal(user_id=1, tenant_id=1, role="tenant_operator",
                  project_ids=())
    out = await dep(request=_req(), principal=p, session=_FakeSession())
    assert out is p


async def test_denies_viewer_post():
    dep = require_perm("/api/v1/tasks*", "POST")
    p = Principal(user_id=1, tenant_id=1, role="tenant_viewer",
                  project_ids=())
    with pytest.raises(HTTPException) as e:
        await dep(request=_req(), principal=p, session=_FakeSession())
    assert e.value.status_code == 403
    assert e.value.detail["code"] == "RBAC_DENIED"


async def test_viewer_can_get():
    dep = require_perm("/api/v1/tasks*", "GET")
    p = Principal(user_id=1, tenant_id=1, role="tenant_viewer",
                  project_ids=())
    out = await dep(request=_req(), principal=p, session=_FakeSession())
    assert out is p


async def test_service_principal_short_circuits():
    dep = require_perm("/api/v1/tasks*", "DELETE")
    p = Principal(user_id=0, tenant_id=1, role="system_admin",
                  project_ids=(), is_service=True)
    out = await dep(request=_req(), principal=p, session=_FakeSession())
    assert out is p
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/authz/test_require_perm.py -v`
Expected: FAIL — `dlw.authz.deps` missing.

- [ ] **Step 3: Implement**

Create `src/dlw/authz/deps.py`:

```python
"""require_perm FastAPI dependency factory (Phase 3 SP1, tenant-scoped)."""
from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dlw.auth.principal import Principal, require_principal
from dlw.db.session import get_engine
from dlw.services.audit import write_audit


async def _session() -> AsyncSession:  # pragma: no cover - trivial
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with factory() as s:
        yield s


def require_perm(obj: str, act: str) -> Callable:
    """Dependency: casbin-enforce (role:<principal.role>, tenant, obj, act,
    rtenant). SP1 is tenant-scoped: rtenant == principal.tenant_id for
    collection/create routes, and object routes already re-assert ownership
    via tenant_filtered (cross-tenant id -> 404). system_admin / service
    principals short-circuit allow. Deny -> 403 RBAC_DENIED + audit."""

    async def _dep(
        request: Request,
        principal: Principal = Depends(require_principal),
        session: AsyncSession = Depends(_session),
    ) -> Principal:
        if principal.is_service or principal.role == "system_admin":
            return principal
        enforcer = request.app.state.casbin
        rtenant = principal.tenant_id
        if not enforcer.enforce(
            f"role:{principal.role}", principal.tenant_id, obj, act, rtenant
        ):
            await write_audit(
                session, action="permission_denied", resource_type="route",
                resource_id=f"{act} {obj}", outcome="denied",
                tenant_id=principal.tenant_id,
                actor_user_id=principal.user_id or None)
            await session.commit()
            raise HTTPException(403, detail={"code": "RBAC_DENIED"})
        return principal

    return _dep
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/authz/test_require_perm.py -v`
Expected: PASS (4 cases).

- [ ] **Step 5: Commit**

```bash
git add src/dlw/authz/deps.py tests/authz/test_require_perm.py
git commit -m "feat(sp1): require_perm dependency (tenant-scoped) + deny audit"
```

---

### Task 9: `tenant_filtered` helper + rewire `api/tasks.py`

**Files:**
- Create: `src/dlw/db/tenant_scope.py`
- Modify: `src/dlw/api/tasks.py`
- Delete: `src/dlw/auth/bearer.py` (no remaining caller — verify with grep)
- Test: `tests/api/test_tasks_tenant_scope.py`

- [ ] **Step 1: Write the failing test**

Create `tests/api/test_tasks_tenant_scope.py`:

```python
"""tasks API is principal-scoped + tenant-filtered (Phase 3 SP1)."""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.auth.principal import issue_system_jwt
from dlw.config import get_settings
from dlw.db.base import Base

SECRET = "unit-secret"


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add_all([
            Tenant(id=1, slug="t1", display_name="T1"),
            Tenant(id=2, slug="t2", display_name="T2"),
        ])
        await s.flush()
        s.add_all([
            Project(id=1, tenant_id=1, name="default"),
            Project(id=2, tenant_id=2, name="default"),
            User(id=1, tenant_id=1, oidc_subject="u1", email="u1@t1",
                 role="tenant_operator"),
            User(id=2, tenant_id=2, oidc_subject="u2", email="u2@t2",
                 role="tenant_operator"),
            StorageBackend(id=1, tenant_id=1, name="s1",
                           backend_type="s3", config_encrypted=b""),
            StorageBackend(id=2, tenant_id=2, name="s2",
                           backend_type="s3", config_encrypted=b""),
        ])
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def client(ephemeral_ca):
    from tests.conftest import make_app_with_state
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


def _hdr(uid, tid):
    tok = issue_system_jwt(secret=SECRET, user_id=uid, tenant_id=tid,
                           role="tenant_operator", project_ids=[])
    return {"Authorization": f"Bearer {tok}"}


@pytest.mark.slow
async def test_create_stamps_principal_tenant(client):
    r = await client.post("/api/v1/tasks", json={
        "repo_id": "o/r", "revision": "0" * 40, "storage_id": 1,
    }, headers=_hdr(1, 1))
    assert r.status_code == 201, r.text


@pytest.mark.slow
async def test_list_only_returns_own_tenant(client):
    await client.post("/api/v1/tasks", json={
        "repo_id": "o/a", "revision": "1" * 40, "storage_id": 1,
    }, headers=_hdr(1, 1))
    r2 = await client.get("/api/v1/tasks", headers=_hdr(2, 2))
    assert r2.status_code == 200
    assert all(it["repo_id"] != "o/a" for it in r2.json()["items"])


@pytest.mark.slow
async def test_cross_tenant_get_returns_404(client):
    c = await client.post("/api/v1/tasks", json={
        "repo_id": "o/secret", "revision": "2" * 40, "storage_id": 1,
    }, headers=_hdr(1, 1))
    tid = c.json()["id"]
    r = await client.get(f"/api/v1/tasks/{tid}", headers=_hdr(2, 2))
    assert r.status_code == 404


@pytest.mark.slow
async def test_cross_tenant_cancel_returns_404(client):
    c = await client.post("/api/v1/tasks", json={
        "repo_id": "o/c", "revision": "3" * 40, "storage_id": 1,
    }, headers=_hdr(1, 1))
    tid = c.json()["id"]
    r = await client.post(f"/api/v1/tasks/{tid}/cancel", headers=_hdr(2, 2))
    assert r.status_code == 404


@pytest.mark.slow
async def test_unauth_401(client):
    r = await client.get("/api/v1/tasks")
    assert r.status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/api/test_tasks_tenant_scope.py -v`
Expected: FAIL — routes still use `require_bearer`/constants.

- [ ] **Step 3: Implement**

Create `src/dlw/db/tenant_scope.py`:

```python
"""Mandatory tenant-scoping helper (Phase 3 SP1, Invariant 8).

EVERY business-table query MUST go through this so a forgotten WHERE
tenant_id= can't leak cross-tenant rows. The CI information_schema scan is
the structural backstop; this is the runtime one."""
from __future__ import annotations

from typing import Any

from dlw.auth.principal import Principal


def tenant_filtered(stmt: Any, model: Any, principal: Principal) -> Any:
    return stmt.where(model.tenant_id == principal.tenant_id)
```

Rewrite `src/dlw/api/tasks.py` (drop `_TENANT_ID/_PROJECT_ID/_OWNER_USER_ID`, `require_bearer`; use `Principal` + `require_perm` + `tenant_filtered`):

```python
"""Tasks API: POST / GET list / GET by id / cancel — principal-scoped (SP1)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from dlw.auth.principal import Principal
from dlw.authz.deps import require_perm
from dlw.config import get_settings
from dlw.db.models.task import DownloadTask
from dlw.db.session import get_engine
from dlw.db.tenant_scope import tenant_filtered
from dlw.schemas.task import TaskCreate, TaskDetail, TaskList, TaskRead
from dlw.services.audit import write_audit
from dlw.services.hf_metadata import (
    HfNetworkError,
    HfPrivateOrAuthRequired,
    RepoNotFound,
)
from dlw.services.quota import QuotaExceeded, check_quota_for_new_task
from dlw.services.task_service import EmptyRepo, cancel_task, create_task

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


async def _session():
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with factory() as session:
        yield session


async def _resolve_project(session: AsyncSession, principal: Principal,
                           body: TaskCreate) -> int:
    """Body project_id if the principal's tenant owns it, else the tenant's
    lowest-id project (its default)."""
    from dlw.db.models.tenant import Project
    requested = getattr(body, "project_id", None)
    if requested is not None:
        owns = await session.scalar(
            select(Project.id).where(Project.id == requested,
                                     Project.tenant_id == principal.tenant_id))
        if owns is None:
            raise HTTPException(403, detail={"code": "RBAC_DENIED"})
        return int(requested)
    pid = await session.scalar(
        select(func.min(Project.id)).where(
            Project.tenant_id == principal.tenant_id))
    if pid is None:
        raise HTTPException(409, detail="tenant has no project")
    return int(pid)


@router.post("", status_code=status.HTTP_201_CREATED)
async def post_task(
    body: TaskCreate,
    principal: Principal = Depends(require_perm("/api/v1/tasks*", "POST")),
    session: AsyncSession = Depends(_session),
) -> TaskRead:
    settings = get_settings()
    try:
        await check_quota_for_new_task(session, principal.tenant_id)
    except QuotaExceeded as e:
        # Spec §7 error-matrix + doc 04 §9.2: 429 must be audited.
        await write_audit(
            session, action="quota.exceeded", resource_type="tenant",
            resource_id=str(principal.tenant_id), outcome="denied",
            tenant_id=principal.tenant_id,
            actor_user_id=principal.user_id or None,
            payload={"metric": e.metric})
        await session.commit()
        raise HTTPException(
            status_code=429,
            detail={"code": "QUOTA_EXCEEDED", "metric": e.metric}) from e
    project_id = await _resolve_project(session, principal, body)
    try:
        task = await create_task(
            session, body,
            owner_user_id=principal.user_id, tenant_id=principal.tenant_id,
            project_id=project_id,
            hf_endpoint=settings.hf_endpoint, hf_token=settings.hf_token,
        )
    except RepoNotFound as e:
        raise HTTPException(status_code=404,
                            detail=f"repo or revision not found: {e}") from e
    except HfPrivateOrAuthRequired as e:
        raise HTTPException(
            status_code=422,
            detail=f"repo is private or requires auth — public only: {e}",
        ) from e
    except HfNetworkError as e:
        raise HTTPException(status_code=503,
                            detail=f"huggingface unreachable: {e}") from e
    except EmptyRepo as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    await session.commit()
    return TaskRead.model_validate(task)


@router.get("")
async def list_tasks(
    principal: Principal = Depends(require_perm("/api/v1/tasks*", "GET")),
    session: AsyncSession = Depends(_session),
) -> TaskList:
    rows = (await session.execute(
        tenant_filtered(select(DownloadTask), DownloadTask, principal)
        .order_by(DownloadTask.created_at.desc())
    )).scalars().all()
    total = await session.scalar(
        tenant_filtered(select(func.count()).select_from(DownloadTask),
                        DownloadTask, principal))
    return TaskList(items=[TaskRead.model_validate(r) for r in rows],
                    total=int(total or 0))


@router.get("/{task_id}")
async def get_task(
    task_id: uuid.UUID,
    principal: Principal = Depends(require_perm("/api/v1/tasks*", "GET")),
    session: AsyncSession = Depends(_session),
) -> TaskDetail:
    row = (await session.execute(
        tenant_filtered(
            select(DownloadTask).where(DownloadTask.id == task_id),
            DownloadTask, principal)
        .options(selectinload(DownloadTask.subtasks))
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    return TaskDetail.model_validate(row)


@router.post("/{task_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def post_cancel_task(
    task_id: uuid.UUID,
    principal: Principal = Depends(require_perm("/api/v1/tasks*", "DELETE")),
    session: AsyncSession = Depends(_session),
) -> TaskRead:
    owned = await session.scalar(
        tenant_filtered(select(DownloadTask.id)
                        .where(DownloadTask.id == task_id),
                        DownloadTask, principal))
    if owned is None:
        raise HTTPException(status_code=404, detail="task not found")
    try:
        task = await cancel_task(session, task_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    await session.commit()
    return TaskRead.model_validate(task)
```

`TaskCreate` may not have a `project_id` field — `getattr(body, "project_id", None)` keeps it optional without a schema change (a schema field is a later UI concern; YAGNI for SP1). Delete `src/dlw/auth/bearer.py` after `grep -rn require_bearer src/` returns nothing.

- [ ] **Step 4: Run tests**

Run: `pytest tests/api/test_tasks_tenant_scope.py -v`
Expected: PASS (5 cases). (`check_quota_for_new_task` is created in Task 11 — if implementing strictly in order, add a temporary `src/dlw/services/quota.py` stub now with `QuotaExceeded(Exception)` + `async def check_quota_for_new_task(...): return None`; Task 11 replaces the body. This keeps Task 9 green standalone.)

- [ ] **Step 5: Commit**

```bash
git add src/dlw/db/tenant_scope.py src/dlw/api/tasks.py tests/api/test_tasks_tenant_scope.py
git rm src/dlw/auth/bearer.py
git commit -m "feat(sp1): principal-scoped tasks API + tenant_filtered; drop bearer"
```

---

### Task 10: Migrate existing task tests + conftest fixtures

**Files:**
- Modify: `tests/conftest.py` (add `principal_headers`, `service_headers`; extend `make_app_with_state` to seed `app.state.casbin`)
- Modify: `tests/api/test_tasks.py`, `tests/e2e/test_happy_path.py`, `tests/e2e/test_executor_e2e.py`, `tests/e2e/test_executor_auth_e2e.py`, and any other `require_bearer`/`DLW_BEARER_TOKEN` site surfaced by grep

- [ ] **Step 1: Identify every site**

Run: `grep -rn "DLW_BEARER_TOKEN\|require_bearer\|bearer_token\|Bearer {_TOKEN}\|test-bearer-token" tests/ src/`
List every file. Each task-route test must switch from the bearer token to a system-JWT.

**CRITICAL false-positive guard:** the grep WILL surface `ExecutorSettings(... bearer_token=...)` in `tests/e2e/test_executor_e2e.py` (and `src/dlw/executor/...`). That `bearer_token` belongs to the **executor-side `ExecutorSettings` class** (W2b1) — the executor authenticating its own controller calls — which is a *completely separate* class from `dlw.config.Settings.bearer_token`. **Do NOT remove or rename `ExecutorSettings.bearer_token` or its `DLW_*` env.** SP1 removes ONLY `dlw.config.Settings.bearer_token` and the user-plane `require_bearer`/`DLW_BEARER_TOKEN` usage on the four task routes. Also `docker-compose.dev.yml` and `docs/demo/*` set `DLW_BEARER_TOKEN` for the controller — those are out of test scope; the breaking-change migration (set `DLW_SYSTEM_ADMIN_TOKEN` instead) is documented in Task 14's operator note, not edited here.

- [ ] **Step 2: Add conftest fixtures + extend `make_app_with_state`**

In `tests/conftest.py`, extend `make_app_with_state` (after the `app.state.settings = _gs()` line added in Task 4) with:

```python
    from dlw.authz.enforcer import build_enforcer
    app.state.casbin = build_enforcer(grants=[])
```

Add at module level:

```python
def principal_headers(*, user_id: int = 1, tenant_id: int = 1,
                       role: str = "tenant_operator",
                       project_ids: list[int] | None = None,
                       secret: str = "unit-secret") -> dict[str, str]:
    """Authorization header carrying a freshly minted system-JWT.
    Caller must have set DLW_SYSTEM_JWT_SECRET=secret + cleared the cache."""
    from dlw.auth.principal import issue_system_jwt
    tok = issue_system_jwt(secret=secret, user_id=user_id,
                           tenant_id=tenant_id, role=role,
                           project_ids=project_ids or [])
    return {"Authorization": f"Bearer {tok}"}


def service_headers(token: str = "svc-tok") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
```

- [ ] **Step 3: Migrate `tests/api/test_tasks.py`**

Replace its auth scaffolding: delete `_TOKEN`, the `_set_token` fixture, the `auth` fixture. Add a `_secret` autouse fixture and an `auth` fixture using `principal_headers`; the `client` fixture must use `make_app_with_state` (so `app.state.settings`/`casbin` are seeded):

```python
import pytest
from httpx import ASGITransport, AsyncClient
from dlw.config import get_settings
from dlw.db.base import Base
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.conftest import make_app_with_state, principal_headers

SECRET = "unit-secret"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def auth():
    return principal_headers(secret=SECRET, role="tenant_admin")


@pytest.fixture
async def client(ephemeral_ca):
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c
```

Keep the existing `_bootstrap` (it seeds tenant/project/user/storage id=1) and the per-test HF monkeypatch. All `headers=auth` call sites stay unchanged. `test_post_task_unauthenticated_returns_401` stays valid (no header → 401).

- [ ] **Step 4: Migrate the e2e files**

For each of `tests/e2e/test_happy_path.py`, `tests/e2e/test_executor_e2e.py`, `tests/e2e/test_executor_auth_e2e.py` (and any other grep hit): where they POST `/api/v1/tasks` with the old bearer header, replace the header with `principal_headers(secret=SECRET, role="tenant_admin")` and add the `_secret` autouse fixture (or set `DLW_SYSTEM_JWT_SECRET` in their existing env fixture). Where they build the app inline, switch to `make_app_with_state`. Executor-loop calls (heartbeat/poll/report/register) are unchanged — they use W3a auth, not the principal.

- [ ] **Step 5: Run the full affected set**

Run: `pytest tests/api/test_tasks.py tests/api/test_tasks_tenant_scope.py tests/e2e/ -v`
Expected: PASS. Then full suite: `pytest -q`
Expected: PASS (no `bearer_token`/`require_bearer` references remain).

- [ ] **Step 6: Commit**

```bash
git add tests/
git commit -m "test(sp1): migrate task-route tests to system-JWT principal auth"
```

---

# Milestone M3 — Quota

### Task 11: `services/quota.py` — check / record / aggregate

**Files:**
- Create: `src/dlw/services/quota.py` (replace the Task 9 stub if present)
- Test: `tests/services/test_quota.py`

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_quota.py`:

```python
"""Quota service: strong-consistent check + record + aggregate (SP1)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
from dlw.services.quota import (
    QuotaExceeded,
    aggregate_snapshots,
    check_quota_for_new_task,
    record_usage,
)

pytestmark = pytest.mark.slow


@pytest.fixture
async def seeded(engine):
    from dlw.db.models.tenant import Project, Tenant, User
    from dlw.db.models.usage import QuotaSnapshot
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Tenant(id=1, slug="t", display_name="T",
                     quota_bytes_month=1000, quota_concurrent=2))
        await s.flush()
        s.add_all([Project(id=1, tenant_id=1, name="d"),
                   User(id=1, tenant_id=1, oidc_subject="u",
                        email="u@t", role="tenant_operator"),
                   QuotaSnapshot(tenant_id=1)])
        await s.commit()
    yield factory
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def test_under_limit_passes(seeded):
    async with seeded() as s:
        await check_quota_for_new_task(s, 1)  # no raise


async def test_snapshotless_tenant_is_lockable(seeded):
    """A tenant with NO pre-seeded quota_snapshots row must still be quota-
    checkable: insert-then-lock creates the row so concurrent creates can't
    race past the quota (spec §3.7 / pre-review fix I)."""
    from sqlalchemy import select

    from dlw.db.models.tenant import Tenant
    from dlw.db.models.usage import QuotaSnapshot
    async with seeded() as s:
        s.add(Tenant(id=2, slug="t2", display_name="T2",
                      quota_bytes_month=1000, quota_concurrent=2))
        await s.commit()
        await check_quota_for_new_task(s, 2)  # no raise, no missing-row error
        await s.commit()
        row = (await s.execute(
            select(QuotaSnapshot).where(QuotaSnapshot.tenant_id == 2))
        ).scalar_one()
        assert row.bytes_used_month == 0


async def test_concurrent_limit_blocks(seeded):
    from dlw.db.models.task import DownloadTask
    async with seeded() as s:
        for i in range(2):
            s.add(DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                               repo_id=f"o/r{i}", revision="0" * 40,
                               storage_id=1, path_template="{repo_id}",
                               status="pending"))
        await s.commit()
        with pytest.raises(QuotaExceeded) as e:
            await check_quota_for_new_task(s, 1)
        assert e.value.metric == "concurrent_tasks"


async def test_bytes_limit_blocks(seeded):
    from dlw.db.models.usage import QuotaSnapshot
    async with seeded() as s:
        snap = (await s.execute(
            select(QuotaSnapshot).where(QuotaSnapshot.tenant_id == 1))
        ).scalar_one()
        snap.bytes_used_month = 1000
        await s.commit()
        with pytest.raises(QuotaExceeded) as e:
            await check_quota_for_new_task(s, 1)
        assert e.value.metric == "bytes_month"


async def test_record_usage_appends(seeded):
    from dlw.db.models.usage import UsageRecord
    async with seeded() as s:
        await record_usage(s, tenant_id=1, project_id=1, user_id=1,
                           task_id=uuid.uuid4(), metric="bytes_month",
                           value=500)
        await s.commit()
        rows = (await s.execute(select(UsageRecord))).scalars().all()
        assert len(rows) == 1 and rows[0].value == 500


async def test_aggregate_recomputes_snapshot(seeded):
    from dlw.db.models.usage import QuotaSnapshot
    async with seeded() as s:
        await record_usage(s, tenant_id=1, project_id=1, user_id=1,
                           task_id=uuid.uuid4(), metric="bytes_month",
                           value=300)
        await s.commit()
        await aggregate_snapshots(s)
        await s.commit()
        snap = (await s.execute(
            select(QuotaSnapshot).where(QuotaSnapshot.tenant_id == 1))
        ).scalar_one()
        assert snap.bytes_used_month == 300
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/test_quota.py -v`
Expected: FAIL — real `quota.py` not implemented (or only the stub).

- [ ] **Step 3: Implement**

Create/replace `src/dlw/services/quota.py`:

```python
"""Per-tenant quota (Phase 3 SP1; security §7).

Strong-consistent create check: SELECT ... FOR UPDATE the snapshot row +
a live COUNT(*) of non-terminal tasks. Only the `hard_block` action is
honored in SP1 (throttle/overage are Phase 4)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.task import DownloadTask
from dlw.db.models.tenant import Tenant
from dlw.db.models.usage import QuotaSnapshot, UsageRecord

# Terminal statuses (codebase uses "succeeded", NOT "completed" —
# verified scheduler.py:156/200, tools/lint_invariants.py VALID_TASK_STATUS).
_TERMINAL = ("succeeded", "failed", "cancelled")


class QuotaExceeded(Exception):
    def __init__(self, metric: str) -> None:
        super().__init__(f"quota exceeded: {metric}")
        self.metric = metric


async def check_quota_for_new_task(
    session: AsyncSession, tenant_id: int
) -> None:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise QuotaExceeded("tenant_missing")
    # A tenant provisioned via /auth/callback after migrate-time has no
    # snapshot row until the minute aggregator runs. Without a row,
    # with_for_update() locks nothing and concurrent creates race past the
    # quota. Insert-then-lock guarantees exactly one lockable row (spec §3.7).
    await session.execute(
        pg_insert(QuotaSnapshot).values(tenant_id=tenant_id)
        .on_conflict_do_nothing(index_elements=["tenant_id"]))
    snap = (await session.execute(
        select(QuotaSnapshot)
        .where(QuotaSnapshot.tenant_id == tenant_id)
        .with_for_update()
    )).scalar_one()
    bytes_used = snap.bytes_used_month
    if tenant.quota_bytes_month and bytes_used >= tenant.quota_bytes_month:
        raise QuotaExceeded("bytes_month")
    live_concurrent = await session.scalar(
        select(func.count()).select_from(DownloadTask).where(
            DownloadTask.tenant_id == tenant_id,
            DownloadTask.status.not_in(_TERMINAL))) or 0
    if tenant.quota_concurrent and live_concurrent >= tenant.quota_concurrent:
        raise QuotaExceeded("concurrent_tasks")


async def record_usage(
    session: AsyncSession, *, tenant_id: int, project_id: int | None,
    user_id: int | None, task_id: uuid.UUID | None, metric: str, value: int,
) -> None:
    session.add(UsageRecord(
        tenant_id=tenant_id, project_id=project_id, user_id=user_id,
        task_id=task_id, metric=metric, value=value))


async def aggregate_snapshots(session: AsyncSession) -> None:
    """Recompute every tenant's snapshot from usage_records (month window)
    + live concurrent count. Caller commits."""
    month_start = datetime.now(UTC).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    tenant_ids = (await session.execute(select(Tenant.id))).scalars().all()
    for tid in tenant_ids:
        bytes_used = await session.scalar(
            select(func.coalesce(func.sum(UsageRecord.value), 0)).where(
                UsageRecord.tenant_id == tid,
                UsageRecord.metric == "bytes_month",
                UsageRecord.occurred_at >= month_start)) or 0
        concurrent = await session.scalar(
            select(func.count()).select_from(DownloadTask).where(
                DownloadTask.tenant_id == tid,
                DownloadTask.status.not_in(_TERMINAL))) or 0
        snap = await session.get(QuotaSnapshot, tid)
        if snap is None:
            snap = QuotaSnapshot(tenant_id=tid)
            session.add(snap)
        snap.bytes_used_month = int(bytes_used)
        snap.concurrent_tasks = int(concurrent)
        snap.last_recomputed_at = datetime.now(UTC)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/services/test_quota.py -v`
Expected: PASS (6 cases).

- [ ] **Step 5: Commit**

```bash
git add src/dlw/services/quota.py tests/services/test_quota.py
git commit -m "feat(sp1): quota service — strong-consistent check + usage + aggregate"
```

---

### Task 12: Wire quota into create/complete + leader-gated aggregator + `/quota/current`

**Files:**
- Modify: `src/dlw/services/scheduler.py` (the `complete_subtask` fence path — emit `record_usage`)
- Modify: `src/dlw/main.py` (leader-gated minute aggregator via the existing `_on_active`/`_on_step_down`)
- Create: `src/dlw/api/quota.py`
- Modify: `src/dlw/main.py` `create_app` (mount quota router)
- Test: `tests/api/test_quota_api.py`, `tests/services/test_complete_subtask_usage.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/api/test_quota_api.py`:

```python
"""GET /api/v1/quota/current (Phase 3 SP1)."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base
from tests.conftest import make_app_with_state, principal_headers

SECRET = "unit-secret"
pytestmark = pytest.mark.slow


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.tenant import Project, Tenant, User
    from dlw.db.models.usage import QuotaSnapshot
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=1, slug="t", display_name="T",
                     quota_bytes_month=1000, quota_concurrent=5,
                     quota_storage_gb=10))
        await s.flush()
        s.add_all([Project(id=1, tenant_id=1, name="d"),
                   User(id=1, tenant_id=1, oidc_subject="u", email="u@t",
                        role="tenant_viewer"),
                   QuotaSnapshot(tenant_id=1, bytes_used_month=42)])
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def client(ephemeral_ca):
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


async def test_quota_current_shape(client):
    r = await client.get("/api/v1/quota/current",
                         headers=principal_headers(secret=SECRET,
                                                   role="tenant_viewer"))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["tenant_id"] == 1
    assert b["bytes_used_month"] == 42
    assert b["bytes_quota_month"] == 1000
    assert b["concurrent_quota"] == 5


async def test_quota_current_unauth_401(client):
    assert (await client.get("/api/v1/quota/current")).status_code == 401
```

Create `tests/services/test_complete_subtask_usage.py`:

This test is **self-contained** (mirrors the proven pattern in
`tests/services/test_scheduler.py::test_complete_subtask_succeeds_when_expected_sha_is_null`:
build a pending subtask, flip it to `assigned` with a token, call
`complete_subtask(final_status="succeeded", ...)`). It spies
`dlw.services.scheduler.record_usage` (the name the Step-3 hook imports into
that module's namespace) and asserts it fired with `metric="bytes_month"`.

```python
"""complete_subtask emits a bytes_month usage record (Phase 3 SP1)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.services.scheduler import complete_subtask

pytestmark = pytest.mark.slow


@pytest.fixture
async def seeded(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Tenant(id=1, slug="t", display_name="T"))
        await s.flush()
        s.add_all([
            Project(id=1, tenant_id=1, name="d"),
            User(id=1, tenant_id=1, oidc_subject="u", email="u@t",
                 role="tenant_operator"),
            StorageBackend(id=1, tenant_id=1, name="s",
                           backend_type="s3", config_encrypted=b""),
        ])
        await s.commit()
    yield factory
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def test_complete_subtask_records_usage(seeded, monkeypatch):
    calls = []

    async def spy(session, **kw):
        calls.append(kw)
    monkeypatch.setattr("dlw.services.scheduler.record_usage", spy)

    async with seeded() as s:
        task = DownloadTask(
            tenant_id=1, project_id=1, owner_user_id=1, repo_id="o/r",
            revision="a" * 40, storage_id=1, path_template="t/{tenant}",
            priority=1, status="pending")
        s.add(task)
        await s.flush()
        token = uuid.uuid4()
        sub = FileSubTask(task_id=task.id, tenant_id=1,
                          filename="config.json", file_size=4096,
                          expected_sha256=None, status="assigned",
                          assignment_token=token)
        s.add(sub)
        await s.flush()
        sub_id = sub.id

        sub_done, _ = await complete_subtask(
            s, sub_id, final_status="succeeded", actual_sha256=None,
            bytes_downloaded=4096, error=None, assignment_token=token)
        await s.commit()

    assert sub_done.status == "succeeded"
    assert any(c.get("metric") == "bytes_month" and c.get("value") == 4096
               for c in calls), calls
```

(If `complete_subtask`'s signature differs from what's shown — verify against
`src/dlw/services/scheduler.py` lines ~96-107 during impl — match it exactly;
do NOT invent a shared helper.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/api/test_quota_api.py -v`
Expected: FAIL — no `/api/v1/quota` route.

- [ ] **Step 3: Implement**

Create `src/dlw/api/quota.py`:

```python
"""GET /api/v1/quota/current (Phase 3 SP1; security §7.5, no forecast)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dlw.auth.principal import Principal
from dlw.authz.deps import require_perm
from dlw.db.models.tenant import Tenant
from dlw.db.models.usage import QuotaSnapshot
from dlw.db.session import get_engine

router = APIRouter(prefix="/api/v1/quota", tags=["quota"])


async def _session():
    f = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with f() as s:
        yield s


@router.get("/current")
async def current(
    principal: Principal = Depends(require_perm("/api/v1/quota*", "GET")),
    session: AsyncSession = Depends(_session),
) -> dict:
    tenant = await session.get(Tenant, principal.tenant_id)
    if tenant is None:
        raise HTTPException(404, detail="tenant not found")
    snap = await session.get(QuotaSnapshot, principal.tenant_id)
    return {
        "tenant_id": tenant.id,
        "bytes_used_month": snap.bytes_used_month if snap else 0,
        "bytes_quota_month": tenant.quota_bytes_month,
        "storage_gb_used": snap.storage_gb_used if snap else 0,
        "storage_gb_quota": tenant.quota_storage_gb,
        "concurrent_tasks": snap.concurrent_tasks if snap else 0,
        "concurrent_quota": tenant.quota_concurrent,
    }
```

In `src/dlw/main.py` `create_app()` add `from dlw.api.quota import router as quota_router; app.include_router(quota_router)`.

In `src/dlw/main.py` lifespan, add a leader-gated minute aggregator alongside the W3c sweep. Extend the `sweep_task_holder` pattern with a second holder:

```python
    quota_task_holder: dict[str, asyncio.Task | None] = {"t": None}

    async def _quota_loop() -> None:
        from dlw.services.quota import aggregate_snapshots
        while True:
            try:
                await asyncio.sleep(60)
                async with factory() as session:
                    await aggregate_snapshots(session)
                    await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("quota aggregator tick failed; retrying")
```

In `_on_active`, add (alongside the existing `sweep_task_holder["t"] = ...` line):

```python
        quota_task_holder["t"] = asyncio.create_task(_quota_loop())
```

In `_on_step_down`, add the symmetric teardown right after the existing
sweep-task cancel block (mirror it exactly — same `wait_for(timeout=2)`):

```python
        qt = quota_task_holder["t"]
        if qt is not None:
            qt.cancel()
            try:
                await asyncio.wait_for(qt, timeout=2)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            quota_task_holder["t"] = None
```

(The lifespan `finally:` already calls `_on_step_down()` once before
`elector.release()`, so both tasks are torn down on shutdown — no extra
edit there. `_quota_loop`/`quota_task_holder` are defined in the same
lifespan scope as `factory`/`sweep_task_holder`, so the closures resolve.)

**Scheduler hook.** In `src/dlw/services/scheduler.py`, add a **module-top**
import (NOT a function-local import — the Task-12 test does
`monkeypatch.setattr("dlw.services.scheduler.record_usage", spy)`, which only
works if the name lives in the module namespace; no circular import:
`quota` imports only models, never `scheduler`):

```python
from dlw.services.quota import record_usage
```

Then in `complete_subtask`, immediately AFTER the block that sets
`sub.status = final_status` / `sub.completed_at` / optional `sub.s3_key`
(currently scheduler.py ~line 190) and BEFORE `parent = await session.get(DownloadTask, sub.task_id, with_for_update=True)`, insert:

```python
    if final_status == "succeeded":
        await record_usage(
            session, tenant_id=sub.tenant_id, project_id=None,
            user_id=None, task_id=sub.task_id, metric="bytes_month",
            value=sub.bytes_downloaded or 0)
```

Rationale for placement: the W4 sha256 gate (scheduler.py ~173-182) may have
already reassigned `final_status` to `"failed"`; gating on the *resolved*
`final_status == "succeeded"` here means a sha-mismatch failure correctly
records NO usage. `record_usage` only `session.add`s — the existing
caller-commit (POST /report → `await session.commit()`) covers it; do NOT
add a commit inside `complete_subtask`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/api/test_quota_api.py tests/services/test_complete_subtask_usage.py tests/services/test_quota.py -v`
Expected: PASS (quota API 2 cases; the self-contained completion-usage test asserts the `record_usage` spy fired with `metric="bytes_month"`, `value=4096`).
Then: `pytest -q` — full suite green.

- [ ] **Step 5: Commit**

```bash
git add src/dlw/api/quota.py src/dlw/main.py src/dlw/services/scheduler.py tests/api/test_quota_api.py tests/services/test_complete_subtask_usage.py
git commit -m "feat(sp1): /quota/current + leader-gated aggregator + usage on complete"
```

---

# Milestone M4 — Isolation E2E + Docs + PR

### Task 13: `E2E-MT-*` tenant isolation end-to-end

**Files:**
- Create: `tests/e2e/test_tenant_isolation.py`

- [ ] **Step 1: Write the failing test**

Create `tests/e2e/test_tenant_isolation.py`:

```python
"""E2E-MT-*: cross-tenant isolation + quota isolation + service token."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base
from tests.conftest import (
    make_app_with_state,
    principal_headers,
    service_headers,
)

SECRET = "unit-secret"
SVC = "svc-tok"
pytestmark = pytest.mark.slow


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    from dlw.db.models.usage import QuotaSnapshot
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add_all([
            Tenant(id=1, slug="a", display_name="A", quota_concurrent=1),
            Tenant(id=2, slug="b", display_name="B", quota_concurrent=50),
        ])
        await s.flush()
        s.add_all([
            Project(id=1, tenant_id=1, name="d"),
            Project(id=2, tenant_id=2, name="d"),
            User(id=1, tenant_id=1, oidc_subject="a1", email="a@a",
                 role="tenant_operator"),
            User(id=2, tenant_id=2, oidc_subject="b1", email="b@b",
                 role="tenant_operator"),
            StorageBackend(id=1, tenant_id=1, name="s1",
                           backend_type="s3", config_encrypted=b""),
            StorageBackend(id=2, tenant_id=2, name="s2",
                           backend_type="s3", config_encrypted=b""),
            QuotaSnapshot(tenant_id=1), QuotaSnapshot(tenant_id=2),
        ])
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    monkeypatch.setenv("DLW_SYSTEM_ADMIN_TOKEN", SVC)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def client(ephemeral_ca):
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


def _a():
    return principal_headers(secret=SECRET, user_id=1, tenant_id=1)


def _b():
    return principal_headers(secret=SECRET, user_id=2, tenant_id=2)


async def test_tenant_b_cannot_see_tenant_a_task(client):
    c = await client.post("/api/v1/tasks", json={
        "repo_id": "o/a-secret", "revision": "0" * 40, "storage_id": 1,
    }, headers=_a())
    assert c.status_code == 201, c.text
    tid = c.json()["id"]
    assert (await client.get(f"/api/v1/tasks/{tid}",
                             headers=_b())).status_code == 404
    lst = await client.get("/api/v1/tasks", headers=_b())
    assert all(i["repo_id"] != "o/a-secret" for i in lst.json()["items"])


async def test_tenant_a_quota_exhaustion_does_not_block_b(client, engine):
    # tenant A quota_concurrent=1 — first task ok, second 429
    r1 = await client.post("/api/v1/tasks", json={
        "repo_id": "o/a1", "revision": "1" * 40, "storage_id": 1,
    }, headers=_a())
    assert r1.status_code == 201
    r2 = await client.post("/api/v1/tasks", json={
        "repo_id": "o/a2", "revision": "2" * 40, "storage_id": 1,
    }, headers=_a())
    assert r2.status_code == 429
    assert r2.json()["detail"]["code"] == "QUOTA_EXCEEDED"
    # spec §7 / doc 04 §9.2: the 429 must be audited as quota.exceeded
    from sqlalchemy import select
    from dlw.db.models.audit import AuditLog
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        rows = (await s.execute(
            select(AuditLog).where(AuditLog.action == "quota.exceeded",
                                   AuditLog.tenant_id == 1))).scalars().all()
    assert len(rows) >= 1
    # tenant B (quota 50) unaffected
    rb = await client.post("/api/v1/tasks", json={
        "repo_id": "o/b1", "revision": "3" * 40, "storage_id": 2,
    }, headers=_b())
    assert rb.status_code == 201, rb.text


async def test_service_token_acts_as_tenant_1(client):
    r = await client.get("/api/v1/auth/me", headers=service_headers(SVC))
    assert r.status_code == 200
    assert r.json()["tenant_id"] == 1 and r.json()["is_service"] is True
```

- [ ] **Step 2: Run test to verify it fails (then passes)**

Run: `pytest tests/e2e/test_tenant_isolation.py -v`
Expected: PASS if M1–M3 complete (this is an integration assertion, not new product code). If any case fails, fix the underlying SP1 code (not the test) and re-run — this is the milestone's acceptance gate.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_tenant_isolation.py
git commit -m "test(sp1): E2E-MT cross-tenant + quota isolation + service token"
```

---

### Task 14: OpenAPI + operator doc + full-suite/lint + PR

**Files:**
- Modify: `api/openapi.yaml`
- Create: `docs/operator/multi-tenancy.md`

- [ ] **Step 1: Update `api/openapi.yaml`**

Add the 4 operations (`GET /api/v1/auth/login`, `/auth/callback`, `/auth/me`, `GET /api/v1/quota/current`). On the 4 task operations add response variants `401`, `403` (`RbacDenied`), `404`, and `429` (`QuotaExceeded`) on `POST /api/v1/tasks`; add components `RbacDenied {code}`, `QuotaExceeded {code, metric}`, `TenantUnresolved {code, message}`. Remove any `bearerAuth`-as-static-token note tied to the deleted `bearer_token`; describe the system-JWT bearer scheme. Match the existing yaml's style/indentation exactly.

- [ ] **Step 2: Run the OpenAPI CI checks locally (exact CI commands)**

There is NO code-vs-yaml diff in CI. The `openapi` job is exactly:

```bash
npm install -g @stoplight/spectral-cli@6 @apidevtools/swagger-cli
cat > .spectral.yaml <<'EOF'
extends: spectral:oas
rules:
  oas3-unused-component: warn
  operation-tag-defined: warn
EOF
spectral lint api/openapi.yaml --fail-severity=error
swagger-cli validate api/openapi.yaml
```

Run those. `--fail-severity=error` + the two rules downgraded to `warn` means unused components only warn (won't fail), but `swagger-cli validate` WILL fail on any unresolved `$ref`, so every new component must be referenced and well-formed. Also: `api/` is yamllinted (Step 4) — keep indentation/line-length consistent with the existing `api/openapi.yaml` and `.yamllint.yml`. Delete the temp `.spectral.yaml` afterward (do not commit it — it was an accidental artifact in a prior cycle). Fix until both commands are clean.

- [ ] **Step 3: Write `docs/operator/multi-tenancy.md`**

Cover: OIDC env vars (`DLW_OIDC_*`, `DLW_SYSTEM_JWT_SECRET`, `DLW_AUTH_TENANT_RULES_JSON` format with an example), the `DLW_SYSTEM_ADMIN_TOKEN` service-token (non-interactive CLI/test path), `DLW_AUTH_DEV_MODE` (CI/local only — never prod), the **breaking change**: `DLW_BEARER_TOKEN` removed (single-token deployments set `DLW_SYSTEM_ADMIN_TOKEN` instead; real users configure OIDC), the single-tenant default (legacy data = tenant 1, no data migration), and that the SP1 startup guard fails closed on insecure prod config. Cross-ref `docs/v2.0/04-security-and-tenancy.md §1/§7` and `INVARIANTS §8`.

- [ ] **Step 4: Full suite + the real CI gates locally**

Run the actual CI-gating commands (verified against `.github/workflows/ci.yml`):

```bash
uv lock && uv sync --all-groups
uv run pytest tests/ --cov=src/dlw --cov-report=term-missing   # the `pytest` job
python -m pytest tools/test_lint_invariants.py -v              # invariant_lint job
python tools/lint_invariants.py                                #   "
python tools/lint_no_direct_status_write.py                    #   "
```

Expected: all green. There is **no** information_schema/tenant_id table scan and **no** ruff/mypy CI job — `lint_invariants.py` is a *source AST* check; it scans `src/dlw/api/tasks.py`, `services/task_service.py`, `services/scheduler.py` for any task `status` literal not in `VALID_TASK_STATUS` and any `status="..."` kwarg not in `VALID_SUBTASK_STATUS`. SP1's only edits to those files (Task 9 `tasks.py`, Task 12 `scheduler.py`) use no status literals except the valid `"succeeded"` in the scheduler hook — confirm `python tools/lint_invariants.py` exits 0. (Optionally also run `ruff check src tests` + `mypy src` as LOCAL quality gates — they are not CI gates and do not block the PR; `uv.lock` MUST be committed so CI's `uv sync --all-groups` succeeds.) For markdown/yaml: CI markdownlint only covers `docs/v2.0/*.md README.md CONTRIBUTING.md` and yamllint covers `deploy/ api/` — so `api/openapi.yaml` must pass `yamllint -c .yamllint.yml api/`; the SP1 spec/plan/operator docs are not CI-linted.

- [ ] **Step 5: Commit + push + PR**

```bash
git add api/openapi.yaml docs/operator/multi-tenancy.md
git commit -m "docs(sp1): OpenAPI + operator multi-tenancy guide"
git push -u origin feat/phase-3-sp1-multi-tenancy
```

Then open the PR:

```bash
gh pr create --title "Phase 3 SP1 — Multi-tenancy (OIDC + RBAC + tenant scoping + quota)" --body "$(cat <<'EOF'
## Summary
- OIDC login/callback + system-JWT principal; casbin RBAC (`require_perm`, tenant-scoped); mandatory `tenant_filtered` scoping (Invariant 8); per-tenant quota (strong-consistent create check + event-sourced usage + leader-gated minute snapshots).
- Removes the single shared `bearer_token`; adds `system_admin` service token + `auth_dev_mode` for the IdP-free path.
- One alembic migration (usage_records / quota_snapshots / casbin_rule); fail-closed startup guard.
- Project-scoped RBAC deferred (SP1 is tenant-scoped only). Closes G1/G2 (user-plane). Phase 3 sub-project 1 of 4.

## Test plan
- [ ] `uv run pytest tests/ --cov=src/dlw` green (incl. `E2E-MT-*` `tests/e2e/test_tenant_isolation.py`)
- [ ] `python -m pytest tools/test_lint_invariants.py` + `python tools/lint_invariants.py` + `python tools/lint_no_direct_status_write.py` green
- [ ] `spectral lint api/openapi.yaml --fail-severity=error` + `swagger-cli validate api/openapi.yaml` clean; `yamllint -c .yamllint.yml api/` clean
- [ ] `uv.lock` updated for `authlib`/`casbin` (CI `uv sync --all-groups`)
- [ ] `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` clean

Spec: `docs/superpowers/specs/2026-05-18-phase-3-sp1-multi-tenancy-design.md`
Plan: `docs/superpowers/plans/2026-05-18-phase-3-sp1-multi-tenancy.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review (completed during planning + revised after 2-reviewer pre-execution review)

**Pre-execution review applied (2026-05-18):** Two independent opus reviewers found 4–5 BLOCKER + 5 IMPORTANT plan-level defects, all verified against code and fixed inline before any implementation: (1) `"completed"`→`"succeeded"` real terminal status (T11 `_TERMINAL`, T12 scheduler hook); (2) model registration in `db/models/__init__.py` not `base.py` (T5); (3) the "information_schema tenant_id scan" does not exist — real Invariant-8 CI gate is the source AST `tools/lint_invariants.py` (T5/T14, removed casbin_rule-allowlist no-op); (4) T12 completion test made self-contained (no non-existent shared helper); (5) NOT-NULL quota/is_active values in migration+seed (T5); (6) project_match dropped — SP1 is tenant-scoped only (callback issues `project_ids=[]`; FastAPI can't plumb per-row `rproject`) → T7/T8 simplified to `enforce()` bool; (7) `quota.exceeded` audit added (T9); (8) casbin `keyMatch`+anchored `^(...)$` regex + a real install-and-run step (T7); (9) snapshot-less-tenant insert-then-lock race fix (T11); (10) `ExecutorSettings.bearer_token` false-positive guard (T10); (11) accurate CI command list — no ruff/mypy/code-vs-yaml CI jobs (Conventions + T14).

**Spec coverage:** OIDC+system-JWT → T2/T3/T4; Principal/service-token → T2; casbin RBAC (tenant-scoped) → T7/T8; tenant_id scoping (Inv 8) → T9 + `tenant_scope.py`; quota strong-check/usage/snapshots → T11/T12; `/quota/current` → T12; migration+seed → T5; startup guard → T6; audit on login/deny/quota.exceeded → T4 (`audit.py`) + T8 + T9; OpenAPI + operator doc → T14; `E2E-MT-*` → T13; existing-test migration → T10; `bearer_token` removal → T1/T9. Project-scoped RBAC + `storage_gb`/`throttle` quota are explicitly deferred (spec §1.3 non-goals; recorded in spec §3.3 update).

**Type consistency (re-verified post-fix):** `Principal(user_id,tenant_id,role,project_ids,is_service)` identical across T2/T8/T9/T13. `issue_system_jwt` kwargs match T2↔conftest↔tests. `QuotaExceeded(metric)` consistent T9(stub)/T11/T12. `require_perm(obj, act)` signature (no `rproject`) consistent T8↔T9↔quota T12. `build_enforcer(*, grants=...)` + `enforce(...)→bool` consistent T7↔T8↔conftest. `tenant_filtered(stmt, model, principal)` consistent T9↔helper. `write_audit(...)` kwargs consistent T4↔T8↔T9. `_TERMINAL=("succeeded","failed","cancelled")` consistent across `check_quota_for_new_task`/`aggregate_snapshots`; scheduler hook gates on resolved `final_status=="succeeded"`.

---

## References
- Spec: `docs/superpowers/specs/2026-05-18-phase-3-sp1-multi-tenancy-design.md`
- Security/tenancy doc: `docs/v2.0/04-security-and-tenancy.md` §1, §1.3, §1.4, §2.1, §7, §9.2
- Invariant 8: `docs/v2.0/INVARIANTS.md` row 8
- Predecessor (leader-gating reused for the aggregator): `docs/superpowers/specs/2026-05-15-phase-2-w3c-active-standby-design.md`
- Branch `feat/phase-3-sp1-multi-tenancy` off `main` (`db86791`), spec committed `170a54d`
