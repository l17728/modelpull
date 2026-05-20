# UI-SP3 — Infrastructure & Governance (Design)

> Sub-project 3 of the Web UI decomposition (UI-SP1 = PR #19; UI-SP2 = PR #20 `de9573a`).
> Status: design self-approved per project Rule #1 (autonomous; recommended/conservative at every fork).
> Branch: `feat/ui-sp3-infra-governance`.

## 1. Context & Scope

SP3 covers the **governance / infrastructure** pages from the UI decomposition: Executors, Audit log,
Quota, Settings. Following the SP2 cycle and the project's "additive = lowest blast radius" lesson,
SP3 ships **only what real backend tables can support** and **defers everything that needs
write paths, new tables, or designed-only data** (drain/restart, executor metrics history, HF-token
rotation, license-policy CRUD, ML forecast, chargeback PDF, real-time tail, etc.).

**In scope (additive, zero Alembic migration):**

1. **Backend — 2 read-only endpoints over the existing schema:**
   - `GET /api/v1/audit/log` — implements the **already-declared** `searchAuditLog`
     (`api/openapi.yaml:1266-1295`) → response `{ items: [AuditEntry] }`, params
     `actor_user_id` / `action` (prefix match) / `from` / `to` / `cursor`. Tenant-scoped
     (`AuditLog.tenant_id == principal.tenant_id`). Cursor = opaque base64 of
     `occurred_at.isoformat() + "|" + id`, same shape as SP2's `events`. NEW Python file
     `src/dlw/api/audit.py` (`require_perm` — not allowed in mTLS files).
   - `GET /api/v1/executors` — **NEW path** added to `openapi.yaml` (`listExecutors`,
     `tags: [executors]`); **must live in a new file `src/dlw/api/executors_read.py`**
     because `tools/lint_invariants.py:check_no_bearer_on_executor_routes` forbids
     non-mTLS deps on `src/dlw/api/executors.py`. Returns
     `{ items: [ExecutorRead] }` filtered by tenant visibility:
     `Executor.tenant_id IS NULL OR Executor.tenant_id == principal.tenant_id`
     (own-tenant + shared-infra view; matches the security invariant
     "tenant_admin cannot list executors outside their tenant"). System admins
     (`principal.role == "system_admin"` or `principal.is_service`) bypass the
     filter and see all executors.

2. **Frontend — 4 new pages, all reusing SP1/SP2 conventions:**
   - **`/executors`** — host-grouped list (group by `Executor.host_id`), per-executor row
     (status badge mapped onto the 9 status tokens, health score, last-heartbeat-ago,
     disk free/total, NIC speed if present, capabilities tags). Status filter
     (all / healthy / degraded / suspect / faulty). Polled via `useLiveResource`
     (5 s) — terminal predicate never fires (executors are long-lived), polling stops
     only when the tab is hidden via the SP2-added `document.visibilityState` slow-down.
   - **`/audit`** — paginated, filterable audit log. Filters: action prefix (free-text),
     actor (user_id), date range (from/to). Cursor-paginated via `Load older` (same
     pattern as SP2 event log). Polled at 10 s (terminal: never; pause when hidden).
   - **`/quota`** — richer client-side display of the **existing** `GET /api/v1/quota/current`
     (no new endpoint). Three usage cards (bytes / storage / concurrent), each with a
     progress bar, formatted values, and a threshold chip (`≥85%` warn, `≥100%` over).
   - **`/settings`** — **frontend-only**. Reuses `stores/session.ts` (principal: user_id /
     tenant_id / role / project_ids / `isServiceToken`) + `stores/ui.ts` (theme / locale)
     + `/health/active` widget showing the controller's leader status. No backend changes.

3. **Nav + router**: 4 new routes (`executors`, `audit`, `quota`, `settings`), 4 new
   nav-registry items (icons from Element Plus, optional `roles` chip). Auth guard
   unchanged.

**Out of scope (deferred & documented):**

- Drain/restart actions on executors (no endpoints; only mTLS register/renew/heartbeat/poll exist).
- Executor metrics history, heartbeat history, NIC utilization chart (no historical tables).
- `GET /quota/usage` (declared in the contract but the backing `usage_records`/`quota_snapshots`
  tables **do not exist on disk** — implementing faithfully would require a migration, which
  the inherited "no Alembic" rule forbids).
- HF token rotation, source-driver registration, license-policy CRUD, maintenance mode
  (all write-side, would need new tables and admin endpoints).
- Real-time audit tail (would need SSE/WS — deferred to UI-SP5).
- ML quota forecast, chargeback PDF, per-region cost breakdown (designed-only).
- ECharts; canvas matrix.

## 2. Inherited Locked Decisions (binding on SP3)

All from UI-SP1 §8 and UI-SP2 §2:

- `useLiveResource` is the **only** realtime seam (with the SP2-added optional `enabled`).
- `DataBoundary` wraps every view (loading skeleton / empty / error / forbidden).
- Element Plus 2.x — **no new runtime dep**. `el-table` with `max-height` + cursor pagination
  for large lists (SP2 contingency stays; `el-table-v2` remains untested in this codebase).
- 9 status semantic colours from `styles/tokens.scss`; never colour-only (icon + label + colour);
  ≥ 4.5:1 contrast.
- `stores/session.ts` for principal; tenant chip stays read-only.
- Additive backend only: new files + new routes + `openapi.yaml` + Pydantic DTOs; **no schema
  changes, no Alembic migration, no edits to existing routes**.
- Pass existing CI only (`pytest`, `OpenAPI` lint, `Invariant` lint, `lint_no_direct_status_write`,
  `frontend-lint`, `frontend-build`, `markdown lint` — `docs/operator/**` is **not** globbed by
  markdownlint).
- i18n: `en-US.json` + `zh-CN.json` at exact key parity (verified by `localeParity.spec.ts`).
- `noUncheckedIndexedAccess` is on — guard every `arr[i]`.
- Frontend tests: `vi.hoisted` for plain holders + async `vi.mock(async () => { const { ref } = await import('vue'); ... })` for any mock that must return a real Vue ref (the SP2 BLOCKER-fix pattern).
- Bash cwd persists across calls → all `git` commands use explicit `cd /d/download_weights && git …`;
  frontend tooling uses `cd /d/download_weights/frontend && pnpm …`.

## 3. Backend Design

### 3.1 Contract (`api/openapi.yaml`)

- Implement `searchAuditLog` to **exactly match** the on-disk schema (response is
  `{ items: [AuditEntry] }` — no `next_cursor` in the declared response; the cursor is
  the page boundary handle, but the contract doesn't expose it. We return `next_cursor`
  inside `items[]`? No — we keep the response shape exactly as declared and signal
  "more pages exist" by returning `len(items) == limit`. The client passes the last
  row's encoded cursor back via the `cursor` query parameter. This matches the contract
  byte-faithfully.).
- **Add** one new path (in the existing `# ========== Executors ==========` section
  style, with `$ref` to existing component params/responses):

  ```yaml
  /executors:
    get:
      tags: [executors]
      summary: List executors visible to the principal
      operationId: listExecutors
      parameters:
        - in: query
          name: status
          schema: {type: string, enum: [joining, healthy, degraded, suspect, faulty]}
      responses:
        '200':
          description: Executors
          content:
            application/json:
              schema: {$ref: '#/components/schemas/ExecutorListResponse'}
  ```

- **Add** one new schema `ExecutorListResponse` containing `items: [ExecutorRead]` (the existing
  `ExecutorRead` schema in `api/openapi.yaml` is **declared but currently unused** —
  `spectral` reports it as an unused component; SP3 uses it, eliminating the warning).
  If `ExecutorRead`'s declared shape does not contain all the fields the UI needs
  (e.g. `host_id`, `capabilities`, `disk_free_gb`, `disk_total_gb`, `nic_speed_gbps`),
  the spec is to **extend** `ExecutorRead` additively with the missing fields rather
  than introduce a parallel schema (keeps the contract canonical). Verified at plan time.

### 3.2 Endpoints (router placement)

| Path | File | Auth | Tenant gate | Notes |
|---|---|---|---|---|
| `GET /api/v1/audit/log` | **new** `src/dlw/api/audit.py` (`prefix="/api/v1/audit"`) | `Depends(require_perm("/api/v1/audit*", "GET"))` | `AuditLog.tenant_id == principal.tenant_id` (single-column filter; no need for the cancel-pattern gate because there's no parent resource) | Returns `{ items: [AuditEntry] }`. Params: `actor_user_id`, `action` (prefix `LIKE`), `from_: datetime | None = Query(default=None, alias="from")`, `to: datetime | None = Query(...)`, `cursor: str | None = Query(...)`. Default page = 50, hard cap 200. Order: `occurred_at DESC, id DESC`. Cursor encode/decode reuses SP2's helpers (extract into `src/dlw/services/_pagination.py` to share with SP2 if cheap; otherwise duplicate the two helpers — they're 6 lines). |
| `GET /api/v1/executors` | **new** `src/dlw/api/executors_read.py` (`prefix="/api/v1/executors"`) | `Depends(require_perm("/api/v1/executors*", "GET"))` | `Executor.tenant_id IS NULL OR Executor.tenant_id == principal.tenant_id`; bypassed for `system_admin` / `is_service` | Returns `{ items: [ExecutorRead] }`. Optional `status` query (validated by enum). Order: `host_id ASC, id ASC` (stable host grouping). No pagination (executor count is bounded — small per environment). |

Both routes follow the existing `require_perm` + `_session` dep pattern. Neither uses
`require_bearer` — that's mTLS-only and forbidden by `lint_no_bearer_on_executor_routes`
on `executors.py`/`subtasks.py`, but the rule does not scan our new file names.

### 3.3 Service layer

New `src/dlw/services/audit_query.py` (single function `search_audit_log(session, tenant_id, *, actor_user_id, action_prefix, from_, to_, cursor, limit) -> tuple[list[AuditLog], str | None]`)
and `src/dlw/services/executors_read.py` (single function
`list_executors_for_principal(session, principal, status_filter) -> list[Executor]`).

Keeping each ≤ 60 lines and self-contained.

### 3.4 DTOs (`src/dlw/schemas/audit.py`, `src/dlw/schemas/executor_read.py`)

New small files. `AuditEntryRead` and `ExecutorRead` Pydantic DTOs with
`model_config = ConfigDict(from_attributes=True)` matching the OpenAPI shapes
exactly (`actor_ip` and `trace_id` fall back to `""` when ORM column is `None`,
because the contract declares them non-nullable; `payload` falls back to `{}`;
`prev_hash`/`tenant_id`/`actor_user_id`/`resource_id` declared nullable already).

### 3.5 Tests (`tests/api/`)

One file per endpoint, mirroring SP2 fixture-block pattern:

- `test_audit_search.py` — happy (insert 3 audit rows; query default page; assert
  shape + newest-first order) + cross-tenant 404 isn't applicable (no parent resource)
  → **cross-tenant filter** test (insert tenant 2 row, query as tenant 1, must not
  appear) + unauth 401 + filter by `action` prefix + filter by `actor_user_id` +
  cursor pagination (next page non-overlapping).
- `test_executors_list.py` — happy (insert 2 executors with `tenant_id=1` and
  `tenant_id=NULL`; auth as tenant 1 → both appear) + tenant isolation (tenant 2's
  executor must not appear for tenant 1) + unauth 401 + `status` query filter.

Each test file: ~120 lines, copy-paste fixture block from SP2's `test_task_detail_*.py`
(module bootstrap drops/creates all + seeds Tenant/Project/User/StorageBackend id=1).

## 4. Frontend Design

### 4.1 Pages & components (`frontend/src/pages/`, `frontend/src/components/infra/`)

New SFCs:

- `pages/Executors.vue` — top: status filter (el-select). Body: `DataBoundary` → grouped list,
  each group = host card with header (`host_id`, executor count, NIC speed sum) and a list of
  `ExecutorRow` children. `useExecutors` composable (live).
- `pages/Audit.vue` — top: filter bar (`action` text input, `actor_user_id` number, `from` /
  `to` date pickers, "重置筛选" button). Body: `DataBoundary` → `AuditRow` list + "Load older"
  button. `useAuditLog` composable (live).
- `pages/QuotaPage.vue` — three `QuotaCard` components fed by `useQuota` (already exists). No
  backend changes.
- `pages/Settings.vue` — three sections: **Profile** (principal fields), **Preferences**
  (theme/locale via `stores/ui.ts`), **System** (controller leader state widget via a
  new `useSystemHealth` composable hitting `GET /health/active`).

New components in `frontend/src/components/infra/`:

- `ExecutorRow.vue` — props `executor: ExecutorRead`. Renders id, status badge, health
  score, last-heartbeat ago (formatted), disk usage bar (inline SVG), NIC speed chip.
- `AuditRow.vue` — props `entry: AuditEntry`. Renders `occurred_at` (formatted), actor (id or
  "system"), `outcome` chip (success / denied / error colour), `action` code, `resource_type`,
  `resource_id` (truncated to 16 chars with full-id tooltip), `trace_id` (clickable to
  `details.trace_id` if any — no external link; just a copyable code).
- `QuotaCard.vue` — props `label`, `used`, `quota`, `format` (bytes|gb|count). Stacked bar with
  threshold chip.
- `HealthPill.vue` — props `state: string`. Pill mapped onto status tokens
  (`active` → success, `recovering` → warning, `standby` → info, else danger).

### 4.2 Composables

- `useExecutors(statusRef: Ref<string|null>)` — `useLiveResource<ExecutorListResponse>(['executors', statusRef], …, { baseIntervalMs: 5_000, enabled: ref(true) })`.
- `useAuditLog(filters: ReactiveFilters)` — wraps useLiveResource at 10 s polling; `Load older`
  is a one-shot `client.get(.../audit/log?cursor=…)` matching SP2's event-log pattern.
- `useSystemHealth()` — `useLiveResource(['health-active'], () => client.get('/health/active'), { baseIntervalMs: 10_000 })`.

### 4.3 Utilities

`frontend/src/utils/format.ts` — extend with `formatDateTime(iso: string | null): string`
(locale-aware) and `formatTimeAgo(iso: string | null): string` (returns "5 min ago" /
"1 h ago" / "2 d ago" / "—"). Add `formatPercent(num, total)` helper if useful (otherwise
inline).

### 4.4 Router + Nav

`frontend/src/router/index.ts` — append 4 routes (`executors`, `audit`, `quota`, `settings`),
all with `props: false` (no params). No new `meta` keys.

`frontend/src/nav/registry.ts` — append 4 `NAV_ITEMS` entries (`Monitor`, `Document`, `Histogram`,
`Setting` icons from Element Plus). Each has a `labelKey` under `nav.*`. No `roles` filter for
this SP (server enforces; tenant_admin sees own data; tenant_operator users hitting
admin-only endpoints get 403 surfaced via `DataBoundary`'s `forbidden` slot — already supported).

### 4.5 i18n

Three new top-level blocks added to **both** `en-US.json` and `zh-CN.json`
(at exact parity, verified by `localeParity.spec.ts`):

- `nav.executors`, `nav.audit`, `nav.quota`, `nav.settings`
- `executors.*` (heading, statusAll, healthy/degraded/suspect/faulty/joining, columns,
  empty, lastHeartbeat, diskUsage, capabilities, etc.)
- `audit.*` (heading, filterAction, filterActor, filterFrom, filterTo, reset, columns,
  outcome.success/denied/error, empty, loadOlder)
- `quotaPage.*` (heading, byteUsage, storageUsage, concurrentUsage, threshold.warn,
  threshold.over)
- `settings.*` (heading, profile, preferences, system, theme.light/dark/auto, locale,
  controllerState, principal.user/tenant/role/projects/serviceToken)

### 4.6 Tests

Mirror SP2 patterns exactly:

- Pure-fn specs: `formatDateTime.spec.ts`, `formatTimeAgo.spec.ts`.
- Component specs: `ExecutorRow.spec.ts`, `AuditRow.spec.ts`, `QuotaCard.spec.ts`,
  `HealthPill.spec.ts`.
- Composable wiring specs: `sp3Composables.spec.ts` (mock `@/api/client` + `@/composables/useLiveResource`
  via `vi.hoisted` + `vi.mock`; assert key, path, polling interval).
- Page specs (full mount, vi.hoisted plain holders + async `vi.mock(async () => { const { ref } = await import('vue'); ... })` for `useExecutors`/`useAuditLog`/`useQuota`/`useSystemHealth`/`useTaskMutations`-equivalents-not-needed; assert `DataBoundary` empty state when no data, filtered rendering when data, filters trigger refetch).
- Locale parity spec already exists (catches additions automatically).

## 5. Data Flow & Error Handling

Identical to SP2:

- `useLiveResource` is the only seam (`enabled` for visibility-paused polling).
- Per-pane `DataBoundary` (`forbidden` covers 403; `error` covers 5xx; `empty` covers 200-no-rows).
- axios 401 interceptor (existing) → logout + `/login?reason=invalid_token`.
- No new Pinia store; server state is query state.

## 6. Milestones (preview for writing-plans)

- **M1 — Backend**: `audit.py` + service + DTO + 6 tests; `executors_read.py` + service + DTO + 4 tests;
  openapi.yaml additions (1 new path + extend `ExecutorRead` if needed); pass full `pytest`
  + `spectral` + `swagger-cli` + `lint_invariants` + `lint_no_direct_status_write`.
- **M2 — Frontend foundation**: api/types additions; 3 composables; format helpers (`formatDateTime`,
  `formatTimeAgo`); pure-fn specs.
- **M3 — Components**: `ExecutorRow`, `AuditRow`, `QuotaCard`, `HealthPill` + their specs.
- **M4 — Pages + i18n + smoke + docs**: 4 pages, router/nav additions, both locales,
  page specs, headed-Playwright smoke against a fresh `:8011`-style controller (using the
  same recipe SP2 codified), update `docs/operator/web-ui.md`.

## 7. Risks & contingencies

- **`AuditEntry` non-nullable contract fields with nullable ORM columns** (`actor_ip`,
  `trace_id`): Pydantic DTO coerces `None → ""` to match the contract byte-faithfully.
  Documented; OpenAPI lint passes.
- **`ExecutorRead` schema mismatch**: if the on-disk `ExecutorRead` schema lacks fields
  the UI wants, extend it additively in `openapi.yaml`. If extending is non-trivial, the
  contingency is to keep the contract minimal (`id`, `status`, `health_score`,
  `last_heartbeat_at`, `host_id`, `tenant_id`) and have the UI render only those —
  acceptable scope reduction documented in the plan.
- **Tenant filter for executors**: the chosen rule (`tenant_id IS NULL OR == principal.tenant_id`,
  bypass for `system_admin` / `is_service`) honours the documented invariant; a tenant_admin
  test asserts cross-tenant executors don't appear.
- **`searchAuditLog` response lacks `next_cursor`**: client detects "more available" by
  `items.length === limit`; documented. If the UX needs a cursor handle exposed, that's a
  contract addition in a follow-up — out of scope for SP3.
- **Audit table indexes**: there's no explicit `(tenant_id, occurred_at)` index. At dev/test
  scale the planner handles it; at production scale this becomes a documented follow-up
  (creating an index is an Alembic migration, which the locked decision forbids in SP3).

## 8. Self-Review

- **Placeholder scan**: none — every endpoint, DTO, service, page, component, and test has a
  concrete shape and contract reference.
- **Consistency**: single `useLiveResource` seam (§2/§4.2), DataBoundary everywhere
  (§4.1/§5), additive backend (§1/§3), contract-faithful response shapes (§3.1/§3.2). No
  contradictions.
- **Scope**: one plan — 2 backend endpoints + 4 frontend pages — matches SP2's shape.
  Designed-only items explicitly deferred (§1 out-of-scope list).
- **Ambiguity**: endpoint paths, DTO field nullability, tenant filter clause, cursor encode
  shape, polling intervals all pinned. Router/nav additions enumerated. i18n blocks listed.
- **Risks** all accompanied by an explicit mitigation or documented follow-up (§7).
