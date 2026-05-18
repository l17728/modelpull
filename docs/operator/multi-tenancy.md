# Multi-Tenancy Operator Guide (SP1)

This guide covers the Phase 3 SP1 multi-tenancy changes: OIDC-based authentication,
tenant routing rules, and the system-admin service token. It supersedes the old
single-bearer-token (`DLW_BEARER_TOKEN`) model.

Cross-references: `docs/v2.0/04-security-and-tenancy.md` §1 and §7,
`docs/v2.0/INVARIANTS.md` row 8.

---

## Environment Variables

### OIDC Configuration

| Variable | Required | Description |
|---|---|---|
| `DLW_OIDC_ISSUER` | yes (non-dev) | OIDC issuer URL, e.g. `https://keycloak.example.com/realms/dlw` |
| `DLW_OIDC_CLIENT_ID` | yes | OAuth 2.0 client ID registered with the IdP |
| `DLW_OIDC_CLIENT_SECRET` | yes | OAuth 2.0 client secret |
| `DLW_OIDC_REDIRECT_URL` | yes | Callback URL, e.g. `https://api.dlw.example.com/auth/callback` |
| `DLW_SYSTEM_JWT_SECRET` | yes (non-dev) | HMAC-HS256 signing secret for system-JWTs. **Must be a strong random secret in production.** |
| `DLW_AUTH_TENANT_RULES_JSON` | yes (non-dev) | JSON array of tenant routing rules (see below) |

### Tenant Routing Rules

`DLW_AUTH_TENANT_RULES_JSON` maps authenticated IdP identities to tenants and roles.
Rules are evaluated in order; the first match wins. A user with no matching rule is
refused login with HTTP 403 `TENANT_UNRESOLVED`.

```json
[
  {
    "match": "email_domain",
    "value": "acme.com",
    "tenant_slug": "acme",
    "role": "tenant_operator"
  },
  {
    "match": "email_domain",
    "value": "partner.example.com",
    "tenant_slug": "partner",
    "role": "tenant_viewer"
  }
]
```

Supported `match` values: `email_domain`, `email_exact`, `oidc_group`.

### Service Token (Non-Interactive)

`DLW_SYSTEM_ADMIN_TOKEN` — a static bearer token that authenticates as a
`system_admin` bound to the default tenant (id=1). Intended for CLI tooling,
SDKs, and test suites that cannot perform interactive OIDC flows.

- Leave empty (or unset) to disable service-token authentication entirely.
- Treat this value as a secret; rotate it via your secrets manager.

### Dev Mode

`DLW_AUTH_DEV_MODE=true` — skips real OIDC and issues a dev system-JWT
automatically. **CI and local development only. Never enable in production.**

---

## Breaking Change: DLW_BEARER_TOKEN Removed

`DLW_BEARER_TOKEN` is removed in SP1. All existing deployments must migrate:

- **Single-token automation** (CLI, SDK, tests): set `DLW_SYSTEM_ADMIN_TOKEN`
  instead. The token value can be the same string — only the env var name changes.
- **Human users**: configure OIDC (`DLW_OIDC_*` vars above) so users log in via
  `GET /auth/login`.

> **Note:** `docker-compose.dev.yml` and files under `docs/demo/` still reference
> `DLW_BEARER_TOKEN`. When updating those, switch to `DLW_SYSTEM_ADMIN_TOKEN`.

---

## Single-Tenant Default (Legacy Data)

Legacy data from before SP1 is automatically assigned to tenant id=1
(`slug=default`). The SP1 migration seeds this tenant at startup — no manual
data migration is needed.

---

## Fail-Closed Startup Guard

In non-dev mode the controller refuses to start if any of the following are true:

- `DLW_SYSTEM_JWT_SECRET` is the insecure default value (`changeme` / empty).
- `DLW_OIDC_ISSUER` is unset.
- `DLW_AUTH_TENANT_RULES_JSON` contains a wildcard rule (`"value": "*"` with
  `"match": "email_domain"`), which would allow any user from any domain to log in.

This guard enforces INVARIANT 8 (fail-closed auth). The controller logs a fatal
error and exits with code 1 if any condition is triggered.

---

## Authentication Flow Summary

```
User browser              Controller              IdP
    |                         |                    |
    |-- GET /auth/login ------>|                    |
    |<-- 307 Location ---------|-- redirect ------->|
    |                         |                    |
    |-- GET /auth/callback --->|                    |
    |   ?code=...&state=...   |-- token exchange -->|
    |                         |<-- id_token --------|
    |                         |-- tenant lookup     |
    |<-- 200 {system_jwt} ----|                    |
    |                         |                    |
    |-- GET /auth/me          |                    |
    |   Authorization: Bearer <system_jwt>          |
    |<-- 200 {user_id, tenant_id, role, ...}        |
```

All subsequent API calls use `Authorization: Bearer <system_jwt>`.
The system-JWT is an HS256 token signed with `DLW_SYSTEM_JWT_SECRET`.
