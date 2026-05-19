# Web UI — Decomposition + UI-SP1 (App Shell + Tasks) Design

**Status:** approved (self-approved under the project's autonomous-execution directive, Rule #1)
**Date:** 2026-05-19
**Sources:** `docs/v2.0/10-frontend-wireframes.md` (9-page design), `docs/v2.0/12-ai-copilot.md` (conversational UI), the **implemented** backend (`src/dlw/api/*` + `api/openapi.yaml`), the existing `frontend/` scaffold. Grounded by a 5-agent exploration (design spec / real API surface / scaffold conventions / AI-Copilot feasibility / industry UX synthesis).

> **Pre-execution review note:** the load-bearing constraint is *backend reality*. Only ~11 v1 endpoints exist. A truly feature-complete UI needs **additive backend read-endpoints** for several pages — those are scoped into later sub-projects, NOT UI-SP1. UI-SP1 is deliberately frontend-only on the existing surface.

---

## 1. Why decompose

The "complete, feature-complete UI" spans 9 pages + an AI-Copilot conversational panel + a download-manager realtime view. That is multiple independent subsystems and (for Copilot) an entire missing backend. Per the brainstorming skill's scope rule and this project's validated sub-project cadence (Phase-3 SP1–SP4 each ran its own spec→plan→2-reviewer→implement→milestone→final-review→PR cycle, all zero-CI-iteration), this is decomposed into **5 UI sub-projects**, built in dependency order. Each gets its own spec/plan/cycle. This document fully specifies the decomposition and **UI-SP1** (the first, highest-value MVP).

## 2. Decomposition (dependency-ordered)

| Sub-project | Scope | Backend dependency |
|---|---|---|
| **UI-SP1** (this doc) | App shell (sidebar + topbar + ⌘K command palette + tenant chip + dark mode + zh/en), auth, `useLiveResource` realtime foundation, Dashboard, Task List, **Task Create** | **None — existing endpoints only** |
| UI-SP2 | Download-manager Task Detail (aggregate ring → per-source segmented bar → virtualized chunk-segmented file table → executor swimlanes → event log) + task/file/chunk actions | Additive read endpoints: subtask-chunks, source-allocation, participating-executors, task-events |
| UI-SP3 | Infrastructure & Governance: Executors (host-grouped, drain/restart), Quota metering, Audit log, Settings | Additive: `GET /executors`, audit query endpoint |
| UI-SP4 | AI-Copilot conversational UI (right slide-over, SSE, tool-call/confirm cards, ⌘K integration) | Full AI backend (`/api/ai/chat` SSE, conversation persistence, LLM bridge, MCP→REST tool bridge) — large, v2.1 |
| UI-SP5 (optional) | Realtime upgrade: swap `useLiveResource` internals to SSE/WS, zero view changes | Backend SSE/WS |

Sequence: **UI-SP1 → (UI-SP2 ∥ UI-SP3) → UI-SP4 → UI-SP5**. MVP value at UI-SP1; flagship differentiation at UI-SP2.

## 3. UI-SP1 goal

Turn the 3-page read-only scaffold into a usable application shell where a user can **create and monitor download tasks from the browser** — closing the "can't create tasks from the UI" gap. Frontend-only; **zero backend/API/migration/lint change** (lowest blast radius, per the project's additive lesson).

## 4. Backend surface UI-SP1 uses (verified implemented)

- `GET /api/v1/auth/login` (302 → OIDC) / `GET /api/v1/auth/callback` (→ system-JWT) / `GET /api/v1/auth/me` (principal). Token-paste login (existing) kept; an "OIDC login" button calls `/auth/login`.
- `POST /api/v1/tasks` (201 `TaskRead`; enumerates HF → subtasks; 409/422/503 errors; 429 quota). Requires `TaskCreate`: `repo_id, revision, storage_id, priority?, source_strategy?, source_blacklist?, trust_non_hf_sha256?, upgrade_from_revision?, path_template?`.
- `GET /api/v1/tasks` (`TaskList{items:TaskRead[], total}`; tenant-scoped; **no server-side filter** → client-side).
- `GET /api/v1/tasks/{id}` (`TaskDetail` = TaskRead + `subtasks[]`).
- `POST /api/v1/tasks/{id}/cancel` (202 → cancelling; 409 if terminal).
- `DELETE /api/v1/tasks/{id}` (204; terminal-only; 409 otherwise).
- `GET /api/v1/quota/current` (tenant quota snapshot).
- `GET /health/{live,ready,active}`.
- Auth header: `Authorization: Bearer <tenant-user system-JWT>`. **Must be a tenant-user JWT (`user_id` matching a real `users` row), not the system-admin service token** (admin → `user_id=0` → `download_tasks.owner_user_id` FK → 500 on create). UI-SP1 surfaces this as a pre-flight check + clear error.

UI-SP1 does **not** depend on any missing endpoint (executors list, source-allocation, events, retry/upgrade, AI chat) — those are later sub-projects.

## 5. Architecture & components (reuse scaffold conventions)

Existing stack kept: Vue 3.5 + Composition/`<script setup>` + TS strict, Pinia, `@tanstack/vue-query`, axios (interceptors: Bearer attach, 401→logout), Element Plus 2.8, vue-router (meta guard), vue-i18n 9, Vitest+@vue/test-utils, Vite (proxy `/api`,`/health` → `DLW_API_PROXY` default `:8001`). Frontend is in CI (`frontend-lint`: lint+typecheck+test:unit; `frontend-build`).

New/changed units (each one responsibility, small files):

- `src/components/shell/AppShell.vue` — replaces `AppLayout`: collapsible sidebar (220↔64, persisted) + topbar. Topbar: ⌘K palette trigger, tenant chip (from JWT `tid`/`role`), dark-mode toggle, locale toggle (zh/en), user menu (logout). Sidebar items + palette built from one **nav registry** (`src/nav/registry.ts`) filtered by `route.meta.roles` vs principal role.
- `src/components/CommandPalette.vue` — ⌘/Ctrl+K modal: fuzzy nav + actions ("Create task", "Go to task by id"). Keyboard-first, a11y (focus trap, aria).
- `src/components/DataBoundary.vue` — wraps any view: slots/props for `loading` (skeleton, not spinner), `empty` (icon + primary CTA), `error` (typed + retry), `forbidden` (permission-empty vs blank). Every page uses it → consistent states.
- `src/composables/useLiveResource.ts` — wraps vue-query: adaptive `refetchInterval` by (state, `document.visibilityState`), terminal-stop predicate, exponential backoff + jitter, optimistic mutate helpers. **All views consume this, never vue-query directly** → SP5 swaps internals to SSE/WS with zero view changes. (Existing `useTaskList`/`useTaskDetail` refactored onto it.)
- `src/stores/ui.ts` — dark mode, sidebar-collapsed, density, locale; persisted to localStorage; `prefers-color-scheme` default.
- `src/stores/session.ts` — principal (user_id, tenant_id, role, project_ids) decoded from JWT (`/auth/me` or local decode); exposes `isServiceToken` (to drive the "use a tenant-user token to create tasks" guard).
- `src/styles/tokens.scss` — design-token layer over Element Plus CSS vars: spacing/radius/typography + **9 task-status semantic colors** (one source of truth; light+dark, ≥4.5:1 contrast; never color-only — icon+label+color).
- `src/pages/Dashboard.vue` — KPI cards (in-progress/completed/failed/total — **client-side aggregated from `GET /api/v1/tasks`** for SP1), recent tasks, quota summary (`GET /quota/current`), 24h created-trend (client-aggregated from `created_at`, ECharts already a peer dep? if not, use a lightweight inline bar — see §8).
- `src/pages/TaskList.vue` (upgrade) — virtualized table (`el-table-v2`), client-side status filter + search, inline aggregate progress (from subtask counts when available else status), row actions (view / cancel / delete-if-terminal) with optimistic update via `useLiveResource`.
- `src/pages/TaskCreate.vue` (new, route `/tasks/new`) — `el-form`: `repo_id` (required, `org/model` pattern), `revision` (required, 40-hex `^[0-9a-f]{40}$` OR `main`→hint it must resolve), `storage_id` (required int>0), `priority` (0-10, default 1), `source_strategy` (select: auto_balance/pin_*/fastest_only/list:), `upgrade_from_revision` (optional 40-hex), `trust_non_hf_sha256` (switch). Submit → `POST /api/v1/tasks` → on 201 route to `/tasks/:id`; map 409/422/503/429 to friendly inline errors; **pre-flight**: if `session.isServiceToken` show a blocking notice ("use a tenant-user token to create tasks").
- `src/pages/Login.vue` (upgrade) — keep token paste; add "Login with OIDC" → `window.location = /api/v1/auth/login`; on `/auth/callback` return, store the system-JWT.
- `src/router/index.ts` — add `/`(Dashboard), `/tasks`, `/tasks/new`; keep `/tasks/:id` (SP2 upgrades it); meta `roles`; guard unchanged (401→login).
- `src/locale/en-US.json` — full parity with `zh-CN.json`; locale switch via `useUiStore`.

## 6. Data flow & error handling

`useLiveResource` is the single realtime seam. Mutations (cancel/delete) optimistic via vue-query `onMutate`→rollback `onError`→`invalidate` `onSettled`. axios 401 interceptor (existing) → logout+redirect. Every page wrapped in `<DataBoundary>` so loading/empty/error/forbidden are uniform. No global state beyond Pinia stores; each store one responsibility.

## 7. Testing

- Vitest + @vue/test-utils (existing conventions): unit-test `useLiveResource` (interval/terminal/backoff/visibility logic as pure functions), `nav/registry` role filtering, `DataBoundary` state rendering, `TaskCreate` validation + error mapping (mock axios), `session` JWT decode + `isServiceToken`.
- One **headed Playwright** smoke (`.run/`-style, dev-only, not CI): login → create task → see it in list → open detail. (Mirrors the project's validated milestone-E2E habit; the controller+UI must be running.)
- Must pass the existing CI `frontend-lint` (eslint `--max-warnings=0` + `vue-tsc` typecheck + `vitest run`) and `frontend-build`. No new CI gate. No new heavy runtime dep (Element Plus virtualized table + ECharts: ECharts is **not** currently a dep — see §8 decision).

## 8. Key decisions / risks (resolved)

- **Charts dependency:** the scaffold has **no ECharts**. Decision (conservative, no heavy dep in SP1): the 24h trend uses a **minimal inline SVG/Element-Plus-based sparkline** (or `el-progress`-style bars), not ECharts. ECharts (for richer SP2 charts) is deferred to UI-SP2 where it's justified. Keeps SP1 dep-free.
- **Virtualized table:** Element Plus 2.8 ships `el-table-v2` (virtualized) — already available, no new dep.
- **Auth:** token-paste stays the primary path (works now, no IdP config); OIDC button is additive and degrades gracefully if no IdP configured.
- **Dashboard aggregates:** client-side from `GET /tasks` for SP1 (no stats endpoint exists; adding one is deferred — keeps SP1 zero-backend). Acceptable at expected scale; documented limitation.
- **Tenant switcher:** SP1 shows the *current* tenant/role from the JWT (read-only chip). Full tenant switching needs multi-tenant token issuance (OIDC) — deferred; not blocking.
- **Additive-only:** UI-SP1 touches only `frontend/**`. No `src/dlw/**`, `api/openapi.yaml`, `alembic`, `tools/` changes → near-zero backend regression risk (mirrors the project's "additive = lowest blast radius" lesson; UI-SP2/SP3 will be the ones adding backend read-endpoints).

## 9. Self-review

- **Placeholders:** none — every component, route, store, endpoint, validation rule, and decision is concrete; ambiguous forks (charts dep, auth path, dashboard aggregation, tenant switcher) explicitly resolved in §8.
- **Internal consistency:** §4 endpoints ⊆ verified-implemented set; §5 components only consume §4; "zero backend change" claim holds (only `frontend/**`); `useLiveResource` is the single realtime seam consistently in §5/§6/§7 and the SP5 upgrade path.
- **Scope:** single focused sub-project (shell + 3 pages + foundation); the larger 9-page/Copilot/download-manager vision is decomposed into UI-SP2..SP5 with explicit backend-dependency notes; YAGNI applied (no ECharts, no real-time backend, no tenant-switch, no Model Search in SP1).
- **Ambiguity:** resolved — exact API contract (incl. the tenant-user-JWT-vs-admin-token 500 trap), exact stack reuse, exact new files, exact deferred items.
- **Risk:** lowest-risk first slice; frontend-only, reuses CI-green scaffold + conventions; the only cross-cutting artifact is replacing `AppLayout`→`AppShell` and refactoring the two existing composables onto `useLiveResource` (covered by existing + new unit tests).
