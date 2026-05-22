# FU6 — `dlw login` device-authorization flow (RFC 8628)

## Problem

The controller's OIDC is a **browser authorization-code redirect** flow
(`/auth/login` → IdP → `/auth/callback` mints an HS256 system-JWT). A CLI has no
browser-redirect listener, so today `dlw` can only use a token pasted via
`--token` / `DLW_TOKEN` / config. SP4-CLI explicitly deferred `dlw login`:

> a CLI needs a **device-code flow (RFC 8628)** endpoint (`POST /auth/device`)
> that **does not exist**. Out of scope (would require new controller support).

FU6 adds that controller support + the `dlw login` / `dlw logout` commands.

## Key facts (from code survey)

- Token minting already exists: `issue_system_jwt(secret, user_id, tenant_id,
  role, project_ids)` (`auth/principal.py:30`). `/auth/callback` already calls it.
  **FU6 reuses it — no new crypto.**
- `Principal` = `{user_id, tenant_id, role, project_ids, is_service}`
  (`auth/principal.py:21`). `require_principal` validates the HS256 system-JWT.
- Config write-back exists: `set_context(name, server=, token=)`
  (`sdk/_config.py:71`) already writes `auth.<ctx>.access_token`.
- `Client.__init__` calls `resolve()` which **requires a token** — so the device
  bootstrap (which runs *before* a token exists) cannot use `Client`. FU6 adds a
  token-free SDK path + a server-only resolver.
- Auth routes live in `api/auth.py` (prefix `/api/v1/auth`), exempt from the
  no-bearer-on-executor-routes lint, and `/auth/*` need NO casbin policy row
  (they are pre-auth). New device routes go here.

## §0 Design

### RFC 8628 grant — three new endpoints in `api/auth.py`

**(1) `POST /api/v1/auth/device`** — start (no auth, `security: []`).
Generates a high-entropy `device_code` (`secrets.token_urlsafe(32)`) and a
short, human-typeable `user_code` (8 chars from an unambiguous alphabet
`BCDFGHJKLMNPQRSTVWXZ23456789` — no vowels/0/1/O/I/L, formatted `XXXX-XXXX`).
Persists a `device_auth_sessions` row (device_code stored **hashed**, never
plaintext) and returns:
```json
{ "device_code": "<opaque>", "user_code": "BCDF-GHJK",
  "verification_uri": "<base>/device",
  "verification_uri_complete": "<base>/device?user_code=BCDF-GHJK",
  "expires_in": 600, "interval": 5 }
```
`user_code` is retried (≤5×) on the unlikely collision with another *pending*
row (unique constraint). Audit `auth.device.authorize` (best-effort; no actor yet).

**(2) `POST /api/v1/auth/device/approve`** — `Depends(require_principal)`.
Body `{ "user_code": "BCDF-GHJK", "action": "approve"|"deny" }` (default
`approve`). This is what an **already-authenticated browser user** calls (after
the normal OIDC login) to authorize the waiting device. Behavior:
- Normalize the submitted code (uppercase, strip spaces/dashes) and look up a
  `pending`, unexpired row by `user_code` `WITH FOR UPDATE`.
- Not found / expired / not-pending → `404 {code: "DEVICE_CODE_INVALID"}`.
- **Reject service principals** (`principal.is_service` — the system-admin token,
  `user_id=0`): `403 {code: "SERVICE_CANNOT_APPROVE"}`. A device must never be
  granted a service identity; only a real user may approve. **(security control)**
- `approve` → set `status=approved`, copy the approver's
  `user_id / tenant_id / role / project_ids` onto the row, `approved_at=now()`.
  `deny` → `status=denied`.
- Audit `auth.device.approve` / `auth.device.deny` (actor = the approver).
- Return `{ "status": "approved"|"denied" }`.

**(3) `POST /api/v1/auth/device/token`** — poll (no auth, `security: []`).
Body `{ "device_code": "<opaque>" }`. Looks up by `sha256(device_code)`
`WITH FOR UPDATE`. RFC 8628 error semantics (all returned as **HTTP 400** with an
`{error: ...}` body, except success which is 200):
- unknown device_code → `400 {error: "invalid_grant"}`.
- poll faster than `interval` (now − `last_polled_at` < interval) →
  `400 {error: "slow_down"}`. Every poll updates `last_polled_at`.
- now > `expires_at` and not yet approved → `400 {error: "expired_token"}`.
- `status == pending` → `400 {error: "authorization_pending"}`.
- `status == denied` → `400 {error: "access_denied"}`.
- `status == consumed` → `400 {error: "expired_token"}` (single-use; already redeemed).
- `status == approved` → mint `issue_system_jwt(secret, user_id, tenant_id, role,
  project_ids)` from the **approver's stored identity**, set `status=consumed`,
  audit `auth.device.token`, return
  `200 { "access_token": "<jwt>", "token_type": "Bearer", "expires_in": 3600,
  "tenant_id": <int>, "role": "<str>" }`.

