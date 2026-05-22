# FU6 — `dlw login` device-authorization flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development / executing-plans. Checkbox steps.

**Goal:** Add an RFC 8628 device-code grant to the controller (3 new `/auth/device*` endpoints + a `device_auth_sessions` table) and `dlw login` / `dlw logout` CLI commands, so a CLI can authenticate without pasting a raw token.

**Spec:** `docs/superpowers/specs/2026-05-22-fu6-device-login-design.md` (read fully — §0 endpoints/table/security envelope, §1 deferrals, §2 tests).

**Locked constraints:**
- Reuse `issue_system_jwt` (no new crypto). The minted token's identity comes ONLY from the authenticated approver; service principals (system-admin token) MUST be rejected at approve.
- `device_code` high-entropy + stored hashed (sha256 hex), single-use (`consumed`), TTL-bounded; `device/token` poll returns RFC errors as HTTP 400 `{error: ...}`, success as 200.
- Device bootstrap is token-free → NOT via `Client` (which requires a token). New `sdk/device.py` builds a bare httpx client; `login`/`logout` dispatch BEFORE `make_client` (like `context`).
- New table → Python `default=`; only `created_at` uses `server_default=func.now()`. No openapi runtime-route mismatch (runtime `/api/v1`, static doc `/api/v2`; keep both internally consistent).
- CI gates: pytest + `lint_invariants` + spectral/swagger-cli (openapi) + (frontend untouched). `ruff --select I001 --fix` touched files.

---

## File Structure
- **Create** `src/dlw/db/models/device_auth.py` (`DeviceAuthSession`); register in `db/models/__init__.py` + `alembic/env.py`.
- **Create** `src/dlw/alembic/versions/f7a8b9c0d1e2_fu6_device_auth_sessions.py`.
- **Modify** `src/dlw/config.py` (3 settings).
- **Modify** `src/dlw/api/auth.py` (3 endpoints + schemas) and `api/openapi.yaml`.
- **Create** `src/dlw/sdk/device.py`; **modify** `src/dlw/sdk/__init__.py`, `src/dlw/sdk/_config.py` (`resolve_server`, `clear_token`).
- **Modify** `src/dlw/cli/main.py` (login/logout subparsers), `src/dlw/cli/handlers.py` (dispatch).
- **Create** `tests/api/test_auth_device.py`, `tests/sdk/test_device.py`, `tests/cli/test_login.py`; **modify** `tests/db/test_alembic.py`.
- **Modify** `docs/operator/cli-sdk.md`.

---

## Milestone M1 — model + migration + settings

### Task 1: model + migration + settings + alembic test
**Files:** new `src/dlw/db/models/device_auth.py`, `src/dlw/db/models/__init__.py`, `src/dlw/alembic/env.py`, new migration, `src/dlw/config.py`, `tests/db/test_alembic.py`.

