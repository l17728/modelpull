# FU6 follow-on — Browser device-approval page

## Problem

FU6 (PR #43) shipped the RFC 8628 device-flow backend (`POST /auth/device`, `/auth/device/approve`, `/auth/device/token`) and the `dlw login` CLI. The approval step is currently HTTP-only — the CLI prints the verification URL `<server>/device?user_code=XXXX-XXXX` and the user has to invoke `/auth/device/approve` via curl or some other tool with a valid Bearer JWT in their browser/session.

This sub-project adds the `/device` browser page so a human operator can land at the URL printed by the CLI, sign in if needed, see the device-code and their own identity, and click **Approve** or **Deny**. This closes the human side of the device-flow loop.

## §0 Design

### Routing

A new authenticated route `/device` in `frontend/src/router/index.ts`:

```ts
{
  path: '/device',
  name: 'device',
  component: () => import('@/pages/Device.vue'),
}
```

No `meta.public` → the existing router guard auto-redirects unauthenticated users to `/login`.

### Login redirect support (minimal additive change)

Today the router guard returns `{ path: '/login' }` discarding the original target, and `Login.vue` always pushes `'/'` on success. To support landing back at `/device?user_code=...` after login, two small changes:

- **Router guard** (`router/index.ts`): `return { path: '/login', query: { redirect: to.fullPath } }` (replaces the current `return { path: '/login' }`).
- **`Login.vue` on success**: `router.replace((route.query.redirect as string) || '/')` (replaces `router.push('/')`).

Validate the redirect destination is same-origin path (starts with `/` and not `//`) before honoring it — defense against open-redirect via crafted `?redirect=https://attacker.example/`.

### `Device.vue` page

Reads `user_code` from URL query, pre-fills the input. UI:

- `el-card` with header "Confirm device".
- Display the current principal (`session.principal.userId` / `role` / `tenantId`) so the operator knows *who* will be linked to the device.
- `el-input` (user_code, pre-filled from `?user_code=`; editable so operators can paste manually).
- If `session.isServiceToken === true`: `el-alert` warning + disable the **Approve** button (mirrors `TaskCreate.vue` UX). The backend rejects with 403 `SERVICE_CANNOT_APPROVE`, but the frontend prevents the call.
- Two buttons: **Approve** (primary) and **Deny**.
- On click: `axios.post('/api/v1/auth/device/approve', { user_code: code, action: 'approve' | 'deny' })`.
- Success: show success result (`el-result` or `el-alert` success) with "Return to the CLI" copy. Hide the form.
- Error mapping:
  - 404 `DEVICE_CODE_INVALID` → "Invalid or expired code. Restart `dlw login`."
  - 403 `SERVICE_CANNOT_APPROVE` → "Service tokens cannot approve a device. Sign in as a real user."
  - other → generic "Approval failed" toast.

### What's NOT in scope (deferred)

- **No OIDC kickoff on the device page** — relies on existing `Login.vue` OIDC button. Future: add an OIDC button on the device page itself for one-click flows.
- **No QR-code rendering** — the CLI prints the URL; user pastes it. QR support could be added later (requires a QR-rendering lib — keep zero-new-dep).
- **No real-time "approved" notification to the CLI** — the CLI polls `/auth/device/token` per existing FU6 design.
- **No tenant/project selection at approval time** — approval copies the current principal's tenant/role/project_ids verbatim. Multi-tenant operators must sign in with the right context first.

## §1 Threat model

- **Open-redirect prevention**: `Login.vue` validates `redirect` is a same-origin path (`startsWith("/") && !startsWith("//")`) before honoring it.
- **Service-token rejection**: enforced both client-side (disabled button + alert) and server-side (FU6's 403 in `device_approve`).
- **CSRF**: not a concern — the API uses bearer JWT in `Authorization` header, not cookies; no implicit cross-origin auth.
- **User_code in URL**: short (8 chars + hyphen), single-use, expires in 10 min, server `with_for_update()` prevents double-approve. URL appearing in browser history is acceptable (the code is useless after consumption).

## §2 Tests

`frontend/tests/unit/Device.spec.ts`:
- Pre-fills `user_code` from `?user_code=ABCD-1234`.
- Approve button calls `/api/v1/auth/device/approve` with `{user_code, action: 'approve'}` and shows success.
- Deny button calls with `action: 'deny'` and shows success.
- 404 DEVICE_CODE_INVALID → error message rendered.
- 403 SERVICE_CANNOT_APPROVE → service-token error message.
- Service token detected client-side → Approve button disabled, warning alert shown.

`frontend/tests/unit/auth.spec.ts` (extend):
- Router guard adds `?redirect=` for protected routes.
- `Login.vue` honors a same-origin `?redirect=` after successful login.
- `Login.vue` rejects an external `?redirect=https://...` and falls back to `/`.

## §3 Files

- **Create** `frontend/src/pages/Device.vue`
- **Modify** `frontend/src/router/index.ts` (add route + guard redirect)
- **Modify** `frontend/src/pages/Login.vue` (honor `?redirect=`)
- **Modify** `frontend/src/locale/en-US.json` + `frontend/src/locale/zh-CN.json` (add `device.*` keys, both at exact parity)
- **Create** `frontend/tests/unit/Device.spec.ts`
- **Extend** `frontend/tests/unit/auth.spec.ts`

Zero backend / migration / openapi / executor change. Zero new runtime dep.

## §4 Notes

- Lint gate: existing `frontend-lint` (eslint `--max-warnings=0` + `vue-tsc`) + `frontend-build` + `vitest run`. No backend gate changes.
- Both `en-US.json` and `zh-CN.json` must include identical key sets (project convention).