**Security envelope (this endpoint issues bearer tokens without auth):**
- `device_code` is high-entropy (256-bit) and stored **hashed** (sha256 hex) — the
  poll endpoint is not brute-forceable and the plaintext never persists/logs.
- The minted JWT's identity comes **solely** from the authenticated approver
  (control 2 above); the device cannot influence which identity it receives.
- Single-use (`consumed`) + short TTL (`expires_in`, default 600 s) bound the
  redemption window; an unredeemed approval expires.
- `slow_down` rate-limits polling per the RFC.
- `user_code` is low-entropy but only usable by an *authenticated* approver
  (approve is bearer-gated), is unique among pending rows, and is short-lived.

### New table — `device_auth_sessions` (new alembic revision off `e6f7a8b9c0d1`)

New table → Python `default=` is fine (no `compare_server_default` drift concern;
that trap is only for ALTERs on existing tables — cf. SP4a/4b precedent). Mirror
`created_at` with `server_default=func.now()` like sibling models.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | BigInteger PK | no | autoincrement |
| `device_code_hash` | String(64) | no | — (UNIQUE) |
| `user_code` | String(16) | no | — (UNIQUE) |
| `status` | String(16) | no | `default="pending"` |
| `user_id` | BigInteger | yes | — (set on approve) |
| `tenant_id` | BigInteger | yes | — |
| `role` | String(32) | yes | — |
| `project_ids` | JSON | yes | — |
| `interval_seconds` | Integer | no | `default=5` |
| `created_at` | DateTime(tz) | no | `server_default=func.now()` |
| `expires_at` | DateTime(tz) | no | — (set in code: now + ttl) |
| `last_polled_at` | DateTime(tz) | yes | — |
| `approved_at` | DateTime(tz) | yes | — |

Register in `db/models/__init__.py` + `alembic/env.py`; add
`"device_auth_sessions"` to `tests/db/test_alembic.py::EXPECTED_TABLES`.

### Settings (`config.py`, after `auth_tenant_rules_json`)

```python
device_code_ttl_seconds: int = Field(default=600, ge=60, le=3600)
device_poll_interval_seconds: int = Field(default=5, ge=1, le=60)
device_verification_uri: str = Field(default="/device")
```
`verification_uri_complete = f"{device_verification_uri}?user_code={user_code}"`.

### SDK — token-free bootstrap

`Client` requires a token, so device bootstrap lives in a new module
`src/dlw/sdk/device.py` building a **bare** `httpx.Client(base_url=server)` (no
Authorization header):
- `device_authorize(server, *, transport=None, timeout=30.0) -> dict` — POST
  `/api/v1/auth/device`; `raise_for_status`; return body.
- `device_token(server, device_code, *, transport=None, timeout=30.0) -> dict` —
  POST `/api/v1/auth/device/token`; on **200 or 400** return `r.json()` (the RFC
  error bodies are expected, not exceptions); any other status → `raise_for_status`.
- `poll_for_token(server, device_code, *, interval, deadline, sleep=time.sleep,
  token_fn=device_token, transport=None) -> dict` — loops: `authorization_pending`
  → sleep(interval) & retry; `slow_down` → interval += 5 & retry; `access_denied`
  / `expired_token` → raise `UsageError`; `access_token` present → return body;
  past `deadline` → raise `Timeout`. `sleep`/`token_fn` injectable so tests never
  really wait.

Export the three from `dlw.sdk.__init__`.

`_config.py` additions:
- `resolve_server(*, server, config_path=None) -> str` — server precedence only
  (flag > `DLW_SERVER` > current-context server > default), **no token required**.
- `clear_token(name, *, config_path=None) -> Path` — remove
  `auth.<name>.access_token`, save.

### CLI (`cli/main.py` parser + `cli/handlers.py`)

Both new commands are dispatched **before** `make_client` (like `context`), since
no token exists yet.