- [ ] **Step 1 (model):** create `src/dlw/db/models/device_auth.py`:
```python
"""RFC 8628 device-authorization session (FU6)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from dlw.db.base import Base


class DeviceAuthSession(Base):
    __tablename__ = "device_auth_sessions"
    __table_args__ = (
        UniqueConstraint("device_code_hash", name="uq_device_code_hash"),
        UniqueConstraint("user_code", name="uq_device_user_code"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    device_code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_code: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tenant_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    project_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    last_polled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
```
(Check `db/base.py` import path matches the other models; use the SAME `Base` import the sibling `storage_object.py` uses.)
- [ ] **Step 2 (register):** add `DeviceAuthSession` to `src/dlw/db/models/__init__.py` (import + `__all__`) and to the explicit import list in `src/dlw/alembic/env.py` — copy the exact idiom used for `StoragePhysicalKey`.
- [ ] **Step 3 (migration):** create `src/dlw/alembic/versions/f7a8b9c0d1e2_fu6_device_auth_sessions.py` (down_revision `e6f7a8b9c0d1`). `upgrade()` = `op.create_table("device_auth_sessions", ...)` mirroring the model columns exactly (BigInteger id PK, String/Integer/JSONB/DateTime(timezone=True), `created_at` `server_default=sa.func.now()`, the two UniqueConstraints). `downgrade()` = `op.drop_table("device_auth_sessions")`. Use `from sqlalchemy.dialects import postgresql` and `postgresql.JSONB()` for project_ids.
- [ ] **Step 4 (settings):** in `src/dlw/config.py` after `auth_tenant_rules_json` (line ~84) add:
```python
    device_code_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    device_poll_interval_seconds: int = Field(default=5, ge=1, le=60)
    device_verification_uri: str = Field(default="/device")
```
- [ ] **Step 5 (alembic test):** in `tests/db/test_alembic.py` add `"device_auth_sessions"` to `EXPECTED_TABLES`.
- [ ] **Step 6: dev-DB upgrade** — `cd "D:/download_weights" && uv run alembic -c alembic.ini upgrade head` (expect "Running upgrade e6f7a8b9c0d1 -> f7a8b9c0d1e2"); then `uv run alembic -c alembic.ini revision --autogenerate -m _probe` into temp, confirm empty upgrade body for `device_auth_sessions`, DELETE the probe file (or use `alembic check` if present). No drift.
- [ ] **Step 7: verify** — `cd "D:/download_weights" && uv run pytest tests/db -q` → all pass.
- [ ] **Step 8: tidy + commit.**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/db/models/device_auth.py src/dlw/db/models/__init__.py src/dlw/alembic/env.py src/dlw/alembic/versions/f7a8b9c0d1e2_fu6_device_auth_sessions.py src/dlw/config.py tests/db/test_alembic.py
git add src/dlw/db/models/device_auth.py src/dlw/db/models/__init__.py src/dlw/alembic/env.py src/dlw/alembic/versions/f7a8b9c0d1e2_fu6_device_auth_sessions.py src/dlw/config.py tests/db/test_alembic.py && git commit -m "feat(fu6): device_auth_sessions table + device-flow settings"
```

### Task 2: M1 backend gate
- [ ] `cd "D:/download_weights" && uv run pytest -q` all pass (failover flake = Windows-local; isolate-confirm if seen); `uv run python -m dlw.tools.lint_invariants --strict` OK. No commit.

---

## Milestone M2 — backend device endpoints

### Task 3: the three `/auth/device*` endpoints + openapi
**Files:** `src/dlw/api/auth.py`, `api/openapi.yaml`; `tests/api/test_auth_device.py`.

- [ ] **Step 1 (failing tests):** write `tests/api/test_auth_device.py` per spec §2. Use the existing app/client fixture + `principal_headers` (`tests/conftest.py:327`) to forge an approver, and the system-admin-token header for the service-reject test (read conftest for how `system_admin_token` is set in tests — it may need a settings override; if there's no easy service-token fixture, forge it by setting `system_admin_token` on app.state.settings within the test or via the existing override fixture). Tests:
  - `test_device_happy_path`: POST `/api/v1/auth/device` → 200 with device_code/user_code/verification_uri/expires_in/interval. POST `/api/v1/auth/device/token` {device_code} → 400 `authorization_pending`. POST `/api/v1/auth/device/approve` {user_code} with `principal_headers(user_id=7, tenant_id=1, role="tenant_operator")` → 200 `{status:"approved"}`. POST token again → 200 with `access_token`; `jwt.decode(access_token, secret, ...)` (or hit `/auth/me` with it) shows sub=7/tid=1/role. POST token again → 400 `expired_token` (consumed).
  - `test_slow_down`: two back-to-back polls → 2nd `{error:"slow_down"}`.
  - `test_expired_token`: after authorize, set the row `expires_at` to the past (via a db session), poll → `expired_token`.
  - `test_access_denied`: approve with `{action:"deny"}` → poll → `access_denied`.
  - `test_invalid_grant`: poll with a bogus device_code → `invalid_grant`.
  - `test_unknown_user_code_approve`: approve a non-existent user_code → 404 `DEVICE_CODE_INVALID`.
  - `test_service_cannot_approve`: approve with the system-admin token → 403 `SERVICE_CANNOT_APPROVE`.
- [ ] **Step 2: verify FAIL** (404 — routes absent).
- [ ] **Step 3 (pydantic schemas):** in `api/auth.py` add request models `DeviceTokenReq(device_code: str)`, `DeviceApproveReq(user_code: str, action: str = "approve")`. Add a module helper `_hash_code(s: str) -> str` (`hashlib.sha256(s.encode()).hexdigest()`) and `_gen_user_code() -> str` (8 chars from `BCDFGHJKLMNPQRSTVWXZ23456789` via `secrets.choice`, formatted `XXXX-XXXX`) and `_norm_user_code(s) -> str` (uppercase, drop non-alnum, re-insert dash → canonical `XXXX-XXXX`).
- [ ] **Step 4 (POST /device):** add `@router.post("/device")` (no auth dep). Build `device_code = secrets.token_urlsafe(32)`; loop ≤5× generating `user_code` until insert succeeds (catch unique-violation / pre-check pending uniqueness). Insert `DeviceAuthSession(device_code_hash=_hash_code(device_code), user_code=uc, status="pending", expires_at=now+settings.device_code_ttl_seconds, interval_seconds=settings.device_poll_interval_seconds)`. `await write_audit(... action="auth.device.authorize", outcome="success", tenant_id=None, actor_user_id=None)` best-effort. Commit. Return JSON {device_code, user_code, verification_uri=settings.device_verification_uri, verification_uri_complete=f"{...}?user_code={uc}", expires_in=settings.device_code_ttl_seconds, interval=settings.device_poll_interval_seconds}. (Use the `_session` dependency already in the file.)
- [ ] **Step 5 (POST /device/approve):** `@router.post("/device/approve")`, `principal: Principal = Depends(require_principal)`, body `DeviceApproveReq`. If `principal.is_service`: raise HTTPException(403, {"code":"SERVICE_CANNOT_APPROVE"}). `select(DeviceAuthSession).where(user_code==_norm_user_code(body.user_code), status=="pending", expires_at>now).with_for_update()`. None → 404 {"code":"DEVICE_CODE_INVALID"}. If action=="deny": status="denied". Else: status="approved", user_id=principal.user_id, tenant_id=principal.tenant_id, role=principal.role, project_ids=list(principal.project_ids), approved_at=now. Audit `auth.device.approve`/`auth.device.deny` (actor_user_id=principal.user_id, tenant_id=principal.tenant_id). Commit. Return {"status": status}.
- [ ] **Step 6 (POST /device/token):** `@router.post("/device/token")` (no auth), body `DeviceTokenReq`. `select(...).where(device_code_hash==_hash_code(body.device_code)).with_for_update()`. Helper to return `JSONResponse({"error": code}, status_code=400)`. None → invalid_grant. If `last_polled_at` and `(now-last_polled_at).total_seconds() < interval_seconds` → set last_polled_at=now, commit, slow_down. Set last_polled_at=now. If now>expires_at and status!="approved" (and !="consumed") → expired_token. Branch on status: pending→authorization_pending; denied→access_denied; consumed→expired_token; approved→ mint `issue_system_jwt(secret=settings.system_jwt_secret, user_id=row.user_id, tenant_id=row.tenant_id, role=row.role, project_ids=row.project_ids or [])`, set status="consumed", audit `auth.device.token` (actor=row.user_id), commit, return 200 {access_token, token_type:"Bearer", expires_in:3600, tenant_id:row.tenant_id, role:row.role}. (Commit after the last_polled_at update even on the error branches so slow_down state persists.)
- [ ] **Step 7 (openapi):** add the 3 paths to `api/openapi.yaml` (under the existing `/auth/*` block) with request/response schemas; `security: []` on `/auth/device` and `/auth/device/token`, bearer on `/auth/device/approve`. No literal `null` in examples. Then lint: run the SAME spectral/swagger-cli command CI uses (find it in `.github/workflows/` — likely `npx @stoplight/spectral-cli@6.11.1 lint api/openapi.yaml` + `swagger-cli validate`). Must pass.
- [ ] **Step 8: verify PASS** — `cd "D:/download_weights" && uv run pytest tests/api/test_auth_device.py -v` all pass.
- [ ] **Step 9: tidy + commit.**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/api/auth.py tests/api/test_auth_device.py
git add src/dlw/api/auth.py api/openapi.yaml tests/api/test_auth_device.py && git commit -m "feat(fu6): RFC 8628 device-code endpoints (/auth/device, /approve, /token)"
```

### Task 4: M2 backend gate
- [ ] `cd "D:/download_weights" && uv run pytest -q` all pass; `lint_invariants --strict` OK. No commit.

---

## Milestone M3 — SDK bootstrap

### Task 5: `sdk/device.py` + `_config` helpers
**Files:** new `src/dlw/sdk/device.py`, `src/dlw/sdk/__init__.py`, `src/dlw/sdk/_config.py`; `tests/sdk/test_device.py`.

- [ ] **Step 1 (failing tests):** `tests/sdk/test_device.py` per spec §2 using `httpx.MockTransport`:
  - `device_authorize(server, transport=mock)` returns the body dict.
  - `device_token` returns the body on a 400 `{error:"authorization_pending"}` mock (NOT raising) and on a 200 token mock; raises on a 500 mock.
  - `poll_for_token(server, "dc", interval=0, deadline=time.monotonic()+10, sleep=lambda *_: None, token_fn=<fake returning pending then token>)` returns the token; a `token_fn` returning `{"error":"access_denied"}` raises `UsageError`; a `token_fn` always pending with `deadline` already passed raises `Timeout`.
- [ ] **Step 2: verify FAIL.**
- [ ] **Step 3 (device.py):**
```python
"""Token-free RFC 8628 device-flow bootstrap (FU6). Used by `dlw login`
before any token exists — does NOT use Client (which requires a token)."""
from __future__ import annotations

import time

import httpx

from dlw.sdk._http import raise_for_status
from dlw.sdk.errors import Timeout, UsageError


def _client(server, transport, timeout):
    return httpx.Client(base_url=server.rstrip("/"), timeout=timeout,
                        transport=transport)


def device_authorize(server, *, transport=None, timeout=30.0) -> dict:
    with _client(server, transport, timeout) as h:
        r = h.post("/api/v1/auth/device")
        raise_for_status(r)
        return r.json()


def device_token(server, device_code, *, transport=None, timeout=30.0) -> dict:
    with _client(server, transport, timeout) as h:
        r = h.post("/api/v1/auth/device/token", json={"device_code": device_code})
        if r.status_code in (200, 400):
            return r.json()
        raise_for_status(r)
        return r.json()


def poll_for_token(server, device_code, *, interval, deadline,
                   sleep=time.sleep, token_fn=device_token, transport=None) -> dict:
    iv = interval
    while True:
        body = token_fn(server, device_code, transport=transport)
        if body.get("access_token"):
            return body
        err = body.get("error")
        if err in ("access_denied", "expired_token", "invalid_grant"):
            raise UsageError(f"device login failed: {err}")
        if err == "slow_down":
            iv += 5
        if time.monotonic() >= deadline:
            raise Timeout("device login timed out")
        sleep(iv)
```
(Verify `raise_for_status`, `Timeout`, `UsageError` import paths match the existing sdk modules — read `sdk/errors.py` + `sdk/_http.py`.)
- [ ] **Step 4 (_config.py):** add `resolve_server(*, server=None, config_path=None) -> str` (server precedence only: flag > `DLW_SERVER` env > current-context server > `_DEFAULT_SERVER`; no token, no raise) and `clear_token(name, *, config_path=None) -> Path` (load cfg; `cfg.get("auth",{}).get(name,{}).pop("access_token", None)`; `save_config`).
- [ ] **Step 5 (__init__.py):** export `device_authorize`, `device_token`, `poll_for_token` from `dlw.sdk`.
- [ ] **Step 6: verify PASS** — `cd "D:/download_weights" && uv run pytest tests/sdk/test_device.py -v` all pass.
- [ ] **Step 7: tidy + commit.**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/sdk/device.py src/dlw/sdk/__init__.py src/dlw/sdk/_config.py tests/sdk/test_device.py
git add src/dlw/sdk/device.py src/dlw/sdk/__init__.py src/dlw/sdk/_config.py tests/sdk/test_device.py && git commit -m "feat(fu6): token-free device-flow SDK bootstrap + server-only resolver"
```

---

## Milestone M4 — CLI + docs

### Task 6: `dlw login` / `dlw logout` + docs
**Files:** `src/dlw/cli/main.py`, `src/dlw/cli/handlers.py`; `tests/cli/test_login.py`; `docs/operator/cli-sdk.md`.

- [ ] **Step 1 (failing tests):** `tests/cli/test_login.py` (read an existing CLI test e.g. `tests/cli/test_context.py` or `test_cli_*` for the invocation harness — how it calls the parser/`run`/`main` and passes `--config <tmp>`):
  - `test_login_device_writes_token`: monkeypatch `dlw.cli.handlers.device_authorize` → `{"device_code":"dc","user_code":"BCDF-GHJK","verification_uri":"/device","verification_uri_complete":"/device?user_code=BCDF-GHJK","expires_in":600,"interval":0}` and `dlw.cli.handlers.poll_for_token` → `{"access_token":"TOK","token_type":"Bearer","expires_in":3600,"tenant_id":1,"role":"tenant_operator"}`; run `login --no-browser --context test --server http://x --config <tmp>`; assert exit 0 and `load_config(tmp)` has `auth.test.access_token == "TOK"` and `current_context == "test"`.
  - `test_login_token_shortcut`: run `login --token T --context test --server http://x --config <tmp>`; assert token T written, NO device call (don't monkeypatch — if device_authorize were called it'd error on a real connection; or assert via a monkeypatch that raises if called).
  - `test_logout_clears_token`: pre-write a context with a token (`set_context`), run `logout --context test --config <tmp>`, assert token now unset.
- [ ] **Step 2: verify FAIL.**
- [ ] **Step 3 (parser):** in `cli/main.py` add `login` and `logout` subparsers. `login`: `--device-code` (store_true), `--no-browser` (store_true), `--context`, `--server`, `--token`, `--timeout` (float, default = device ttl fallback e.g. 600). `logout`: `--context`. (Both already inherit the global `--config` if it's a parent-parser arg — check how `context` gets `--config`; mirror it.)
- [ ] **Step 4 (handlers):** add `_login_cmd(args)` and `_logout_cmd(args)` and dispatch them at the TOP of `run()` (before `make_client`), exactly like `if args.cmd == "context": return _context_cmd(args)`. Import `device_authorize, poll_for_token` from `dlw.sdk.device` (module-level import so tests can monkeypatch `dlw.cli.handlers.device_authorize`) and `resolve_server, set_context, clear_token, load_config` from `dlw.sdk._config`.
  - `_login_cmd`: `ctx = args.context or load_config(args.config).get("current_context") or "default"`. If `args.token`: `set_context(ctx, server=args.server, token=args.token, config_path=args.config)`; print; return 0. Else: `server = resolve_server(server=args.server, config_path=args.config)`; `r = device_authorize(server)`; print `f"Visit: {r['verification_uri']}\nEnter code: {r['user_code']}\n"` to stdout; unless `args.no_browser`, `try: import webbrowser; webbrowser.open(r["verification_uri_complete"]) except Exception: pass`; `import time; deadline = time.monotonic() + (args.timeout or r["expires_in"])`; `tok = poll_for_token(server, r["device_code"], interval=r["interval"], deadline=deadline)`; `set_context(ctx, server=server, token=tok["access_token"], make_current=True, config_path=args.config)`; print `f"Logged in (context '{ctx}', tenant {tok.get('tenant_id')}, role {tok.get('role')})\n"`; return 0. (UsageError/Timeout propagate to `main()` → exit codes, like context.)
  - `_logout_cmd`: `ctx = args.context or load_config(args.config).get("current_context") or "default"`; `clear_token(ctx, config_path=args.config)`; print `f"Logged out of context '{ctx}'\n"`; return 0.
- [ ] **Step 5 (docs):** `docs/operator/cli-sdk.md`: document `dlw login` (device-code flow: prints a URL + code, you approve in a browser already logged into the org IdP, then the CLI receives and stores the token) and `dlw logout`. State the honest deferrals (browser approval page is a follow-on — until it ships, approval is via the authenticated `POST /auth/device/approve`; no browser-authcode mode; no refresh token, re-login on 1 h expiry).
- [ ] **Step 6: verify PASS** — `cd "D:/download_weights" && uv run pytest tests/cli/test_login.py -v` all pass.
- [ ] **Step 7: tidy + commit.**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/cli/main.py src/dlw/cli/handlers.py tests/cli/test_login.py
git add src/dlw/cli/main.py src/dlw/cli/handlers.py tests/cli/test_login.py docs/operator/cli-sdk.md && git commit -m "feat(fu6): dlw login (device-code) + dlw logout"
```

### Task 7: M4 full gate
- [ ] `cd "D:/download_weights" && uv run pytest -q` all pass; `lint_invariants --strict` OK; spectral/swagger-cli openapi lint pass. No commit.

---

## Self-Review
- **Spec coverage:** §0 table → Task 1 ✓; §0 endpoints+security → Task 3 ✓; §0 SDK bootstrap → Task 5 ✓; §0 CLI → Task 6 ✓; §2 tests → Tasks 3,5,6 ✓; openapi → Task 3 Step 7 ✓; §3 milestones → M1-M4 ✓.
- **Placeholder scan:** Task 3 Step 1 (test forging the service-admin token) and Task 6 Step 1 (CLI invocation harness) reference reading conftest / an existing test for the exact fixture idiom — those are real lookups, not TODOs; the assertions themselves are concrete.
- **Type consistency:** `device_code` (str opaque) vs `device_code_hash` (sha256 hex, the stored/queried form); `user_code` canonical `XXXX-XXXX` via `_norm_user_code` on both write and approve-lookup; `project_ids` JSONB list ↔ `issue_system_jwt(project_ids=list)`; settings names `device_code_ttl_seconds`/`device_poll_interval_seconds`/`device_verification_uri` used identically in endpoints.
- **Open risks for reviewers:** (a) unauthenticated `POST /auth/device/token` ISSUES a bearer token — is the envelope (hashed high-entropy device_code, identity-from-approver-only, service-reject, single-use, TTL, slow_down) airtight? (b) approve `with_for_update` + the token-mint `with_for_update` race — can a double-poll mint two tokens? (status flips to `consumed` under row lock in one txn → second poll sees consumed.) (c) `_norm_user_code` must canonicalize identically on generate vs approve-lookup or approval silently 404s. (d) new unauth routes — confirm NO casbin row needed and they're genuinely pre-auth (no `require_perm`). (e) does `write_audit` accept `tenant_id=None`/`actor_user_id=None` for the pre-identity authorize event (it does for the OIDC `denied` path — see auth.py callback). (f) openapi spectral on the new paths (null-example trap).
