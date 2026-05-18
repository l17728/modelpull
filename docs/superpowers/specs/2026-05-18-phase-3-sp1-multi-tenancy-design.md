# Phase 3 SP1 — Multi-Tenancy (OIDC + RBAC + tenant scoping + quota) Design

> **Status:** Draft (brainstormed 2026-05-18).
> **Companion plan:** `docs/superpowers/plans/2026-05-18-phase-3-sp1-multi-tenancy.md` (to be written by writing-plans skill after spec approval).
> **Roadmap source:** `docs/v2.0/08-mvp-roadmap.md` §3 — Phase 3 ("平台化") Week 1 ("多租户底层"): OIDC + RBAC(casbin) + 全表 tenant_id + 配额. §3.4 entry criterion ("Phase 2 出场标准全满足"); §3.5 exit (`E2E-MT-*` 多租户隔离, 跨租户隔离).
> **Phase 3 decomposition:** Phase 3 is four independent sub-projects — **SP1 multi-tenancy** (this spec), SP2 multi-source+routing, SP3 incremental download, SP4 CLI+SDK. SP1 is the foundation everything else authorizes/routes through. SP2–SP4 get their own spec→plan→implementation cycles.
> **Security/tenancy source:** `docs/v2.0/04-security-and-tenancy.md` §1 (租户与身份模型), §1.4 (RBAC casbin policy), §2.1 (用户侧 OIDC+JWT), §7 (配额与计量), §9.2 (审计事件).
> **Invariant source:** `docs/v2.0/INVARIANTS.md` row 8 ("业务表必须有 `tenant_id`"; CI information_schema 扫描).
> **Closes:** G1 (租户与身份模型), G2 (配额与计量) — the user-plane half. License/gated/audit-chain governance (G3 / SEC-09 chain-hash) stays Phase 4.

---

## 1. Goal & Scope

### 1.1 Goal

Turn the single-tenant PoC into a real multi-tenant platform: every user-facing request is authenticated to an OIDC-provisioned **User**, authorized by **casbin RBAC**, scoped to that user's **Tenant** on every business query, and metered against per-tenant **quota**.

**Mechanism.** A user authenticates via OIDC authorization-code flow (`/api/v1/auth/login` → IdP → `/api/v1/auth/callback`). The controller upserts a `User` keyed by `oidc_subject`, then issues a short-lived **system-JWT** (`{tenant_id, user_id, role, project_ids}`). Every subsequent API call carries that JWT; a `require_principal` dependency decodes it into a request-scoped `Principal`, and a `require_perm(obj, act)` casbin dependency replaces the single shared bearer token on user-facing routes. Handlers read `principal.tenant_id` instead of the hard-coded `_TENANT_ID = 1`. Task creation runs a strong-consistent quota check; completed work is recorded into an append-only `usage_records` stream aggregated each minute into `quota_snapshots`.

After SP1, single-tenant deployments still work: a `default` tenant (id=1, the existing seeded data) plus a non-interactive `system_admin` service token preserve the IdP-free path that tests, the executor enrollment flow, and (later) the SP4 CLI/SDK depend on.

### 1.2 In scope

| Item | Where |
|---|---|
| OIDC authorization-code client + user upsert + system-JWT issuance | `src/dlw/auth/oidc.py` (new), `src/dlw/api/auth.py` (new: `/auth/login`, `/auth/callback`, `/auth/me`) |
| `Principal` + `require_principal` (decode system-JWT; `system_admin` service-token bypass) | `src/dlw/auth/principal.py` (new) |
| casbin enforcer + `tenant_match` / `project_match` matchers + `require_perm(obj, act)` dep factory | `src/dlw/authz/enforcer.py` (new), `src/dlw/authz/model.conf` + `src/dlw/authz/policy.csv` (new) |
| Tenant-scoped query helper (centralizes `WHERE tenant_id =`) | `src/dlw/db/tenant_scope.py` (new) |
| `api/tasks.py` rewire: drop `_TENANT_ID/_PROJECT_ID/_OWNER_USER_ID` constants; use `Principal`; replace `require_bearer` with `require_perm` | `src/dlw/api/tasks.py` |
| Quota service: strong-consistent create check + usage recording + minute aggregator | `src/dlw/services/quota.py` (new) |
| `UsageRecord` + `QuotaSnapshot` + `CasbinRule` models + one alembic migration | `src/dlw/db/models/usage.py` (new), `src/dlw/db/models/casbin_rule.py` (new), `src/dlw/alembic/versions/<rev>_p3sp1_tenancy_quota.py` (new) |
| Audit-log writes for auth + authz-deny + quota events (using the existing `AuditLog` model) | `src/dlw/services/audit.py` (new thin helper) |
| `main.py`: mount auth router, bootstrap casbin enforcer into `app.state`, leader-gated minute quota-aggregation loop | `src/dlw/main.py` |
| Config: OIDC issuer/client/secret/redirect, `system_jwt_secret`, `system_admin_token`, `auth_dev_mode` | `src/dlw/config.py` |
| `GET /api/v1/quota/current` | `src/dlw/api/quota.py` (new) |
| Multi-tenant isolation e2e (`E2E-MT-*`) | `tests/e2e/test_tenant_isolation.py` (new) |
| Operator/onboarding note: OIDC env, service token, dev mode, single-tenant default | `docs/operator/multi-tenancy.md` (new) |

