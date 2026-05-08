# Phase 1 Week 3 — UI Scaffold Design

> Spec for the alpha-demo frontend — Login + TaskList + TaskDetail. Companion to
> `docs/v2.0/10-frontend-wireframes.md` (which targets the full v2.0 product, of
> which this spec implements a Phase-1-shaped subset).

- **Status**: design approved (2026-05-08)
- **Phase**: Phase 1, Week 3 (UI half — Executor half shipped in PR #3)
- **Backend constraint**: Phase 1 §1.3 explicitly excludes OIDC, multi-tenant,
  fence-token, mTLS. Auth is shared Bearer (`DLW_BEARER_TOKEN` env).
- **Author**: l17728
- **Reviewer**: TBD (multi-agent reviewer pass before plan execution)

---

## 1. Goal & Non-Goals

### 1.1 Goal

Ship a 3-page Vue 3 SPA so an alpha-demo user can:

1. Paste a Bearer token to log in.
2. See the list of download tasks.
3. Open a task and watch its (subtask-level) progress refresh in near-real-time.

The scaffold must be a **clean foundation** for Phase 2/3 expansion (more pages,
OIDC, WebSocket, multi-tenant) — i.e., the auth boundary, API client, store
shape, and project structure should not need rework when those features arrive.

### 1.2 Non-goals (deferred)

| Item | Deferred to | Reason |
|------|-------------|--------|
| WebSocket `/ws/v1` (snapshot+delta+seq) | Phase 2 | Controller has 0 WS code; polling is adequate for alpha demo. |
| TaskCreate page | Phase 2 | `curl` covers demo; form + speed-probe is its own scope. |
| ExecutorList page | Phase 2 | Controller has no `GET /executors`; single-executor demo doesn't need it. |
| Dashboard / Quota / Audit / Copilot | Phase 3–4 | Multi-tenant / audit / AI are not yet bottomed out. |
| OIDC + multi-user | Phase 3 | Phase 1 §1.3 explicit OUT. |
| `en-US` locale | Phase 2 | `zh-CN.json` first; structure supports adding it later. |
| ECharts | Phase 2 | No charts in 3-page MVP. |
| Dark mode | Phase 2 | Not a demo blocker. |
| `openapi-typescript` codegen | Phase 2 | 3 hand-written interfaces are faster than codegen for now. |
| Production nginx + `Dockerfile.frontend` | Phase 2 | Alpha demo runs `pnpm dev` on host. |
| Playwright CI required | Phase 1 end or Phase 2 | Smoke locally is fine; CI can skip until stable. |

---

## 2. Tech Stack

| Concern | Choice | Notes |
|---------|--------|-------|
| Framework | Vue 3 + Composition API + `<script setup>` | Matches `docs/v2.0/10-frontend-wireframes.md` §0. |
| Build | Vite 5 | HMR, simple config. |
| Language | TypeScript 5.x, `strict: true` | CI fails on `tsc --noEmit` errors. |
| Router | vue-router 4 | 3 routes. |
| State | Pinia 2 | Auth store + tasks selection store. |
| Server-state | `@tanstack/vue-query` v5 | Polling lives here, not in Pinia. |
| HTTP | axios | Request + response interceptors hold auth + 401 logic. |
| UI lib | Element Plus 2.x | ElForm / ElTable / ElTag / ElContainer / ElMessage. |
| i18n | vue-i18n 9 | `zh-CN.json` only in Phase 1; `en-US` Phase 2. |
| Tests (unit) | Vitest | CI required. |
| Tests (E2E) | Playwright | Smoke only; CI optional. |
| Lint | ESLint + Prettier + `eslint-plugin-vue` | CI required. |
| Pkg manager | pnpm 9 | Fast, deterministic, lockfile in repo. |

**Excluded vs v2.0 doc** (intentional): ECharts, OIDC client (`oidc-client-ts`),
WebSocket composable, dark mode, `en-US` locale, `openapi-typescript-codegen`,
Tailwind / Vuetify / Quasar.

---

## 3. Project Structure

```
modelpull/
├── frontend/                          # NEW — Vue 3 SPA
│   ├── package.json
│   ├── pnpm-lock.yaml
│   ├── vite.config.ts                 # proxy /api/* + /health/* → :8000
│   ├── tsconfig.json                  # strict
│   ├── tsconfig.node.json             # for vite.config.ts
│   ├── .eslintrc.cjs
│   ├── .prettierrc
│   ├── .gitignore                     # node_modules, dist, .env.local
│   ├── index.html
│   ├── public/
│   │   └── favicon.svg
│   ├── src/
│   │   ├── main.ts                    # createApp + Pinia + vue-query + i18n + Element Plus + router
│   │   ├── App.vue                    # <AppLayout><RouterView/></AppLayout>
│   │   ├── router/
│   │   │   └── index.ts               # 3 routes + auth guard
│   │   ├── api/
│   │   │   ├── client.ts              # axios instance + interceptors
│   │   │   └── types.ts               # TaskRead / TaskDetail / SubTaskRead (hand-written)
│   │   ├── stores/
│   │   │   └── auth.ts                # accessToken (localStorage-synced)
│   │   ├── composables/
│   │   │   ├── useTaskList.ts         # vue-query: GET /api/v1/tasks
│   │   │   └── useTaskDetail.ts       # vue-query: GET /api/v1/tasks/:id with terminal-aware polling
│   │   ├── pages/
│   │   │   ├── Login.vue
│   │   │   ├── TaskList.vue
│   │   │   └── TaskDetail.vue
│   │   ├── components/
│   │   │   ├── AppLayout.vue          # ElContainer + header (logo, logout) + main
│   │   │   ├── StatusBadge.vue        # status string → ElTag type+label
│   │   │   └── EmptyState.vue         # icon + message + optional action slot
│   │   ├── locale/
│   │   │   └── zh-CN.json             # all UI strings
│   │   └── styles/
│   │       └── main.scss              # CSS reset + Element Plus token tweaks
│   └── tests/
│       ├── unit/                      # Vitest *.spec.ts
│       └── e2e/                       # Playwright smoke
├── src/dlw/                           # existing — only schemas/task.py + api/tasks.py change
└── ...
```

**Why `frontend/` at repo root** (not `src/dlw/web/`): clean ecosystem boundary
(npm vs uv), separate CI jobs, matches `docs/v2.0/10-frontend-wireframes.md` §1.

---

## 4. Routing & Auth Boundary

### 4.1 Routes

| Path | Component | Guard |
|------|-----------|-------|
| `/login` | `Login.vue` | none — public |
| `/` | `TaskList.vue` | requires token |
| `/tasks/:id` | `TaskDetail.vue` | requires token |
| `*` | redirect to `/` | (catch-all) |

`?reason=invalid_token` query param on `/login` triggers an `ElMessage.error`.

### 4.2 Auth store (`stores/auth.ts`)

```typescript
export const useAuthStore = defineStore('auth', () => {
  const accessToken = ref<string | null>(localStorage.getItem('dlw_token'))

  function login(token: string) {
    localStorage.setItem('dlw_token', token)
    accessToken.value = token
  }

  function logout() {
    localStorage.removeItem('dlw_token')
    accessToken.value = null
  }

  const isAuthenticated = computed(() => accessToken.value !== null)

  return { accessToken, isAuthenticated, login, logout }
})
```

**Phase 3 OIDC migration plan**: replace `login(token)` with an OIDC callback
that sets the same `accessToken` ref. Consumers (axios interceptor, router
guard) don't change.

### 4.3 axios interceptor logic (`api/client.ts`)

```typescript
// request
config.headers.Authorization = `Bearer ${authStore.accessToken}`

// response error
if (status === 401) {
  authStore.logout()
  router.push({ path: '/login', query: { reason: 'invalid_token' } })
  return Promise.reject(error)              // tell vue-query to NOT retry
}
if (status >= 500) return Promise.reject(error)  // vue-query auto retry × 3
// 4xx other than 401 falls through → caller decides
```

### 4.4 Why no token-validation ping at login

The Login form does **not** pre-validate the token with a probe request. It
stores the token, pushes `/`, and lets the natural `TaskList` request fail
through the 401 interceptor. Saves a roundtrip and a duplicated error path; the
UX cost (one extra screen-flicker on bad token) is acceptable for alpha demo.

---

## 5. Data Flow

### 5.1 App boot

```
main.ts
  └→ createPinia()
  └→ authStore = useAuthStore()             // hydrates from localStorage in setup()
  └→ router.beforeEach((to) => {
       if (to.path === '/login') return true
       if (!authStore.isAuthenticated) return '/login'
       return true
     })
  └→ app.mount('#app')
```

### 5.2 TaskList page

```
useTaskList():
  useQuery({
    queryKey: ['tasks'],
    queryFn: () => client.get<TaskListResponse>('/api/v1/tasks'),
    refetchInterval: 5_000,
    staleTime: 5_000,
    refetchOnWindowFocus: true,
  })
```

`5s` is a balance: real-time enough for demo, low enough load to not stress
single-instance controller. Tab-focus refetch makes stale lists snap fresh
when developer alt-tabs back.

### 5.3 TaskDetail page

```typescript
const TERMINAL = new Set(['succeeded', 'failed', 'cancelled'])

useQuery({
  queryKey: ['task', taskId],
  queryFn: () => client.get<TaskDetail>(`/api/v1/tasks/${taskId}`),
  refetchInterval: (query) =>
    query.state.data && TERMINAL.has(query.state.data.status) ? false : 1_000,
  refetchOnWindowFocus: true,
})
```

Polling stops automatically when the task hits a terminal state — no idle
controller load after the demo finishes.

### 5.4 Logout

`AppLayout` header button → `authStore.logout()` → `router.push('/login')`
(no `?reason` query — distinguishes deliberate logout from token expiry).

---

## 6. Backend Changes (Minimal)

### 6.1 Schema split — TaskRead vs TaskDetail

`src/dlw/schemas/task.py`:

```python
class TaskRead(BaseModel):
    """List item — slim, no subtasks."""
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    repo_id: str
    revision: str
    status: str
    priority: int
    created_at: datetime
    completed_at: datetime | None
    error_message: str | None


class TaskDetail(TaskRead):
    """GET /api/v1/tasks/{id} — adds subtasks list."""
    subtasks: list[SubTaskRead] = []
```

### 6.2 Endpoint update

`src/dlw/api/tasks.py`:

```python
from sqlalchemy.orm import selectinload
from dlw.schemas.task import TaskCreate, TaskList, TaskRead, TaskDetail


@router.get("/{task_id}", dependencies=[Depends(require_bearer)])
async def get_task(task_id: uuid.UUID, session: AsyncSession = Depends(_session)) -> TaskDetail:
    row = (await session.execute(
        select(DownloadTask)
          .where(DownloadTask.id == task_id, DownloadTask.tenant_id == _TENANT_ID)
          .options(selectinload(DownloadTask.subtasks))
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    return TaskDetail.model_validate(row)
```

`list_tasks` is unchanged — keeps `TaskRead` (no subtasks) to avoid N+1 across
many tasks.

### 6.3 Tests added

`tests/api/test_tasks.py`:

- `test_get_task_includes_subtasks` — assert response has `subtasks`, length =
  number of mock subtasks created by `task_service.create_task` (currently 2).
- `test_list_tasks_does_not_include_subtasks` — assert `'subtasks' not in items[0]`
  to lock the slim list contract.

No DB migration (response shape change only).

---

## 7. Error Handling

| Trigger | Handling |
|---------|----------|
| 401 (any request) | axios interceptor: clear token + push `/login?reason=invalid_token`; vue-query does not retry. |
| 404 (`GET /tasks/{id}`) | `TaskDetail` shows `<EmptyState>` "任务不存在或已删除" + "返回列表" link. |
| 5xx | vue-query default retry × 3 (exponential backoff); on terminal failure show `ElAlert` at page top "服务暂不可用，X 秒后自动重试" + manual refresh button. |
| Network error / offline | Same path as 5xx; vue-query auto-refresh on `online` event. |
| 422 (Login token empty) | Element Plus form-level validation, no request sent. |
| 422 (other — should not occur) | `ElMessage.error(detail)` fallback. |

**Explicitly not handled in Phase 1**:

- CSRF — not applicable to Bearer auth (cookie auth would need it).
- Optimistic update UX (cancel button etc.) — Phase 2.
- Offline cache — alpha demo doesn't need it.
- Network retry progress UI — vue-query handles silently.

**i18n discipline**: error strings live under the `errors.*` namespace in
`locale/zh-CN.json`. axios interceptor calls `i18n.global.t('errors.invalid_token')`
to compose the `ElMessage`.

---

## 8. Testing Strategy

### 8.1 Frontend unit (Vitest) — CI required

| Test | File | Assertion |
|------|------|-----------|
| StatusBadge color map | `tests/unit/StatusBadge.spec.ts` | 5 statuses → 5 ElTag types one-to-one. |
| auth login/logout | `tests/unit/auth.spec.ts` | login writes localStorage + `accessToken`; logout clears both. |
| auth hydrate | `tests/unit/auth.spec.ts` | localStorage primed → boot sets `accessToken`. |
| axios req interceptor | `tests/unit/client.spec.ts` | mock `accessToken='abc'` → request carries `Authorization: Bearer abc`. |
| axios resp 401 | `tests/unit/client.spec.ts` | injected 401 → calls logout + pushes `/login`, rejects promise. |
| useTaskDetail terminal stop | `tests/unit/useTaskDetail.spec.ts` | `data.status='succeeded'` → `refetchInterval` returns `false`. |

### 8.2 Frontend E2E (Playwright) — CI optional

```typescript
// tests/e2e/smoke.spec.ts
test('happy path', async ({ page }) => {
  await page.goto('/login')
  await page.getByLabel('Bearer Token').fill(process.env.TEST_TOKEN!)
  await page.getByRole('button', { name: '登录' }).click()
  await expect(page).toHaveURL('/')
  await expect(page.getByRole('table')).toBeVisible()
  await page.locator('tbody tr').first().click()
  await expect(page.getByText(/repo_id/)).toBeVisible()
})

test('401 kicks back to login', async ({ page }) => {
  await page.goto('/login')
  await page.getByLabel('Bearer Token').fill('wrong-token')
  await page.getByRole('button', { name: '登录' }).click()
  await expect(page).toHaveURL(/\/login\?reason=invalid_token/)
  await expect(page.getByText(/token 无效/)).toBeVisible()
})
```

E2E requires controller + PG running with a known token. Phase 1 acceptance is
"smoke passes locally before merging"; CI integration deferred until stable.

### 8.3 Backend tests (added)

- `tests/api/test_tasks.py::test_get_task_includes_subtasks`
- `tests/api/test_tasks.py::test_list_tasks_does_not_include_subtasks`

---

## 9. CI Changes

`.github/workflows/ci.yml` adds two jobs:

```yaml
frontend-lint:
  runs-on: ubuntu-latest
  defaults: { run: { working-directory: frontend } }
  steps:
    - uses: actions/checkout@v4
    - uses: pnpm/action-setup@v4
      with: { version: 9 }
    - uses: actions/setup-node@v4
      with:
        node-version: '20'
        cache: pnpm
        cache-dependency-path: frontend/pnpm-lock.yaml
    - run: pnpm install --frozen-lockfile
    - run: pnpm lint
    - run: pnpm typecheck
    - run: pnpm test:unit -- --run

frontend-build:
  runs-on: ubuntu-latest
  defaults: { run: { working-directory: frontend } }
  steps:
    # (same setup as above)
    - run: pnpm install --frozen-lockfile
    - run: pnpm build
```

`ci-status` aggregator job's `needs:` list adds `frontend-lint` and
`frontend-build` so the README badge is honest.

Backend `pytest` job is unchanged. Coverage gate stays 80% (Phase 1 §1.5).

---

## 10. Dev Workflow

### 10.1 Local dev

```bash
# Terminal 1: controller
docker compose -f docker-compose.dev.yml up -d postgres
uv run alembic upgrade head
uv run uvicorn dlw.main:app --port 8000

# Terminal 2: frontend
cd frontend
pnpm install
echo "VITE_API_BASE=http://localhost:8000" > .env.local
pnpm dev    # http://localhost:5173

# Seed a task so the list isn't empty
curl -X POST http://localhost:8000/api/v1/tasks \
  -H "Authorization: Bearer $DLW_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"repo_id":"deepseek-ai/DeepSeek-V3","revision":"abc123def4567890abc123def4567890abc12345","storage_id":1}'
```

### 10.2 Vite proxy

`vite.config.ts`:

```typescript
export default defineConfig({
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
    },
  },
})
```

Frontend uses relative paths (`/api/v1/tasks`) so the browser console doesn't
show CORS noise. `VITE_API_BASE` is for `pnpm preview` / production builds.

### 10.3 README addition

A "Week 3 UI demo" block parallel to the existing "Week 3 demo" block, showing
the two-terminal workflow above and the demo URL.

---

## 11. Acceptance Criteria

- [ ] User can paste a Bearer token, see the task list, click into a task, and
      watch its status + subtask states refresh automatically.
- [ ] Wrong token → kicked back to `/login` with red error message.
- [ ] Manual logout works.
- [ ] All Vitest unit tests pass; CI green.
- [ ] `pnpm build` produces a `dist/` artifact; CI green.
- [ ] Backend `GET /api/v1/tasks/{id}` returns `subtasks: [...]`; new pytest
      cases pass; existing pytest suite still green.
- [ ] `frontend/` directory layout matches §3.
- [ ] README documents the dev workflow.

---

## 12. Implementation Phasing (preview for plan)

The plan will follow the established Phase 1 pattern: TDD per task, 4 milestones.

| Milestone | Deliverable | Verification |
|-----------|-------------|--------------|
| M1: Backend schema split + tests | `TaskDetail` returns subtasks | `pytest tests/api/test_tasks.py` |
| M2: `frontend/` scaffolding | `pnpm dev` boots, no token redirects to `/login` | Browser shows empty Login page (no token in localStorage) |
| M3: Auth + TaskList | Login + list of tasks | Manual: paste token → see seeded task |
| M4: TaskDetail + polling | Detail page + 1s polling + terminal stop | Manual: status changes reflected; succeeded → polling ceases |

Plan task count: ~10–12 tasks across the 4 milestones. CI changes batched into
M2.

---

## 13. References

- Companion full design: `docs/v2.0/10-frontend-wireframes.md`
- Phase 1 scope: `docs/v2.0/08-mvp-roadmap.md` §1
- Protocol (HTTP only used in Phase 1): `docs/v2.0/02-protocol.md` §2–3
- Phase 1 plan precedents: `docs/superpowers/plans/2026-05-09-phase-1-week-3-executor-process.md`
- Auth (Phase 1 simplified): `src/dlw/auth/bearer.py`
- API surface used: `src/dlw/api/tasks.py` (GET /api/v1/tasks, GET /api/v1/tasks/{id})
- Schema source: `src/dlw/schemas/task.py`, `src/dlw/schemas/subtask.py`
