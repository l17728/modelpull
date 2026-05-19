# Web UI — Operator/User Guide (UI-SP1)

> UI-SP1 ships the **app shell + auth + Dashboard + Task List + Task Create**.
> It is **frontend-only** (no backend/API change) and runs on the existing
> controller. The full 9-page vision is decomposed — see §5.
> Spec: `docs/superpowers/specs/2026-05-19-ui-sp1-shell-tasks-design.md`.
> Local deploy of the controller: `docs/operator/local-deployment.md`.

---

## 1. What UI-SP1 delivers

- **App shell**: collapsible sidebar + topbar, tenant/role chip (from JWT),
  dark-mode toggle, zh/en locale toggle, **command palette (Ctrl/⌘+K)** for
  nav + "create task" + "open task by id".
- **Auth**: paste a tenant-user JWT, or "Sign in with OIDC" button
  (`/api/v1/auth/login`). 401 → auto sign-out.
- **Dashboard** (`/`): KPI cards (in-progress/completed/failed/total,
  client-aggregated from the task list), a 24h created-count sparkline,
  quota summary (`/api/v1/quota/current`), recent tasks.
- **Task List** (`/tasks`): client-side status filter + repo/id search,
  per-row actions (view / cancel non-terminal / delete terminal) with
  optimistic refresh.
- **Task Create** (`/tasks/new`): repo / revision (40-hex) / storage_id /
  priority / source-strategy / upgrade-from / trust-non-hf, validation,
  friendly error mapping (409/422/429/403/5xx), success → task detail.
- Realtime via a single `useLiveResource` seam (adaptive polling: faster
  on detail, slower on lists, ×3 when the tab is hidden, stops at terminal,
  backs off on error). UI-SP5 will swap this to SSE/WS with **zero view
  changes**.

## 2. Run it

```bash
# controller (browser-friendly plain-HTTP instance) — see local-deployment.md
#   → http://localhost:8001
cd frontend && pnpm install && pnpm dev      # → http://localhost:5173
#   Vite proxies /api,/health → DLW_API_PROXY (default http://localhost:8001)
```

Open `http://localhost:5173`, paste a **tenant-user JWT** on the login page.

## 3. The token (important)

Use a **tenant-user JWT** (`user_id` matching a real `users` row), **not
the system-admin service token**: the admin token is `user_id=0` and
`download_tasks.owner_user_id` has an FK to `users` — creating a task with
it fails (HTTP 500). The Task Create page detects a service token and
**disables submit** with a clear warning. Mint a tenant-user JWT:

```bash
uv run python -c "from dlw.auth.principal import issue_system_jwt; \
  print(issue_system_jwt(secret='dev-system-jwt-change-me', user_id=1, \
  tenant_id=1, role='tenant_admin', project_ids=[], ttl_seconds=2592000))"
```

(30-day token for convenience during manual testing; production uses OIDC.)

## 4. Keyboard / UX notes

- **Ctrl/⌘+K** — command palette (navigate, create task, open task by id).
- Dark mode + locale persist (localStorage), default from
  `prefers-color-scheme`.
- Every page has uniform loading / empty / error / forbidden states
  (`DataBoundary`).

## 5. Decomposition — what's deferred (and why)

UI-SP1 is the first of 5 UI sub-projects (the full design needs additive
backend endpoints that don't exist yet):

| Sub-project | Scope | Backend it needs |
|---|---|---|
| **UI-SP1** (this) | shell + auth + dashboard + list + create | none (existing API) |
| UI-SP2 | download-manager Task Detail (aggregate ring → per-source bar → virtualized chunk-segmented file table → executor swimlanes → event log) + task/file/chunk actions | new read endpoints: subtask-chunks, source-allocation, participating-executors, task-events |
| UI-SP3 | Executors (host-grouped, drain/restart), Quota metering, Audit log, Settings | `GET /executors`, audit query endpoint |
| UI-SP4 | AI-Copilot conversational UI (right slide-over, SSE, tool-call/confirm cards, ⌘K) | full AI backend (`/api/ai/chat` SSE, conversation persistence, LLM bridge, MCP→REST tool bridge) |
| UI-SP5 | realtime upgrade: `useLiveResource` → SSE/WS, zero view change | backend SSE/WS |

**Known UI-SP1 scope limits:** Task Detail is still the simple scaffold view
(UI-SP2 makes it the download-manager view); no Executors/Search/Quota-mgmt/
Audit/Settings/Copilot pages; Dashboard aggregates are client-side; tenant
chip is read-only (no tenant switcher); list filtering is client-side
(no server-side filter endpoint yet).

Cross-ref: `docs/getting-started.md`, `docs/operator/cli-sdk.md`.