### 1.3 Non-goals (deferred — explicit list)

| Item | Where |
|---|---|
| License white/blacklist, gated-model approval, export-control, trust_remote_code workflow (G3) | **Phase 4** §4.2 (合规与审批). |
| Audit-log chain-hash trigger + WORM parquet export (SEC-09 tamper-evidence) | **Phase 4** — SP1 only *writes* audit rows via the existing `AuditLog` schema; `prev_hash`/`self_hash` chaining + verifier + S3 Object-Lock export is Phase 4. |
| `audit_reader` cross-tenant role; AI/Copilot RBAC (Invariant 15) | **Phase 4 / v2.1** — roles limited to those current endpoints exercise. |
| Quota `throttle` / `overage_billing` actions; chargeback PDF/CSV; ML usage forecast | **Phase 4 / v2.1** — SP1 ships `hard_block` only; the action column exists but only `hard_block` is honored. |
| `/api/quota/usage` reporting + `/api/quota/forecast` | **Phase 4** — SP1 ships only `GET /api/v1/quota/current`. |
| Tenant/Project/User admin CRUD UI + tenant-onboarding approval workflow | deferred — SP1 provisions users JIT on first OIDC login via configured email-domain→tenant rules; admin CRUD is a later UI sub-project. Unmapped users → 403 (no auto-tenant-create). |
| Per-tenant HF token reverse-proxy lookup change | already correct — W3b's `hf_proxy` resolves the token via `subtask.task.tenant_id`; SP1 makes `task.tenant_id` real instead of constant `1`, no proxy code change. |
| `tenant_id` on the **executor** pool | out of scope by design — executors are a system-shared pool (`executors.tenant_id` stays nullable, doc 04 §2.2.1). Heartbeat/poll/report stay executor-JWT-authed (W3a), **not** principal-scoped. |
| PG row-level security / ORM global criteria enforcement | rejected in §4 (approach B) — explicit dependency-layer scoping + the existing CI Invariant-8 scan is the chosen guarantee. |
| Prometheus `tenant_id` metric labels (doc 04 §10) | observability polish — Phase 4 §4.2 (SLI/SLO). |

---

## 2. Tech Stack Additions

| Dep | Why | Notes |
|---|---|---|
| `authlib>=1.3,<2.0` (runtime) | OIDC authorization-code client (doc 04 §1.2 names Authlib) | pure-Python; used only for the code↔token exchange + JWKS verify of the IdP token. Our own system-JWT uses the already-present `pyjwt`. |
| `casbin>=1.36,<2.0` (runtime) | RBAC enforcer (doc 04 §1.4 names casbin) | pure-Python; in-memory adapter seeded from `policy.csv` + DB `casbin_rule` for per-subject grants. No `casbin-sqlalchemy-adapter` dep — we load grants ourselves (one query at bootstrap + on grant change). |

Reused (no new dep): `pyjwt[crypto]` (system-JWT, HS256 with `system_jwt_secret`), `httpx` (Authlib transport), SQLAlchemy async, FastAPI deps, `structlog` (audit/log).

**One alembic migration** (first since W2b2 — W3a/W3b/W3c were migration-free). No new CI jobs. The existing 12 CI checks all still apply; the Invariant-8 information_schema scan must stay green (it already passes — columns exist; SP1 keeps them NOT NULL where they already are).

---

## 3. Components

### 3.1 New: `src/dlw/auth/principal.py`