- **`dlw login [--device-code] [--no-browser] [--context NAME] [--server URL]
  [--token TOKEN] [--timeout SECONDS]`**
  - `--token` shortcut: write it straight to the context
    (`set_context(ctx, server=server, token=token)`), print confirmation, exit 0.
    (Matches the v2.0 spec's `--token`.)
  - Otherwise device flow (device-code is the only implemented mode; the
    browser-authcode mode that opens a browser + runs a local redirect listener is
    a documented deferral): `server = resolve_server(...)`;
    `r = device_authorize(server)`; print `Visit: <verification_uri>` +
    `Enter code: <user_code>`; unless `--no-browser`, best-effort
    `webbrowser.open(verification_uri_complete)` (swallow failures);
    `tok = poll_for_token(server, r["device_code"], interval=r["interval"],
    deadline=now + r["expires_in"])`; on success
    `set_context(ctx, server=server, token=tok["access_token"], make_current=True)`
    and print `Logged in (context '<ctx>', tenant <id>, role <role>)`.
  - `ctx` defaults to the current context name, else `"default"`.
- **`dlw logout [--context NAME]`** — `clear_token(ctx)`; print
  `Logged out of context '<ctx>'`. Local-only (the system-JWT is stateless; there
  is no server-side session to revoke — documented).

### OpenAPI (`api/openapi.yaml`, static `/api/v2` doc)

Add the three paths (`/auth/device`, `/auth/device/approve`, `/auth/device/token`)
with request/response schemas. `security: []` on `device` and `device/token`;
bearer on `device/approve`. Honor the spectral constraints already pinned in CI
(no literal `null` example values; examples match patterns; `6.11.1`).

## §1 Honest scope / deferrals

- **The browser approval page is deferred** (a named follow-on). FU6 ships the
  RFC 8628 backend grant + CLI. `verification_uri` points at a UI route
  (`/device`) that does not yet render a page; until it does, approval is via the
  authenticated `POST /auth/device/approve` API (a logged-in browser session, a
  dev-mode JWT, or curl). The contract is complete and fully testable at the
  API+CLI layer; the page is thin UI glue over the approve endpoint.
- **Browser authorization-code mode for `dlw login`** (no `--device-code`: open a
  browser + run an ephemeral local redirect listener) is deferred; device-code is
  the implemented mode. `--device-code` is accepted (and is the default behavior)
  for forward-compat with the v2.0 flag.
- **No refresh token.** The minted system-JWT has a 1 h TTL; on expiry the user
  re-runs `dlw login`. The v2.0 config's `refresh_token`/`expires_at` keys remain
  unimplemented (consistent with FU1).

## §2 Tests (TDD)

`tests/api/test_auth_device.py` (backend grant, real DB + forged principal):
- happy path: `POST /auth/device` → codes; `POST /auth/device/token` →
  `authorization_pending`; `POST /auth/device/approve` (forged principal via
  `principal_headers`) → `approved`; poll → `access_token`; **decode the JWT and
  assert it carries the approver's `sub`/`tid`/`role`**; poll again → `consumed`
  → `expired_token`.
- `slow_down`: two immediate polls → 2nd is `slow_down`.
- `expired_token`: set the row's `expires_at` to the past → poll → `expired_token`.
- `access_denied`: approve with `action="deny"` → poll → `access_denied`.
- unknown device_code → `invalid_grant`.
- approve with unknown user_code → `404 DEVICE_CODE_INVALID`.
- **service principal approve** (system-admin token) → `403 SERVICE_CANNOT_APPROVE`.

`tests/sdk/test_device.py` (httpx MockTransport):
- `device_authorize` returns the parsed body.
- `device_token` returns the body on a **400** error response (does NOT raise) and
  on 200; raises on 500.
- `poll_for_token` with an injected `token_fn` returning `authorization_pending`
  then `access_token` (and `sleep=lambda *_: None`) returns the token; a
  `token_fn` returning `access_denied` raises `UsageError`; past-deadline raises
  `Timeout`.

`tests/cli/test_login.py` (handler, no real client/sleep):
- `login` device path with monkeypatched `device_authorize`/`poll_for_token` →
  asserts `set_context` wrote `auth.<ctx>.access_token` and made it current
  (use a temp `--config` path).
- `login --token T` → writes T to the context without any device call.
- `logout` → clears the token (token `unset` afterward).

`tests/db/test_alembic.py`: `EXPECTED_TABLES` gains `device_auth_sessions`; the
exact-set assertion passes after `upgrade head`.

## §3 Milestones

- **M1** — model + migration + Settings + alembic/EXPECTED_TABLES. Gate:
  `pytest tests/db -q`, dev-DB `:5433` upgrade clean, no drift.
- **M2** — backend endpoints in `api/auth.py` + openapi.yaml + backend tests.
  Gate: full `pytest -q`, `lint_invariants --strict`, spectral lint of
  `api/openapi.yaml` (the `swagger-cli`/`spectral` invocation CI uses).
- **M3** — SDK `device.py` + `_config.py` helpers + tests. Gate: `pytest tests/sdk -q`.
- **M4** — CLI `login`/`logout` + tests + docs (`docs/operator/cli-sdk.md`). Gate:
  full `pytest -q`, `lint_invariants --strict`.

## §4 Migration checklist (applied)

- down_revision = `e6f7a8b9c0d1` (current single head).
- New model registered in `db/models/__init__.py` + `alembic/env.py`.
- New table → Python `default=`; only `created_at` uses `server_default=func.now()`
  (no ALTER, so no `compare_server_default` drift risk).
- `EXPECTED_TABLES` += `"device_auth_sessions"`.
- Dev-DB `:5433` `alembic -c alembic.ini upgrade head` + autogenerate drift check.