```python
"""Request principal decoded from the system-JWT (Phase 3 SP1).

The system-JWT is issued by /auth/callback after OIDC. It is HS256-signed
with settings.system_jwt_secret (shared across active/standby — both must
verify the same user tokens, so it's a config secret, not a per-instance
bootstrapped keypair like the executor EdDSA key)."""
from __future__ import annotations

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
    role: str                 # system_admin | tenant_admin | tenant_operator |
                              # tenant_viewer | project_owner | project_member
    project_ids: tuple[int, ...]
    is_service: bool = False  # True for the system_admin service-token path


def issue_system_jwt(*, secret: str, user_id: int, tenant_id: int,
                     role: str, project_ids: list[int],
                     ttl_seconds: int = SYSTEM_JWT_TTL_SECONDS) -> str:
    now = int(time.time())
    return _pyjwt.encode(
        {"iss": SYSTEM_JWT_ISS, "sub": str(user_id), "tid": tenant_id,
         "role": role, "pids": project_ids, "iat": now, "exp": now + ttl_seconds},
        secret, algorithm=SYSTEM_JWT_ALG,
    )


async def require_principal(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Principal:
    """Decode system-JWT → Principal. The system_admin service token
    (settings.system_admin_token, constant-time compared) yields a
    service Principal bound to the default tenant (id=1)."""
    settings = request.app.state.settings
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
    token = authorization.removeprefix("Bearer ").strip()

    # system_admin service token (non-interactive: CLI/SDK/tests).
    svc = settings.system_admin_token
    if svc and _ct_eq(token, svc):
        return Principal(user_id=0, tenant_id=1, role="system_admin",
                         project_ids=(), is_service=True)
    try:
        claims = _pyjwt.decode(token, settings.system_jwt_secret,
                               algorithms=[SYSTEM_JWT_ALG], issuer=SYSTEM_JWT_ISS,
                               options={"require": ["sub", "tid", "role", "exp", "iss", "iat"]})
    except _pyjwt.PyJWTError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {e}",
                            headers={"WWW-Authenticate": "Bearer"}) from e
    return Principal(user_id=int(claims["sub"]), tenant_id=int(claims["tid"]),
                     role=claims["role"], project_ids=tuple(claims.get("pids", [])))
```

`_ct_eq` is a constant-time compare: `secrets.compare_digest(a.encode(), b.encode())` (the same constant-time pattern the soon-to-be-removed `auth/bearer.py` used).

### 3.2 New: `src/dlw/auth/oidc.py` + `src/dlw/api/auth.py`

`oidc.py` wraps `authlib.integrations.httpx_client.AsyncOAuth2Client`:

- `build_authorize_url(state) -> str` — PKCE S256, scopes `openid email profile groups`.
- `exchange_code(code, state) -> OidcClaims` — code→token, verify IdP id_token via JWKS, return `{sub, email, groups}`.
- **Dev mode** (`settings.auth_dev_mode=True`): `exchange_code` skips the network and returns claims decoded from a locally HS256-signed token using `system_jwt_secret` (a *static issuer* for hermetic CI — same philosophy as Phase 2's enrollment token / local-PG-no-Docker). Real OIDC code path is exercised by a unit test with a stub JWKS.

`api/auth.py` routes (no `require_principal` — these *establish* it):

| Route | Behaviour |
|---|---|
| `GET /api/v1/auth/login` | 307 → IdP authorize URL; sets signed `state` cookie (SameSite=Strict). Dev mode: 307 → `/auth/callback?code=dev:<email>&state=...`. |
| `GET /api/v1/auth/callback?code&state` | validate state; `exchange_code`; **resolve tenant** (see 3.6); upsert `User` by `oidc_subject`; audit `login`; return `{system_jwt, expires_in, tenant_id, role}`. |
| `GET /api/v1/auth/me` | `Depends(require_principal)` → echo the principal (UI bootstrap; SP4 SDK `whoami`). |

### 3.3 New: `src/dlw/authz/` — casbin RBAC

`model.conf` (RBAC + ABAC matcher, doc 04 §1.4):

```ini
[request_definition]
r = sub, tenant, obj, act, rtenant, rproject
[policy_definition]
p = sub, obj, act, scope
[role_definition]
g = _, _
[policy_effect]
e = some(where (p.eft == allow))
[matchers]
m = g(r.sub, p.sub) && keyMatch2(r.obj, p.obj) && regexMatch(r.act, p.act) \
    && ( p.scope == "any" || r.tenant == r.rtenant )
```

casbin owns role→obj→act + the tenant equality (`r.tenant` = principal tenant, `r.rtenant` = the resource's tenant; for collection routes like `POST /tasks` they're equal by construction). The **project-scope** narrowing (`project_owner`/`project_member` may only touch their own projects) is *not* expressed in the casbin matcher — casbin returns the matched policy row's `scope`, and `require_perm` then enforces it in Python: if the winning policy's `scope == "project_match"`, `require_perm` additionally asserts `rproject in principal.project_ids` (raising 403 otherwise). This keeps the casbin model free of request-side list membership (which casbin matchers handle poorly) and keeps `rproject` the only project field. `rproject`/`rtenant` for object-bound routes (`GET/DELETE /tasks/{id}`, cancel) come from the loaded `DownloadTask` row; for collection/create routes they default to the principal's own tenant and the resolved project.

`policy.csv` — role→permission rows scoped to current endpoints only:

```csv
p, role:system_admin,    /api/v1/*,          (GET|POST|DELETE|PUT), any
p, role:tenant_admin,    /api/v1/tasks*,     (GET|POST|DELETE),     tenant_match
p, role:tenant_admin,    /api/v1/quota*,     GET,                   tenant_match
p, role:tenant_operator, /api/v1/tasks*,     (GET|POST|DELETE),     tenant_match
p, role:tenant_viewer,   /api/v1/tasks*,     GET,                   tenant_match
p, role:tenant_viewer,   /api/v1/quota*,     GET,                   tenant_match
p, role:project_member,  /api/v1/tasks*,     (GET|POST),            project_match
p, role:project_owner,   /api/v1/tasks*,     (GET|POST|DELETE),     project_match
```

`enforcer.py`:

- `build_enforcer(grants: list[tuple[str,str]]) -> casbin.Enforcer` — loads `model.conf`+`policy.csv` (string adapter), then adds `g` grants. **role mapping**: `g, role:<user.role>, role:<user.role>` is implicit; per-subject grants (`g, user:<id>, role:project_owner`, plus `project:<id>` membership) are loaded from the DB `casbin_rule` table at bootstrap. SP1's default: a user's `users.role` column *is* the grant; the `casbin_rule` table is created and used for project-scoped grants and is the extension point for later admin CRUD.
- `require_perm(obj_template: str, act: str)` — FastAPI dependency factory. Resolves `Principal` (via `require_principal`); `system_admin` short-circuits allow. Computes the request's `rtenant`/`rproject` (object-bound routes load the `DownloadTask` row — see 3.5; collection/create routes use the principal's tenant + resolved project). Calls `enforcer.enforce_ex(...)` to get both the allow decision **and** the matched policy's `scope`. On not-allowed → `403 {"code":"RBAC_DENIED"}`. If allowed and the matched `scope == "project_match"`, additionally require `rproject in principal.project_ids` (else `403 RBAC_DENIED`). Every deny → audit `permission_denied`. Returns the `Principal` so handlers reuse it.

### 3.4 New: `src/dlw/db/tenant_scope.py`

A tiny, mandatory helper so no business query forgets the filter (Invariant 8):

```python
def tenant_filtered(stmt, model, principal: Principal):
    """Append WHERE model.tenant_id == principal.tenant_id.
    system_admin with no explicit X-Tenant-Id override still scopes to its
    bound tenant (id=1) — cross-tenant admin reads are a Phase-4 concern."""
    return stmt.where(model.tenant_id == principal.tenant_id)
```

`api/tasks.py` `list_tasks` / `get_task` / `post_cancel_task` route every `select(DownloadTask)` / mutation through `tenant_filtered(...)`. `get_task` / cancel also re-assert `row.tenant_id == principal.tenant_id` defensively (a cross-tenant id returns **404**, not 403 — don't leak existence).

### 3.5 Modified: `src/dlw/api/tasks.py`

- Delete `_TENANT_ID/_PROJECT_ID/_OWNER_USER_ID` constants.
- `post_task`: `principal = Depends(require_perm("/api/v1/tasks", "POST"))`; `create_task(..., owner_user_id=principal.user_id, tenant_id=principal.tenant_id, project_id=_resolve_project(principal, body))`. Quota gate (3.7) runs **before** HF enumeration to fail fast on `hard_block`.
- `list_tasks`/`get_task`/`post_cancel_task`: swap `require_bearer` → `require_perm("/api/v1/tasks*", <act>)`, replace constant filters with `tenant_filtered(...)`.
- `project_id` resolution: `body.project_id` if the principal is a member/owner of it, else the tenant's default project; `project_member`/`project_owner` roles require the resource project ∈ `principal.project_ids` (enforced by the casbin `project_match` scope, fed from the JWT `pids`).

`require_bearer` / `auth/bearer.py` is **removed from user routes**. It is deleted entirely if no caller remains (executor routes use W3a auth, not bearer); the `Settings.bearer_token` field is removed and documented in the operator note.

### 3.6 Tenant resolution on first login (doc 04 §1.3)

Config rule `auth_tenant_rules` — ordered list of `{match: "email_domain"|"group", value: str, tenant_slug: str, role: str}`. `/auth/callback`:

1. Existing `User` (by `oidc_subject`) → use its `tenant_id`/`role` (re-sync `email`).
2. New user → first matching rule decides `tenant` (by slug) + default `role`. Project membership: none initially (tenant-level role).
3. No rule matches → `403 {"code":"TENANT_UNRESOLVED"}` + audit `login` outcome=`denied` (no auto tenant creation — explicit admin onboarding is a later sub-project).

Default seed (idempotent, in the migration / `dlw-seed`): tenant `default` (id=1, the existing data), one rule `{match:"email_domain", value:"*", tenant_slug:"default", role:"tenant_operator"}` **only when `auth_dev_mode=True`** (prod must configure real rules; a wildcard rule in prod is refused at startup with a clear error).

### 3.7 New: `src/dlw/services/quota.py`

Per doc 04 §7. Three metrics: `bytes_month`, `storage_gb`, `concurrent_tasks`.

- `check_quota_for_new_task(session, tenant_id) -> None` — **strong-consistent**: `SELECT ... FOR UPDATE` the `quota_snapshots` row + a live `COUNT(*)` of the tenant's non-terminal tasks (`status NOT IN (completed,failed,cancelled)`). If `concurrent_tasks >= tenant.quota_concurrent` or `bytes_used_month >= tenant.quota_bytes_month` (snapshot value) → raise `QuotaExceeded(metric)`. Action honored: `hard_block` only (→ caller returns `429 {"code":"QUOTA_EXCEEDED","metric":...}`).
- `record_usage(session, *, tenant_id, project_id, user_id, task_id, metric, value)` — append-only insert into `usage_records`. Called from `complete_subtask` (W1's existing fence path) with `metric="bytes_month", value=bytes_downloaded`.
- `aggregate_snapshots(session)` — recompute `quota_snapshots` for all tenants from `usage_records` (month window) + live concurrent count; bump `last_recomputed_at`. Driven by a **leader-gated** minute loop in `main.py` (reuses the W3c `_on_active`/`_on_step_down` pattern — only the active controller aggregates).

### 3.8 Modified: `src/dlw/main.py`

- `app.state.settings = get_settings()` (principal/oidc deps read it from app.state).
- Mount `auth.router` + `quota.router`; bootstrap `app.state.casbin = build_enforcer(load_grants(...))`.
- Add `_on_active` → also start the minute `aggregate_snapshots` task; `_on_step_down` cancels it (mirrors W3c sweep-task wiring; standby does not aggregate).
- Startup guard: if not `auth_dev_mode` and (`system_jwt_secret` is the insecure default OR a wildcard tenant rule is configured OR OIDC issuer unset) → refuse to start with an explicit error (fail-closed on misconfig).

### 3.9 Config additions (`src/dlw/config.py`)

```python
    # Phase 3 SP1 — multi-tenancy
    auth_dev_mode: bool = Field(default=False)
    system_jwt_secret: str = Field(default="dev-system-jwt-change-me")
    system_admin_token: str = Field(default="")            # "" disables the bypass
    oidc_issuer: str = Field(default="")
    oidc_client_id: str = Field(default="")
    oidc_client_secret: str = Field(default="")
    oidc_redirect_url: str = Field(default="http://localhost:8000/api/v1/auth/callback")
    auth_tenant_rules_json: str = Field(default="[]")       # JSON list of rule dicts
```

`bearer_token` is **removed**. Env names follow the `DLW_` prefix (`DLW_AUTH_DEV_MODE`, `DLW_SYSTEM_JWT_SECRET`, …).

---

## 4. Approaches Considered

- **A — Dependency-layer scoping (chosen).** `Principal` dep + casbin `require_perm` dep + mandatory `tenant_filtered` helper; CI Invariant-8 information_schema scan is the backstop. Smallest blast radius, every unit testable in isolation, one obvious place per concern. Risk: a new query could forget `tenant_filtered` — mitigated by the CI scan + the helper being the only sanctioned way to query business tables (documented + reviewed).
- **B — PG Row-Level Security / SQLAlchemy global `with_loader_criteria`.** Strongest guarantee (DB enforces). Rejected: couples correctness to PG RLS session-var plumbing through asyncpg pooling and to ORM internals; much heavier migration; far harder to unit-test; over-engineered for SP1's surface (essentially one business table family).
- **C — Per-endpoint manual filters, no helper.** Minimal code. Rejected: one forgotten `WHERE tenant_id=` is a cross-tenant data breach (Invariant 8 is a hard compliance invariant) — unacceptable fragility.

---

## 5. Schema Changes

One migration `<rev>_p3sp1_tenancy_quota` (down_revision = `6f37b72630ce`, the current W3a head).

**New tables** (doc 04 §7.3 / §1.4):

```sql
CREATE TABLE usage_records (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   BIGINT NOT NULL REFERENCES tenants(id),
    project_id  BIGINT,
    user_id     BIGINT,
    task_id     UUID,
    metric      VARCHAR(64)  NOT NULL,    -- bytes_month | storage_gb | concurrent_tasks
    value       BIGINT       NOT NULL,
    occurred_at TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX idx_usage_tenant_metric_time ON usage_records(tenant_id, metric, occurred_at);

CREATE TABLE quota_snapshots (
    tenant_id          BIGINT PRIMARY KEY REFERENCES tenants(id),
    bytes_used_month   BIGINT      NOT NULL DEFAULT 0,
    storage_gb_used    BIGINT      NOT NULL DEFAULT 0,
    concurrent_tasks   INT         NOT NULL DEFAULT 0,
    last_recomputed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE casbin_rule (
    id    BIGSERIAL PRIMARY KEY,
    ptype VARCHAR(8)  NOT NULL,           -- 'p' | 'g'
    v0    VARCHAR(256), v1 VARCHAR(256), v2 VARCHAR(256),
    v3    VARCHAR(256), v4 VARCHAR(256), v5 VARCHAR(256)
);
CREATE INDEX idx_casbin_ptype ON casbin_rule(ptype);
```

**Data seed (idempotent, in `upgrade()`):** ensure tenant `id=1 slug='default'` exists (it already does in dev data — `INSERT ... ON CONFLICT DO NOTHING`); seed one `quota_snapshots` row per existing tenant. **No backfill of `tenant_id` columns** — business rows are already `tenant_id=1`. **No NOT NULL changes** — `download_tasks.tenant_id` etc. are already `NOT NULL`.

`down_revision`: drop the three tables (reverse order). No business-table column is altered, so downgrade is clean.

Invariant 8: `usage_records`/`quota_snapshots` carry `tenant_id` (snapshot's is the PK). `casbin_rule` is an authz infrastructure table, not a business/data table — same category as `alembic_version`; documented in the migration + the Invariant-8 scan's known-exempt list (verify the scan's allowlist already excludes infra tables; if it whitelists by name, add `casbin_rule`).

---

## 6. Wire Format Changes

### 6.1 New endpoints

| Endpoint | Auth | Success | Error |
|---|---|---|---|
| `GET /api/v1/auth/login` | none | 307 → IdP (or dev callback) | — |
| `GET /api/v1/auth/callback` | none (state cookie) | 200 `{system_jwt, expires_in, tenant_id, role}` | 400 bad/expired state; 401 IdP token invalid; 403 `TENANT_UNRESOLVED`; 503 IdP unreachable |
| `GET /api/v1/auth/me` | system-JWT | 200 `{user_id, tenant_id, role, project_ids}` | 401 |
| `GET /api/v1/quota/current` | system-JWT + `require_perm` | 200 (doc 04 §7.5 shape, minus forecast) | 401 / 403 |

### 6.2 Changed auth on existing endpoints

`POST/GET /api/v1/tasks`, `GET /api/v1/tasks/{id}`, `POST /api/v1/tasks/{id}/cancel`: `Depends(require_bearer)` → `Depends(require_perm("/api/v1/tasks*", <verb>))`. New responses: `401` (no/invalid system-JWT — replaces old bearer 401), `403 {"code":"RBAC_DENIED"}`, `404` (cross-tenant id — existence not leaked), `429 {"code":"QUOTA_EXCEEDED","metric":...}` on `post_task`.

Executor-loop endpoints (heartbeat/poll/report/hf-proxy/register/renew) — **unchanged** (W3a executor auth; not principal-scoped).

### 6.3 Config surface

- Added: `auth_dev_mode`, `system_jwt_secret`, `system_admin_token`, `oidc_issuer`, `oidc_client_id`, `oidc_client_secret`, `oidc_redirect_url`, `auth_tenant_rules_json` (all `DLW_`-prefixed).
- Removed: `bearer_token` (`DLW_BEARER_TOKEN`). Documented in `docs/operator/multi-tenancy.md` as a breaking deployment change with the migration path (set `DLW_SYSTEM_ADMIN_TOKEN` for the prior single-token use case; configure OIDC for real users).

### 6.4 OpenAPI

`api/openapi.yaml` gains the 4 new operations + the new 401/403/404/429 response variants on the 4 task operations, with `RbacDenied`/`QuotaExceeded`/`TenantUnresolved` error components. The OpenAPI-vs-code CI assertion must stay green.

---

## 7. Error Handling Matrix

| Situation | Behaviour |
|---|---|
| No / malformed `Authorization` on a scoped route | 401 `missing/invalid token`, `WWW-Authenticate: Bearer` |
| Expired/forged system-JWT | 401 `invalid token: <PyJWT reason>` |
| Valid JWT, role lacks the permission | 403 `{"code":"RBAC_DENIED"}` + audit `permission_denied` |
| Valid JWT, requests another tenant's task by id | 404 (existence not leaked); audit `permission_denied` outcome=denied |
| `system_admin` service token | allow (bound to tenant 1); audited as actor_user_id=0 service |
| OIDC callback, IdP unreachable / JWKS fetch fails | 503 `UPSTREAM_DEGRADED`; audit `login` outcome=error |
| OIDC callback, no tenant rule matches user | 403 `{"code":"TENANT_UNRESOLVED"}`; audit `login` outcome=denied; user **not** created |
| `state` cookie missing/mismatched (CSRF) | 400 `invalid state` |
| Task create, `concurrent_tasks` ≥ tenant quota | 429 `{"code":"QUOTA_EXCEEDED","metric":"concurrent_tasks"}`; audit `quota.exceeded` |
| Task create, `bytes_used_month` ≥ tenant quota | 429 `{"code":"QUOTA_EXCEEDED","metric":"bytes_month"}` |
| Two concurrent creates racing the last quota slot | `SELECT ... FOR UPDATE` on the snapshot row serializes them; the second sees the live `COUNT(*)`+1 and is rejected (no over-admission) |
| Quota snapshot row missing for a tenant (new tenant) | treated as all-zero usage; aggregator creates it next minute; create check inserts-or-locks defensively |
| Misconfig in prod (`auth_dev_mode=False` + insecure jwt secret / wildcard rule / no OIDC issuer) | controller refuses to start with an explicit message (fail-closed) |
| Standby instance | does not run the quota aggregator (leader-gated); serves reads with stale-by-≤1min snapshots — acceptable (create check recomputes concurrent live) |
| Existing single-tenant deployment upgrading | set `DLW_SYSTEM_ADMIN_TOKEN`; all legacy data is tenant 1; `default` tenant + snapshot seeded by migration; zero data migration |

---

## 8. Testing Strategy

TDD throughout (`superpowers:test-driven-development`): red test → implement → green, per task.

### 8.1 New unit/integration tests (~28–34 cases)

| Area | File | Cases |
|---|---|---|
| Principal / system-JWT | `tests/auth/test_principal.py` | issue+decode round-trip / expired → 401 / forged sig → 401 / missing claim → 401 / service-token → service Principal / wrong-issuer → 401 |
| OIDC | `tests/auth/test_oidc.py` | dev-mode `exchange_code` returns claims / real-mode code exchange against stub JWKS / id_token bad sig → error / login sets state cookie / callback state mismatch → 400 |
| Tenant resolution | `tests/auth/test_tenant_resolution.py` | existing user → own tenant / new user email-domain rule → tenant+role / group rule / no rule → 403 + no user row / prod wildcard rule refused |
| casbin | `tests/authz/test_enforcer.py` | tenant_operator POST tasks allow / tenant_viewer POST → deny / cross-tenant (rtenant≠tenant) deny / project_member project_match allow+deny / system_admin any / keyMatch2 path globbing |
| Tenant scoping | `tests/api/test_tasks_tenant_scope.py` | list returns only own-tenant / get cross-tenant id → 404 / cancel cross-tenant → 404 / create stamps principal tenant+user |
| Quota | `tests/services/test_quota.py` | under-limit passes / concurrent at limit → QuotaExceeded / bytes at limit → QuotaExceeded / record_usage appends / aggregate recomputes snapshot / FOR UPDATE serializes racing creates (no over-admit) |
| Quota API | `tests/api/test_quota.py` | `/quota/current` shape + values / 401 unauth / 403 wrong role |
| Migration | `tests/db/test_p3sp1_migration.py` | upgrade creates 3 tables + seeds default tenant/snapshot / downgrade drops cleanly / idempotent re-seed |
| E2E isolation (`E2E-MT-*`) | `tests/e2e/test_tenant_isolation.py` | tenant A creates task; tenant B JWT cannot list/get/cancel it (404); A's quota exhaustion does not 429 B; service token sees tenant 1 |
| Startup guard | `tests/test_startup_guard.py` | prod insecure secret → refuse / dev mode → ok |

### 8.2 Migration of existing tests

Every test hitting the four task routes currently sends `Authorization: Bearer <bearer_token>`. They migrate to a `principal_headers` conftest fixture that mints a system-JWT (tenant 1, `tenant_operator`) via `issue_system_jwt`, or uses the service token. Affected (per `require_bearer` grep + task-route usage): `tests/api/test_tasks*.py`, `tests/e2e/test_happy_path.py`, `tests/e2e/test_executor_e2e.py`, `tests/e2e/test_executor_auth_e2e.py`, and any task-create helper in `tests/conftest.py` / `make_app_with_state` callers (~8–12 sites). Add `principal_headers` + `service_headers` fixtures to `tests/conftest.py`; seed casbin/enforcer onto the test app (extend `make_app_with_state` to also set `app.state.settings` + `app.state.casbin`).

### 8.3 Not tested in SP1

- Real external OIDC IdP round-trip (CI uses dev mode + a stub-JWKS unit test; live IdP is an SRE onboarding check).
- Audit chain-hash tamper-evidence (Phase 4 — SP1 only asserts audit *rows are written*).
- `throttle`/`overage_billing` quota actions, chargeback, forecast (deferred).
- Prometheus tenant-label cardinality (Phase 4 observability).
- Multi-process active/standby quota-aggregator handoff (covered structurally by reusing the W3c leader-gating, which has its own drill).

### 8.4 CI 12-check expectations

| Check | SP1 impact |
|---|---|
| pytest | +~28–34 new, ~8–12 migrated auth-fixture sites |
| OpenAPI lint + code-vs-yaml | 4 new ops + new 401/403/404/429 variants + error components |
| Invariant + cross-ref lint | Invariant 8 referenced in `tenant_scope.py` + spec/plan cross-ref; `casbin_rule` added to scan's infra-exempt allowlist |
| Markdown lint | spec/plan + `docs/operator/multi-tenancy.md` cross-refs `04 §1/§7`, `INVARIANTS §8` |
| Security scan (gitleaks/pip-audit) | new deps `authlib`,`casbin` pip-audited; no secrets (config-driven) |
| mypy strict | new modules fully annotated (incl. casbin `Enforcer` — add stub or `# type: ignore[no-untyped-call]` narrowly if casbin lacks types) |
| Other 6 | no change |

---

## 9. Acceptance Criteria

- [ ] OIDC login/callback issues a system-JWT; dev mode works without a live IdP; real-mode code path unit-tested against stub JWKS.
- [ ] `Principal` + `require_principal` decode the system-JWT; `system_admin` service token yields a service principal (tenant 1).
- [ ] casbin enforcer + `require_perm` gate task/quota routes; `tenant_match`/`project_match`; cross-tenant denied; `system_admin` allowed; deny is audited.
- [ ] `api/tasks.py` has **no** `_TENANT_ID/_PROJECT_ID/_OWNER_USER_ID` constants and no `require_bearer`; every business query goes through `tenant_filtered`; cross-tenant id → 404.
- [ ] Tenant resolution: existing user → own tenant; new user → rule-mapped tenant/role; unmapped → 403 + no user row; prod wildcard refused at startup.
- [ ] Quota: strong-consistent create check (`FOR UPDATE`, live concurrent count); `hard_block` → 429; `usage_records` append; minute aggregator updates `quota_snapshots`, leader-gated.
- [ ] `GET /api/v1/quota/current` returns the doc 04 §7.5 shape (no forecast).
- [ ] One alembic migration: 3 new tables, idempotent default-tenant/snapshot seed, clean downgrade; Invariant-8 scan green (`casbin_rule` exempted).
- [ ] Audit rows written for `login`, `permission_denied`, `quota.exceeded` via the existing `AuditLog`.
- [ ] Startup guard fails closed on insecure prod config.
- [ ] Full suite green; OpenAPI lint + code-vs-yaml clean; `E2E-MT-*` isolation test passes; `bearer_token` fully removed; operator note written.
- [ ] Two new runtime deps only (`authlib`, `casbin`); no new CI jobs.

---

## 10. Implementation Phasing (preview for plan)

4 milestones, ~12–14 TDD tasks.

- **M1 — Identity core.** Config additions; `Principal` + `require_principal` + system-JWT (HS256) + service-token bypass; `auth/oidc.py` (dev mode + real path) + `api/auth.py` (login/callback/me) + tenant-resolution rules; `test_principal.py`/`test_oidc.py`/`test_tenant_resolution.py`. Migration: 3 tables + seed. Startup guard.
- **M2 — RBAC + tenant scoping.** `authz/` (model.conf, policy.csv, enforcer, `require_perm`); `db/tenant_scope.py`; rewire `api/tasks.py` (drop constants/bearer, principal + scoped queries); `casbin_rule` loading; `test_enforcer.py`/`test_tasks_tenant_scope.py`; migrate existing task-route tests to `principal_headers`/`service_headers` fixtures + `make_app_with_state` extension.
- **M3 — Quota.** `services/quota.py` (check/record/aggregate); wire create-gate into `post_task` + `record_usage` into `complete_subtask`; leader-gated minute aggregator in `main.py`; `api/quota.py`; `test_quota.py`/`test_quota_api.py`.
- **M4 — Isolation e2e + docs + PR.** `tests/e2e/test_tenant_isolation.py` (`E2E-MT-*` centrepiece); OpenAPI updates; `docs/operator/multi-tenancy.md`; full suite + 12-check lint; commit; PR.

Branch: `feat/phase-3-sp1-multi-tenancy` (created off `main` after PR #14 / `db86791`).

---

## 11. References

- Spec source: brainstormed 2026-05-18 (this document).
- Roadmap: `docs/v2.0/08-mvp-roadmap.md` §3 (Phase 3), §3.6 W1 task split, §3.7 risk ("多租户 retrofit … Phase 1 已有 tenant_id 列, 仅逻辑接入").
- Security/tenancy: `docs/v2.0/04-security-and-tenancy.md` §1 (model), §1.3 (first-login flow), §1.4 (casbin RBAC), §2.1 (user OIDC+JWT), §7 (quota), §9.2 (audit events).
- Invariant 8: `docs/v2.0/INVARIANTS.md` row 8 ("业务表必须有 `tenant_id`"; information_schema 扫描).
- Current state anchors: `src/dlw/api/tasks.py:34-36` (`_TENANT_ID/_PROJECT_ID/_OWNER_USER_ID`), `src/dlw/auth/bearer.py` (single shared token, "Phase 3" TODO), `src/dlw/db/models/tenant.py` (Tenant/Project/User already exist w/ quota cols), `src/dlw/db/models/audit.py` (AuditLog schema exists), `src/dlw/config.py:24` (`bearer_token` comment "multi-user OIDC PKCE in Phase 3"), alembic head `6f37b72630ce` (W3a).
- Predecessor specs: `docs/superpowers/specs/2026-05-15-phase-2-w3c-active-standby-design.md` (leader-gating pattern reused for the quota aggregator), `2026-05-14-phase-2-w3a-mtls-jwt-hmac-design.md` (executor auth — orthogonal; not principal-scoped), `2026-05-14-phase-2-w3b-hf-reverse-proxy-design.md` (per-tenant HF token via `task.tenant_id` — made real by SP1).
- Phase 2 final PR (merged): https://github.com/l17728/modelpull/pull/14 (squash `db86791`).
