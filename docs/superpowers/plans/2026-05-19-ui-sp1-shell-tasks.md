# UI-SP1 — App Shell + Auth + Tasks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (project's validated variant: 2 opus pre-execution reviewers → implementer/controller per task → controller milestone E2E + frontend CI gates → opus final review → PR). Steps use `- [ ]`.

**Goal:** Turn the 3-page read-only scaffold into a usable app shell where a user can create + monitor download tasks from the browser.

**Architecture:** Frontend-only (`frontend/**` only — NO `src/dlw`, `api/openapi.yaml`, `alembic`, `tools`). Reuse the scaffold's conventions. New foundation (`useLiveResource`, `DataBoundary`, `ui`/`session` stores, nav registry, design tokens, en-US locale) → app shell (sidebar+topbar+command palette) → pages (Dashboard, TaskList upgrade, TaskCreate). All realtime via the single `useLiveResource` seam (SP5 swaps to SSE/WS, zero view change).

**Tech Stack:** Vue 3.5 `<script setup>` TS strict, Pinia setup-stores, `@tanstack/vue-query`, axios `client` (existing interceptors), Element Plus 2.8, vue-i18n 9, Vite, Vitest + `@vue/test-utils` (happy-dom). **No new dependency.**

---

## Locked decisions (from spec §8 + scaffold reality)

1. **`el-table` (not `el-table-v2`) for SP1.** Spec §5 mentioned virtualized `el-table-v2`; el-table-v2's render-function column API is heavy for templated SFCs and SP1 task counts are modest. SP1 uses plain `el-table` (consistent with existing `TaskList.vue`). Virtualization (`el-table-v2`) is **deferred to UI-SP2** where file/chunk tables are genuinely large. Recorded deviation; pre-reviewers may rule.
2. **No ECharts.** 24h trend = inline SVG sparkline (pure helper + tiny SFC). No new dep.
3. **Frontend-only.** Zero backend/api/migration change. Dashboard KPIs/trend = client-side aggregation of `GET /api/v1/tasks`.
4. **Auth:** keep token-paste; add an OIDC button (`window.location → /api/v1/auth/login`). No IdP config needed for the token path.
5. **eslint `plugin:vue/vue3-recommended`** enforces one-attribute-per-line etc. Every task's last step runs `pnpm lint:fix` and folds the autofix into the same commit (the project's known Vue-template lint lesson). The implementer is explicitly allowed to do this.
6. **Commands run from `frontend/`.** `pnpm` (9.12). The 4 gates that must stay green: `pnpm lint` (eslint `--max-warnings=0`), `pnpm typecheck` (`vue-tsc --noEmit`), `pnpm test:unit` (`vitest run`), `pnpm build`.
7. **Login must NOT show the app shell.** `App.vue` renders `AppShell` chrome only when authenticated AND route ≠ `login`; otherwise bare `<RouterView/>`.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/locale/en-US.json` (new) | English messages, full key-parity with zh-CN |
| `src/locale/zh-CN.json` (modify) | add all new keys |
| `src/main.ts` (modify) | register en-US; hydrate ui store; mount global key listener |
| `src/styles/tokens.scss` (new) | design tokens + dark css-vars; imported by main.scss |
| `src/styles/main.scss` (modify) | `@use './tokens'`; theme-aware bg/color |
| `src/stores/ui.ts` (new) | theme/sidebar/locale, persisted, applies `html.dark` + i18n locale |
| `src/stores/session.ts` (new) | decode JWT → principal; `isServiceToken` |
| `src/composables/useLiveResource.ts` (new) | adaptive-poll wrapper over vue-query; pure `computeInterval` |
| `src/composables/useTaskList.ts` (modify) | onto useLiveResource |
| `src/composables/useTaskDetail.ts` (modify) | onto useLiveResource |
| `src/composables/useQuota.ts` (new) | GET /quota/current |
| `src/composables/useTaskMutations.ts` (new) | cancel/delete optimistic mutations |
| `src/nav/registry.ts` (new) | nav items + `visibleNav(role)` |
| `src/components/DataBoundary.vue` (new) | uniform loading/empty/error/forbidden |
| `src/components/Sparkline.vue` (new) | inline SVG sparkline |
| `src/components/shell/AppShell.vue` (new) | sidebar + topbar chrome |
| `src/components/CommandPalette.vue` (new) | ⌘K nav+actions |
| `src/App.vue` (modify) | conditional shell + `<CommandPalette/>` |
| `src/router/index.ts` (modify) | add `/`,`/tasks`,`/tasks/new`; meta roles |
| `src/pages/Dashboard.vue` (new) | KPIs + quota + recent + sparkline |
| `src/pages/TaskList.vue` (modify) | filter + actions + useLiveResource |
| `src/pages/TaskCreate.vue` (new) | create form + service-token guard |
| `src/pages/Login.vue` (modify) | + OIDC button |
| `src/api/types.ts` (modify) | + `QuotaCurrent`, `TaskCreateBody`, `Principal` |
| `tests/unit/*.spec.ts` (new) | per task |
| `.run/pw/ui-sp1.mjs` (new, not CI) | headed Playwright smoke |

`AppLayout.vue`/`EmptyState.vue`/`StatusBadge.vue` kept (DataBoundary reuses EmptyState; AppLayout removed from App.vue but file left in place — deleting it is optional cleanup in the final task).

---

# Milestone M1 — Foundation

### Task 1: en-US locale + zh-CN parity + i18n registration

**Files:** Create `src/locale/en-US.json`; Modify `src/locale/zh-CN.json`, `src/main.ts`; Test `tests/unit/locale.spec.ts`

- [ ] **Step 1: Write the failing test** — `frontend/tests/unit/locale.spec.ts`:
```ts
import { describe, expect, test } from 'vitest'
import zh from '@/locale/zh-CN.json'
import en from '@/locale/en-US.json'

function keys(o: unknown, p = ''): string[] {
  if (o && typeof o === 'object' && !Array.isArray(o)) {
    return Object.entries(o as Record<string, unknown>)
      .flatMap(([k, v]) => keys(v, p ? `${p}.${k}` : k))
  }
  return [p]
}

describe('locale parity', () => {
  test('en-US and zh-CN have identical key sets', () => {
    expect(keys(en).sort()).toEqual(keys(zh).sort())
  })
  test('new keys present', () => {
    for (const k of ['nav.dashboard', 'nav.tasks', 'tasks.create',
      'tasks.filterStatus', 'create.heading', 'create.serviceTokenWarn',
      'shell.theme', 'shell.language', 'palette.placeholder',
      'dashboard.heading', 'errors.quota_exceeded'])
      expect(keys(zh)).toContain(k)
  })
})
```

- [ ] **Step 2: Run** `cd frontend && pnpm test:unit -- tests/unit/locale.spec.ts` → FAIL (no en-US.json).

- [ ] **Step 3: Implement.** Replace `frontend/src/locale/zh-CN.json` with (adds nav/shell/dashboard/create/palette + extra error keys, keeps existing):
```json
{
  "app": { "title": "modelpull", "logout": "退出登录" },
  "login": {
    "heading": "登录 modelpull", "tokenLabel": "Bearer Token",
    "tokenPlaceholder": "粘贴租户用户 JWT", "submit": "登录",
    "tokenRequired": "请输入 token", "oidc": "使用 OIDC 登录"
  },
  "nav": {
    "dashboard": "概览", "tasks": "任务", "createTask": "新建任务"
  },
  "shell": {
    "theme": "主题", "language": "语言", "commandHint": "命令面板 (Ctrl/⌘+K)",
    "tenant": "租户", "role": "角色"
  },
  "palette": {
    "placeholder": "跳转或执行操作…", "navGroup": "导航",
    "actionGroup": "操作", "createTask": "新建任务", "openTaskById": "按 ID 打开任务",
    "openTaskPrompt": "输入任务 ID"
  },
  "dashboard": {
    "heading": "概览", "inProgress": "进行中", "completed": "已完成",
    "failed": "失败", "total": "总计", "recent": "最近任务",
    "quota": "配额", "trend": "近 24h 新建", "quotaBytes": "本月流量",
    "quotaStorage": "存储", "quotaConcurrent": "并发任务"
  },
  "tasks": {
    "listHeading": "任务列表", "empty": "暂无任务，点击「新建任务」创建一个",
    "create": "新建任务", "filterStatus": "状态筛选", "filterAll": "全部",
    "search": "搜索仓库/ID", "cancel": "取消", "delete": "删除",
    "cancelConfirm": "确认取消该任务？", "deleteConfirm": "确认删除该终态任务？",
    "cancelled": "已请求取消", "deleted": "已删除",
    "columns": { "id": "ID", "repo": "仓库", "revision": "Revision",
      "status": "状态", "createdAt": "创建时间", "actions": "操作" },
    "view": "查看", "detailHeading": "任务详情", "subtasksHeading": "子任务",
    "polling": "实时刷新中…", "completed": "已停止刷新（终态）",
    "back": "返回列表", "notFound": "任务不存在或已删除",
    "subtaskColumns": { "filename": "文件名", "size": "大小",
      "sha256": "SHA256", "status": "状态" }
  },
  "create": {
    "heading": "新建下载任务", "repo": "仓库 (org/model)",
    "revision": "Revision (40 位 hex sha)", "storageId": "存储后端 ID",
    "priority": "优先级", "strategy": "源策略", "upgradeFrom": "增量基线 revision",
    "trustNonHf": "信任非 HF 源 sha256", "submit": "创建任务",
    "repoRequired": "请输入仓库", "repoPattern": "格式应为 org/model",
    "revRequired": "请输入 revision", "revPattern": "应为 40 位十六进制 sha",
    "storageRequired": "请输入存储后端 ID",
    "serviceTokenWarn": "当前是 system-admin 服务 token（user_id=0），无法创建任务（owner FK）。请用租户用户 JWT 登录后再创建。",
    "success": "任务已创建"
  },
  "status": {
    "pending": "排队中", "queued": "排队中", "scheduling": "调度中",
    "downloading": "下载中", "succeeded": "成功", "failed": "失败",
    "cancelled": "已取消", "assigned": "已分派", "in_progress": "下载中"
  },
  "errors": {
    "invalid_token": "Token 无效或已失效，请重新登录",
    "service_unavailable": "服务暂不可用，正在重试…",
    "network": "网络错误，请检查连接",
    "quota_exceeded": "配额已超限",
    "conflict": "状态冲突（任务可能非终态或重复）",
    "validation": "请求参数有误",
    "forbidden": "无权限访问"
  }
}
```

Create `frontend/src/locale/en-US.json` (identical structure, English values):
```json
{
  "app": { "title": "modelpull", "logout": "Sign out" },
  "login": {
    "heading": "Sign in to modelpull", "tokenLabel": "Bearer Token",
    "tokenPlaceholder": "Paste a tenant-user JWT", "submit": "Sign in",
    "tokenRequired": "Token is required", "oidc": "Sign in with OIDC"
  },
  "nav": { "dashboard": "Overview", "tasks": "Tasks", "createTask": "New task" },
  "shell": {
    "theme": "Theme", "language": "Language",
    "commandHint": "Command palette (Ctrl/⌘+K)", "tenant": "Tenant", "role": "Role"
  },
  "palette": {
    "placeholder": "Jump to or run an action…", "navGroup": "Navigate",
    "actionGroup": "Actions", "createTask": "New task",
    "openTaskById": "Open task by ID", "openTaskPrompt": "Enter task ID"
  },
  "dashboard": {
    "heading": "Overview", "inProgress": "In progress", "completed": "Completed",
    "failed": "Failed", "total": "Total", "recent": "Recent tasks",
    "quota": "Quota", "trend": "Created (last 24h)", "quotaBytes": "Bytes this month",
    "quotaStorage": "Storage", "quotaConcurrent": "Concurrent tasks"
  },
  "tasks": {
    "listHeading": "Tasks", "empty": "No tasks yet — click \"New task\" to create one",
    "create": "New task", "filterStatus": "Status", "filterAll": "All",
    "search": "Search repo / id", "cancel": "Cancel", "delete": "Delete",
    "cancelConfirm": "Cancel this task?", "deleteConfirm": "Delete this terminal task?",
    "cancelled": "Cancellation requested", "deleted": "Deleted",
    "columns": { "id": "ID", "repo": "Repo", "revision": "Revision",
      "status": "Status", "createdAt": "Created", "actions": "Actions" },
    "view": "View", "detailHeading": "Task detail", "subtasksHeading": "Subtasks",
    "polling": "Live refreshing…", "completed": "Stopped (terminal)",
    "back": "Back to list", "notFound": "Task not found or deleted",
    "subtaskColumns": { "filename": "File", "size": "Size",
      "sha256": "SHA256", "status": "Status" }
  },
  "create": {
    "heading": "Create download task", "repo": "Repo (org/model)",
    "revision": "Revision (40-hex sha)", "storageId": "Storage backend ID",
    "priority": "Priority", "strategy": "Source strategy",
    "upgradeFrom": "Upgrade-from revision", "trustNonHf": "Trust non-HF sha256",
    "submit": "Create task", "repoRequired": "Repo is required",
    "repoPattern": "Expected org/model", "revRequired": "Revision is required",
    "revPattern": "Must be a 40-hex sha", "storageRequired": "Storage ID is required",
    "serviceTokenWarn": "You are using the system-admin service token (user_id=0); task creation fails the owner FK. Sign in with a tenant-user JWT to create tasks.",
    "success": "Task created"
  },
  "status": {
    "pending": "Pending", "queued": "Queued", "scheduling": "Scheduling",
    "downloading": "Downloading", "succeeded": "Succeeded", "failed": "Failed",
    "cancelled": "Cancelled", "assigned": "Assigned", "in_progress": "Downloading"
  },
  "errors": {
    "invalid_token": "Token invalid or expired — please sign in again",
    "service_unavailable": "Service unavailable, retrying…",
    "network": "Network error, check your connection",
    "quota_exceeded": "Quota exceeded", "conflict": "State conflict (task not terminal or duplicate)",
    "validation": "Invalid request parameters", "forbidden": "Forbidden"
  }
}
```

Modify `frontend/src/main.ts` — import en + register:
```ts
import zhCN from './locale/zh-CN.json'
import enUS from './locale/en-US.json'

const i18n = createI18n({
  legacy: false,
  locale: localStorage.getItem('dlw_locale') ?? 'zh-CN',
  fallbackLocale: 'zh-CN',
  messages: { 'zh-CN': zhCN, 'en-US': enUS },
})
```

- [ ] **Step 4: Run** `pnpm test:unit -- tests/unit/locale.spec.ts` → PASS. Then `pnpm lint:fix && pnpm lint && pnpm typecheck`.

- [ ] **Step 5: Commit**
```bash
git add frontend/src/locale frontend/src/main.ts frontend/tests/unit/locale.spec.ts
git commit -m "feat(ui-sp1): en-US locale + zh parity + i18n registration"
```

---

### Task 2: design tokens + dark css-vars

**Files:** Create `src/styles/tokens.scss`; Modify `src/styles/main.scss`; Test `tests/unit/tokens.spec.ts`

- [ ] **Step 1: Write the failing test** — `frontend/tests/unit/tokens.spec.ts`:
```ts
import { describe, expect, test } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'

const css = readFileSync(
  fileURLToPath(new URL('../../src/styles/tokens.scss', import.meta.url)),
  'utf-8',
)

describe('design tokens', () => {
  test('defines status color tokens for all 9 task statuses', () => {
    for (const s of ['pending', 'queued', 'scheduling', 'downloading',
      'succeeded', 'failed', 'cancelled', 'assigned', 'in_progress'])
      expect(css).toContain(`--dlw-status-${s}`)
  })
  test('defines a dark theme block', () => {
    expect(css).toMatch(/(:root\.dark|html\.dark|\.dark)\s*\{/)
  })
  test('defines spacing + radius tokens', () => {
    expect(css).toContain('--dlw-space-')
    expect(css).toContain('--dlw-radius')
  })
})
```

- [ ] **Step 2: Run** `pnpm test:unit -- tests/unit/tokens.spec.ts` → FAIL.

- [ ] **Step 3: Implement** `frontend/src/styles/tokens.scss`:
```scss
:root {
  --dlw-space-1: 4px;  --dlw-space-2: 8px;  --dlw-space-3: 16px;
  --dlw-space-4: 24px; --dlw-space-5: 32px;
  --dlw-radius: 8px;
  --dlw-bg: #f5f7fa;          --dlw-surface: #ffffff;
  --dlw-text: #303133;        --dlw-text-soft: #909399;
  --dlw-border: #ebeef5;
  --dlw-status-pending: #909399;  --dlw-status-queued: #909399;
  --dlw-status-scheduling: #e6a23c; --dlw-status-downloading: #409eff;
  --dlw-status-assigned: #409eff;  --dlw-status-in_progress: #409eff;
  --dlw-status-succeeded: #67c23a; --dlw-status-failed: #f56c6c;
  --dlw-status-cancelled: #909399;
}
:root.dark {
  --dlw-bg: #141414;          --dlw-surface: #1d1e1f;
  --dlw-text: #e5eaf3;        --dlw-text-soft: #8d9095;
  --dlw-border: #363637;
  --dlw-status-pending: #6b6d71;  --dlw-status-queued: #6b6d71;
  --dlw-status-scheduling: #b88230; --dlw-status-downloading: #409eff;
  --dlw-status-assigned: #409eff;  --dlw-status-in_progress: #409eff;
  --dlw-status-succeeded: #529b2e; --dlw-status-failed: #c45656;
  --dlw-status-cancelled: #6b6d71;
}
```

Modify `frontend/src/styles/main.scss` — prepend `@use './tokens';` and make body theme-aware:
```scss
@use './tokens';

*, *::before, *::after { box-sizing: border-box; }

html, body, #app {
  height: 100%; margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC',
    'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
}
body { background-color: var(--dlw-bg); color: var(--dlw-text); }
a { color: var(--el-color-primary); text-decoration: none; }
.page-container { padding: var(--dlw-space-4); max-width: 1280px; margin: 0 auto; }
```

- [ ] **Step 4: Run** `pnpm test:unit -- tests/unit/tokens.spec.ts` → PASS. `pnpm typecheck && pnpm build` (build proves scss `@use` resolves).

- [ ] **Step 5: Commit**
```bash
git add frontend/src/styles frontend/tests/unit/tokens.spec.ts
git commit -m "feat(ui-sp1): design tokens + dark css-vars"
```

---

### Task 3: ui store (theme/sidebar/locale, persisted)

**Files:** Create `src/stores/ui.ts`; Test `tests/unit/ui.spec.ts`

- [ ] **Step 1: Write the failing test** — `frontend/tests/unit/ui.spec.ts`:
```ts
import { beforeEach, describe, expect, test, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const setLocaleMock = vi.fn()
vi.mock('@/i18n', () => ({ setI18nLocale: setLocaleMock }))

import { useUiStore } from '@/stores/ui'

describe('ui store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    document.documentElement.classList.remove('dark')
    setLocaleMock.mockClear()
  })

  test('toggleTheme flips + persists + sets html.dark', () => {
    const ui = useUiStore()
    expect(ui.theme).toBe('light')
    ui.toggleTheme()
    expect(ui.theme).toBe('dark')
    expect(document.documentElement.classList.contains('dark')).toBe(true)
    expect(localStorage.getItem('dlw_theme')).toBe('dark')
  })

  test('setLocale persists + calls i18n setter', () => {
    const ui = useUiStore()
    ui.setLocale('en-US')
    expect(ui.locale).toBe('en-US')
    expect(localStorage.getItem('dlw_locale')).toBe('en-US')
    expect(setLocaleMock).toHaveBeenCalledWith('en-US')
  })

  test('toggleSidebar persists', () => {
    const ui = useUiStore()
    const before = ui.sidebarCollapsed
    ui.toggleSidebar()
    expect(ui.sidebarCollapsed).toBe(!before)
    expect(localStorage.getItem('dlw_sidebar')).toBe(String(!before))
  })
})
```

- [ ] **Step 2: Run** `pnpm test:unit -- tests/unit/ui.spec.ts` → FAIL.

- [ ] **Step 3: Implement.** Create `frontend/src/i18n.ts` (extract i18n instance so store + main share it):
```ts
import { createI18n } from 'vue-i18n'
import zhCN from './locale/zh-CN.json'
import enUS from './locale/en-US.json'

export type LocaleCode = 'zh-CN' | 'en-US'

export const i18n = createI18n({
  legacy: false,
  locale: (localStorage.getItem('dlw_locale') as LocaleCode) ?? 'zh-CN',
  fallbackLocale: 'zh-CN',
  messages: { 'zh-CN': zhCN, 'en-US': enUS },
})

export function setI18nLocale(l: LocaleCode): void {
  i18n.global.locale.value = l
}
```
Modify `frontend/src/main.ts` to import the shared instance instead of building its own:
```ts
import 'element-plus/dist/index.css'
import './styles/main.scss'

import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { VueQueryPlugin } from '@tanstack/vue-query'
import ElementPlus from 'element-plus'

import App from './App.vue'
import router from './router'
import { i18n } from './i18n'
import { useUiStore } from './stores/ui'

const app = createApp(App)
const pinia = createPinia()
app.use(pinia)
app.use(router)
app.use(i18n)
app.use(ElementPlus)
app.use(VueQueryPlugin, {
  queryClientConfig: {
    defaultOptions: {
      queries: { retry: 3, refetchOnWindowFocus: true, staleTime: 5_000 },
    },
  },
})
useUiStore().hydrate()
app.mount('#app')
```
Create `frontend/src/stores/ui.ts`:
```ts
import { ref } from 'vue'
import { defineStore } from 'pinia'
import { setI18nLocale, type LocaleCode } from '@/i18n'

type Theme = 'light' | 'dark'

export const useUiStore = defineStore('ui', () => {
  const theme = ref<Theme>(
    (localStorage.getItem('dlw_theme') as Theme) ??
      (window.matchMedia?.('(prefers-color-scheme: dark)').matches
        ? 'dark' : 'light'),
  )
  const sidebarCollapsed = ref(localStorage.getItem('dlw_sidebar') === 'true')
  const locale = ref<LocaleCode>(
    (localStorage.getItem('dlw_locale') as LocaleCode) ?? 'zh-CN',
  )

  function applyTheme(): void {
    document.documentElement.classList.toggle('dark', theme.value === 'dark')
  }
  function toggleTheme(): void {
    theme.value = theme.value === 'dark' ? 'light' : 'dark'
    localStorage.setItem('dlw_theme', theme.value)
    applyTheme()
  }
  function toggleSidebar(): void {
    sidebarCollapsed.value = !sidebarCollapsed.value
    localStorage.setItem('dlw_sidebar', String(sidebarCollapsed.value))
  }
  function setLocale(l: LocaleCode): void {
    locale.value = l
    localStorage.setItem('dlw_locale', l)
    setI18nLocale(l)
  }
  function hydrate(): void {
    applyTheme()
    setI18nLocale(locale.value)
  }

  return { theme, sidebarCollapsed, locale,
    toggleTheme, toggleSidebar, setLocale, hydrate }
})
```

- [ ] **Step 4: Run** `pnpm test:unit -- tests/unit/ui.spec.ts` → PASS. `pnpm lint:fix && pnpm lint && pnpm typecheck`.

- [ ] **Step 5: Commit**
```bash
git add frontend/src/i18n.ts frontend/src/main.ts frontend/src/stores/ui.ts frontend/tests/unit/ui.spec.ts
git commit -m "feat(ui-sp1): shared i18n + ui store (theme/sidebar/locale)"
```

---

### Task 4: session store (decode JWT → principal, isServiceToken)

**Files:** Create `src/stores/session.ts`; Modify `src/api/types.ts`; Test `tests/unit/session.spec.ts`

- [ ] **Step 1: Write the failing test** — `frontend/tests/unit/session.spec.ts`:
```ts
import { beforeEach, describe, expect, test } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { decodePrincipal } from '@/stores/session'

// JWT = header.<base64url payload>.sig ; only payload matters
function tok(payload: Record<string, unknown>): string {
  const b64 = (o: unknown) =>
    btoa(JSON.stringify(o)).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_')
  return `${b64({ alg: 'HS256' })}.${b64(payload)}.sig`
}

describe('decodePrincipal', () => {
  beforeEach(() => setActivePinia(createPinia()))
  test('tenant user', () => {
    const p = decodePrincipal(tok({ sub: '1', tid: 1, role: 'tenant_admin', pids: [] }))
    expect(p).toEqual({ userId: 1, tenantId: 1, role: 'tenant_admin',
      projectIds: [], isServiceToken: false })
  })
  test('service token (sub=0) → isServiceToken', () => {
    const p = decodePrincipal(tok({ sub: '0', tid: 1, role: 'system_admin', pids: [] }))
    expect(p?.isServiceToken).toBe(true)
  })
  test('role system_admin → isServiceToken even if sub != 0', () => {
    const p = decodePrincipal(tok({ sub: '5', tid: 1, role: 'system_admin', pids: [] }))
    expect(p?.isServiceToken).toBe(true)
  })
  test('null / malformed → null', () => {
    expect(decodePrincipal(null)).toBeNull()
    expect(decodePrincipal('garbage')).toBeNull()
    expect(decodePrincipal('a.notbase64!.c')).toBeNull()
  })
})
```

- [ ] **Step 2: Run** `pnpm test:unit -- tests/unit/session.spec.ts` → FAIL.

- [ ] **Step 3: Implement.** Append to `frontend/src/api/types.ts`:
```ts
export interface Principal {
  userId: number
  tenantId: number
  role: string
  projectIds: number[]
  isServiceToken: boolean
}

export interface QuotaCurrent {
  tenant_id: number
  bytes_used_month: number
  bytes_quota_month: number
  storage_gb_used: number
  storage_gb_quota: number
  concurrent_tasks: number
  concurrent_quota: number
}

export interface TaskCreateBody {
  repo_id: string
  revision: string
  storage_id: number
  priority?: number
  source_strategy?: string
  source_blacklist?: string[]
  trust_non_hf_sha256?: boolean
  upgrade_from_revision?: string | null
}
```
Create `frontend/src/stores/session.ts`:
```ts
import { computed } from 'vue'
import { defineStore } from 'pinia'
import { useAuthStore } from '@/stores/auth'
import type { Principal } from '@/api/types'

export function decodePrincipal(token: string | null): Principal | null {
  if (!token) return null
  const parts = token.split('.')
  if (parts.length !== 3) return null
  try {
    const b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/')
    const json = decodeURIComponent(
      atob(b64).split('').map(
        (c) => '%' + c.charCodeAt(0).toString(16).padStart(2, '0')).join(''),
    )
    const c = JSON.parse(json) as Record<string, unknown>
    const userId = Number(c.sub)
    const role = String(c.role ?? '')
    if (!Number.isFinite(userId)) return null
    return {
      userId,
      tenantId: Number(c.tid ?? 0),
      role,
      projectIds: Array.isArray(c.pids) ? (c.pids as number[]) : [],
      isServiceToken: userId === 0 || role === 'system_admin',
    }
  } catch {
    return null
  }
}

export const useSessionStore = defineStore('session', () => {
  const auth = useAuthStore()
  const principal = computed(() => decodePrincipal(auth.accessToken))
  const role = computed(() => principal.value?.role ?? 'guest')
  const isServiceToken = computed(() => principal.value?.isServiceToken ?? false)
  return { principal, role, isServiceToken }
})
```

- [ ] **Step 4: Run** `pnpm test:unit -- tests/unit/session.spec.ts` → PASS. `pnpm lint:fix && pnpm lint && pnpm typecheck`.

- [ ] **Step 5: Commit**
```bash
git add frontend/src/stores/session.ts frontend/src/api/types.ts frontend/tests/unit/session.spec.ts
git commit -m "feat(ui-sp1): session store (JWT principal + isServiceToken)"
```

---

### Task 5: useLiveResource + refactor existing composables

**Files:** Create `src/composables/useLiveResource.ts`; Modify `src/composables/useTaskList.ts`, `src/composables/useTaskDetail.ts`; Modify `tests/unit/useTaskDetail.spec.ts`; Test `tests/unit/useLiveResource.spec.ts`

- [ ] **Step 1: Write the failing test** — `frontend/tests/unit/useLiveResource.spec.ts`:
```ts
import { describe, expect, test } from 'vitest'
import { computeInterval } from '@/composables/useLiveResource'

describe('computeInterval', () => {
  const base = 2000
  test('active + visible → base', () => {
    expect(computeInterval({ base, terminal: false, hidden: false, errored: false })).toBe(2000)
  })
  test('terminal → false (stop)', () => {
    expect(computeInterval({ base, terminal: true, hidden: false, errored: false })).toBe(false)
  })
  test('hidden → base × 3', () => {
    expect(computeInterval({ base, terminal: false, hidden: true, errored: false })).toBe(6000)
  })
  test('errored (no data) → 5000 backoff', () => {
    expect(computeInterval({ base, terminal: false, hidden: false, errored: true })).toBe(5000)
  })
  test('terminal beats hidden/errored', () => {
    expect(computeInterval({ base, terminal: true, hidden: true, errored: true })).toBe(false)
  })
})
```
Replace `frontend/tests/unit/useTaskDetail.spec.ts` import + helper to use the new module (keep equivalent assertions):
```ts
import { describe, expect, test } from 'vitest'
import { computeInterval } from '@/composables/useLiveResource'

describe('task-detail polling via computeInterval', () => {
  test('non-terminal → 1000', () => {
    expect(computeInterval({ base: 1000, terminal: false, hidden: false, errored: false })).toBe(1000)
  })
  test('terminal → false', () => {
    expect(computeInterval({ base: 1000, terminal: true, hidden: false, errored: false })).toBe(false)
  })
  test('errored first fetch → 5000', () => {
    expect(computeInterval({ base: 1000, terminal: false, hidden: false, errored: true })).toBe(5000)
  })
})
```

- [ ] **Step 2: Run** `pnpm test:unit -- tests/unit/useLiveResource.spec.ts` → FAIL.

- [ ] **Step 3: Implement** `frontend/src/composables/useLiveResource.ts`:
```ts
import { useQuery, type QueryKey } from '@tanstack/vue-query'
import { type MaybeRefOrGetter, toValue } from 'vue'

const ERROR_BACKOFF_MS = 5_000
const HIDDEN_MULTIPLIER = 3

export function computeInterval(o: {
  base: number; terminal: boolean; hidden: boolean; errored: boolean
}): number | false {
  if (o.terminal) return false
  if (o.errored) return ERROR_BACKOFF_MS
  return o.hidden ? o.base * HIDDEN_MULTIPLIER : o.base
}

export interface LiveOptions<T> {
  baseIntervalMs: number
  isTerminal?: (data: T) => boolean
  staleTime?: number
}

/**
 * Single realtime seam. Today: adaptive polling on vue-query. UI-SP5 swaps
 * the internals to SSE/WS — consumers (views) never change.
 */
export function useLiveResource<T>(
  key: MaybeRefOrGetter<QueryKey>,
  fetcher: () => Promise<T>,
  opts: LiveOptions<T>,
) {
  return useQuery<T>({
    queryKey: () => toValue(key),
    queryFn: fetcher,
    staleTime: opts.staleTime ?? 0,
    refetchInterval: (query) => {
      const data = query.state.data as T | undefined
      const errored = query.state.status === 'error'
      const terminal = data !== undefined && !!opts.isTerminal?.(data)
      const hidden =
        typeof document !== 'undefined' && document.visibilityState === 'hidden'
      return computeInterval({
        base: opts.baseIntervalMs, terminal, hidden, errored,
      })
    },
  })
}
```
Replace `frontend/src/composables/useTaskList.ts`:
```ts
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { TaskListResponse } from '@/api/types'

export function useTaskList() {
  return useLiveResource<TaskListResponse>(
    ['tasks'],
    async () => (await client.get<TaskListResponse>('/api/v1/tasks')).data,
    { baseIntervalMs: 5_000, staleTime: 5_000 },
  )
}
```
Replace `frontend/src/composables/useTaskDetail.ts`:
```ts
import type { Ref } from 'vue'
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import { TERMINAL_STATUSES, type TaskDetail } from '@/api/types'

export function useTaskDetail(taskId: Ref<string>) {
  return useLiveResource<TaskDetail>(
    () => ['task', taskId.value],
    async () => (await client.get<TaskDetail>(`/api/v1/tasks/${taskId.value}`)).data,
    { baseIntervalMs: 1_000, isTerminal: (d) => TERMINAL_STATUSES.has(d.status) },
  )
}
```

- [ ] **Step 4: Run** `pnpm test:unit` (whole suite — the replaced useTaskDetail.spec must pass too) → all PASS. `pnpm lint:fix && pnpm lint && pnpm typecheck`.

- [ ] **Step 5: Commit**
```bash
git add frontend/src/composables frontend/tests/unit/useLiveResource.spec.ts frontend/tests/unit/useTaskDetail.spec.ts
git commit -m "feat(ui-sp1): useLiveResource seam + refactor task composables"
```

---

### Task 6: DataBoundary component

**Files:** Create `src/components/DataBoundary.vue`; Test `tests/unit/DataBoundary.spec.ts`

- [ ] **Step 1: Write the failing test** — `frontend/tests/unit/DataBoundary.spec.ts`:
```ts
import { describe, expect, test } from 'vitest'
import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createI18n } from 'vue-i18n'
import DataBoundary from '@/components/DataBoundary.vue'
import zh from '@/locale/zh-CN.json'

const i18n = createI18n({ legacy: false, locale: 'zh-CN', messages: { 'zh-CN': zh } })
const mountB = (props: Record<string, unknown>) =>
  mount(DataBoundary, {
    props, slots: { default: '<div class="content">DATA</div>' },
    global: { plugins: [ElementPlus, i18n] },
  })

describe('DataBoundary', () => {
  test('loading → skeleton, no content', () => {
    const w = mountB({ loading: true })
    expect(w.findComponent({ name: 'ElSkeleton' }).exists()).toBe(true)
    expect(w.find('.content').exists()).toBe(false)
  })
  test('forbidden → forbidden message', () => {
    const w = mountB({ loading: false, forbidden: true })
    expect(w.text()).toContain(zh.errors.forbidden)
    expect(w.find('.content').exists()).toBe(false)
  })
  test('error → alert', () => {
    const w = mountB({ loading: false, error: true })
    expect(w.findComponent({ name: 'ElAlert' }).exists()).toBe(true)
  })
  test('empty → EmptyState', () => {
    const w = mountB({ loading: false, isEmpty: true })
    expect(w.findComponent({ name: 'EmptyState' }).exists()).toBe(true)
  })
  test('ok → renders default slot', () => {
    const w = mountB({ loading: false })
    expect(w.find('.content').text()).toBe('DATA')
  })
})
```

- [ ] **Step 2: Run** `pnpm test:unit -- tests/unit/DataBoundary.spec.ts` → FAIL.

- [ ] **Step 3: Implement** `frontend/src/components/DataBoundary.vue`:
```vue
<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import EmptyState from '@/components/EmptyState.vue'

withDefaults(defineProps<{
  loading?: boolean
  error?: boolean
  isEmpty?: boolean
  forbidden?: boolean
  emptyMessage?: string
}>(), { loading: false, error: false, isEmpty: false, forbidden: false })

const { t } = useI18n()
</script>

<template>
  <el-skeleton
    v-if="loading"
    :rows="5"
    animated
  />
  <EmptyState
    v-else-if="forbidden"
    :message="t('errors.forbidden')"
  />
  <el-alert
    v-else-if="error"
    type="error"
    :title="t('errors.service_unavailable')"
    :closable="false"
  />
  <EmptyState
    v-else-if="isEmpty"
    :message="emptyMessage ?? ''"
  >
    <slot name="empty-action" />
  </EmptyState>
  <slot v-else />
</template>
```

- [ ] **Step 4: Run** `pnpm test:unit -- tests/unit/DataBoundary.spec.ts` → PASS. `pnpm lint:fix && pnpm lint && pnpm typecheck`.

- [ ] **Step 5: Commit**
```bash
git add frontend/src/components/DataBoundary.vue frontend/tests/unit/DataBoundary.spec.ts
git commit -m "feat(ui-sp1): DataBoundary state wrapper"
```

---

### Task 7: nav registry + role filtering

**Files:** Create `src/nav/registry.ts`; Test `tests/unit/nav.spec.ts`

- [ ] **Step 1: Write the failing test** — `frontend/tests/unit/nav.spec.ts`:
```ts
import { describe, expect, test } from 'vitest'
import { NAV_ITEMS, visibleNav } from '@/nav/registry'

describe('nav registry', () => {
  test('all items have route + labelKey', () => {
    for (const i of NAV_ITEMS) {
      expect(i.route).toBeTruthy()
      expect(i.labelKey).toMatch(/^nav\./)
    }
  })
  test('visibleNav: no roles → visible to everyone', () => {
    const names = visibleNav('guest').map((i) => i.route)
    expect(names).toContain('taskList')
    expect(names).toContain('dashboard')
  })
  test('role-gated item hidden for wrong role', () => {
    const gated = { route: 'x', labelKey: 'nav.x', icon: 'i', roles: ['system_admin'] }
    expect(visibleNav('tenant_admin', [...NAV_ITEMS, gated]).map((i) => i.route))
      .not.toContain('x')
    expect(visibleNav('system_admin', [...NAV_ITEMS, gated]).map((i) => i.route))
      .toContain('x')
  })
})
```

- [ ] **Step 2: Run** `pnpm test:unit -- tests/unit/nav.spec.ts` → FAIL.

- [ ] **Step 3: Implement** `frontend/src/nav/registry.ts`:
```ts
export interface NavItem {
  route: string          // route name
  labelKey: string       // i18n key under nav.*
  icon: string           // element-plus icon component name
  roles?: string[]       // if set, visible only to these roles
}

export const NAV_ITEMS: NavItem[] = [
  { route: 'dashboard', labelKey: 'nav.dashboard', icon: 'Odometer' },
  { route: 'taskList', labelKey: 'nav.tasks', icon: 'List' },
  { route: 'taskCreate', labelKey: 'nav.createTask', icon: 'Plus' },
]

export function visibleNav(role: string, items: NavItem[] = NAV_ITEMS): NavItem[] {
  return items.filter((i) => !i.roles || i.roles.includes(role))
}
```

- [ ] **Step 4: Run** `pnpm test:unit -- tests/unit/nav.spec.ts` → PASS. `pnpm lint:fix && pnpm lint && pnpm typecheck`.

- [ ] **Step 5: Commit**
```bash
git add frontend/src/nav/registry.ts frontend/tests/unit/nav.spec.ts
git commit -m "feat(ui-sp1): nav registry + role filtering"
```

---

# Milestone M2 — Shell

### Task 8: AppShell (sidebar + topbar) + App.vue conditional layout

**Files:** Create `src/components/shell/AppShell.vue`; Modify `src/App.vue`; Test `tests/unit/AppShell.spec.ts`

- [ ] **Step 1: Write the failing test** — `frontend/tests/unit/AppShell.spec.ts`:
```ts
import { beforeEach, describe, expect, test, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { createI18n } from 'vue-i18n'
import AppShell from '@/components/shell/AppShell.vue'
import zh from '@/locale/zh-CN.json'
import { useAuthStore } from '@/stores/auth'

const i18n = createI18n({ legacy: false, locale: 'zh-CN', messages: { 'zh-CN': zh } })
const push = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push }),
  useRoute: () => ({ name: 'taskList' }),
  RouterView: { template: '<div class="rv" />' },
}))

function mountShell() {
  return mount(AppShell, { global: { plugins: [ElementPlus, i18n] } })
}

describe('AppShell', () => {
  beforeEach(() => { setActivePinia(createPinia()); push.mockClear() })

  test('authenticated → renders nav items', () => {
    useAuthStore().login('h.' + btoa(JSON.stringify(
      { sub: '1', tid: 1, role: 'tenant_admin', pids: [] })) + '.s')
    const w = mountShell()
    expect(w.text()).toContain(zh.nav.tasks)
    expect(w.text()).toContain(zh.nav.dashboard)
  })

  test('logout calls auth.logout + redirects', async () => {
    const auth = useAuthStore()
    auth.login('h.' + btoa(JSON.stringify(
      { sub: '1', tid: 1, role: 'tenant_admin', pids: [] })) + '.s')
    const w = mountShell()
    await w.find('[data-test=logout]').trigger('click')
    expect(auth.isAuthenticated).toBe(false)
    expect(push).toHaveBeenCalledWith('/login')
  })
})
```

- [ ] **Step 2: Run** `pnpm test:unit -- tests/unit/AppShell.spec.ts` → FAIL.

- [ ] **Step 3: Implement** `frontend/src/components/shell/AppShell.vue`:
```vue
<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { useSessionStore } from '@/stores/session'
import { visibleNav } from '@/nav/registry'

const { t } = useI18n()
const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const ui = useUiStore()
const session = useSessionStore()

const items = computed(() => visibleNav(session.role))
const activeRoute = computed(() => String(route.name ?? ''))

function go(name: string) {
  router.push({ name })
}
function logout() {
  auth.logout()
  router.push('/login')
}
function toggleLocale() {
  ui.setLocale(ui.locale === 'zh-CN' ? 'en-US' : 'zh-CN')
}
</script>

<template>
  <el-container class="app-shell">
    <el-aside :width="ui.sidebarCollapsed ? '64px' : '220px'">
      <div class="brand">
        <img
          src="/favicon.svg"
          alt="logo"
          class="logo"
        >
        <span v-show="!ui.sidebarCollapsed">{{ t('app.title') }}</span>
      </div>
      <el-menu
        :default-active="activeRoute"
        :collapse="ui.sidebarCollapsed"
      >
        <el-menu-item
          v-for="i in items"
          :key="i.route"
          :index="i.route"
          @click="go(i.route)"
        >
          <span>{{ t(i.labelKey) }}</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="topbar">
        <el-button
          link
          @click="ui.toggleSidebar()"
        >
          ☰
        </el-button>
        <span class="hint">{{ t('shell.commandHint') }}</span>
        <div class="spacer" />
        <el-tag
          v-if="session.principal"
          size="small"
          type="info"
        >
          {{ t('shell.tenant') }} {{ session.principal.tenantId }} ·
          {{ session.principal.role }}
        </el-tag>
        <el-button
          link
          @click="ui.toggleTheme()"
        >
          {{ ui.theme === 'dark' ? '🌙' : '☀️' }}
        </el-button>
        <el-button
          link
          @click="toggleLocale"
        >
          {{ ui.locale === 'zh-CN' ? 'EN' : '中' }}
        </el-button>
        <el-button
          data-test="logout"
          link
          type="primary"
          @click="logout"
        >
          {{ t('app.logout') }}
        </el-button>
      </el-header>
      <el-main>
        <slot />
      </el-main>
    </el-container>
  </el-container>
</template>

<style lang="scss" scoped>
.app-shell { min-height: 100vh; }
.el-aside {
  background: var(--dlw-surface);
  border-right: 1px solid var(--dlw-border);
  transition: width 0.2s;
}
.brand {
  display: flex; align-items: center; gap: var(--dlw-space-2);
  padding: var(--dlw-space-3); font-weight: 600;
  .logo { width: 28px; height: 28px; }
}
.topbar {
  background: var(--dlw-surface);
  border-bottom: 1px solid var(--dlw-border);
  display: flex; align-items: center; gap: var(--dlw-space-3);
  .hint { color: var(--dlw-text-soft); font-size: 12px; }
  .spacer { flex: 1; }
}
</style>
```
Replace `frontend/src/App.vue`:
```vue
<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import AppShell from '@/components/shell/AppShell.vue'
import CommandPalette from '@/components/CommandPalette.vue'

const route = useRoute()
const auth = useAuthStore()
const chrome = computed(
  () => auth.isAuthenticated && route.name !== 'login',
)
</script>

<template>
  <template v-if="chrome">
    <AppShell>
      <RouterView />
    </AppShell>
    <CommandPalette />
  </template>
  <RouterView v-else />
</template>
```
(NOTE: `CommandPalette.vue` is created in Task 9; until then `App.vue` won't typecheck. To keep Task 8 self-contained, **Task 8 Step 3 also creates a minimal placeholder** `frontend/src/components/CommandPalette.vue`:
```vue
<script setup lang="ts"></script>
<template><span /></template>
```
Task 9 replaces it with the real component.)

- [ ] **Step 4: Run** `pnpm test:unit -- tests/unit/AppShell.spec.ts` → PASS. `pnpm lint:fix && pnpm lint && pnpm typecheck && pnpm build`.

- [ ] **Step 5: Commit**
```bash
git add frontend/src/components/shell/AppShell.vue frontend/src/components/CommandPalette.vue frontend/src/App.vue frontend/tests/unit/AppShell.spec.ts
git commit -m "feat(ui-sp1): AppShell (sidebar+topbar) + conditional layout"
```

---

### Task 9: CommandPalette (⌘/Ctrl+K)

**Files:** Modify `src/components/CommandPalette.vue` (replace placeholder); Test `tests/unit/palette.spec.ts`

- [ ] **Step 1: Write the failing test** — `frontend/tests/unit/palette.spec.ts`:
```ts
import { describe, expect, test } from 'vitest'
import { buildCommands } from '@/components/palette'

describe('buildCommands', () => {
  const t = (k: string) => k
  test('includes nav items + create + open-by-id', () => {
    const cmds = buildCommands('tenant_admin', t)
    const ids = cmds.map((c) => c.id)
    expect(ids).toContain('nav:dashboard')
    expect(ids).toContain('nav:taskList')
    expect(ids).toContain('action:createTask')
    expect(ids).toContain('action:openTaskById')
  })
  test('role-gates nav', () => {
    const cmds = buildCommands('guest', t)
    expect(cmds.find((c) => c.id === 'nav:dashboard')).toBeTruthy()
  })
})
```

- [ ] **Step 2: Run** `pnpm test:unit -- tests/unit/palette.spec.ts` → FAIL.

- [ ] **Step 3: Implement.** Create `frontend/src/components/palette.ts`:
```ts
import { visibleNav } from '@/nav/registry'

export interface Command {
  id: string
  label: string
  kind: 'nav' | 'action'
  routeName?: string
  action?: 'createTask' | 'openTaskById'
}

export function buildCommands(role: string, t: (k: string) => string): Command[] {
  const nav: Command[] = visibleNav(role).map((i) => ({
    id: `nav:${i.route}`, label: t(i.labelKey), kind: 'nav', routeName: i.route,
  }))
  const actions: Command[] = [
    { id: 'action:createTask', label: t('palette.createTask'),
      kind: 'action', action: 'createTask' },
    { id: 'action:openTaskById', label: t('palette.openTaskById'),
      kind: 'action', action: 'openTaskById' },
  ]
  return [...nav, ...actions]
}
```
Replace `frontend/src/components/CommandPalette.vue`:
```vue
<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { useSessionStore } from '@/stores/session'
import { buildCommands, type Command } from '@/components/palette'

const { t } = useI18n()
const router = useRouter()
const session = useSessionStore()
const open = ref(false)
const q = ref('')

const all = computed(() => buildCommands(session.role, t))
const filtered = computed(() =>
  all.value.filter((c) => c.label.toLowerCase().includes(q.value.toLowerCase())))

function onKey(e: KeyboardEvent) {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    open.value = !open.value
    q.value = ''
  } else if (e.key === 'Escape') {
    open.value = false
  }
}
onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => window.removeEventListener('keydown', onKey))

async function run(c: Command) {
  open.value = false
  if (c.kind === 'nav' && c.routeName) {
    router.push({ name: c.routeName })
  } else if (c.action === 'createTask') {
    router.push({ name: 'taskCreate' })
  } else if (c.action === 'openTaskById') {
    const r = await ElMessageBox.prompt(t('palette.openTaskPrompt'), '', {
      inputPattern: /\S+/,
    }).catch(() => null)
    if (r?.value) router.push({ name: 'taskDetail', params: { id: r.value.trim() } })
  }
}
</script>

<template>
  <el-dialog
    v-model="open"
    :show-close="false"
    top="12vh"
    width="520px"
  >
    <el-input
      v-model="q"
      :placeholder="t('palette.placeholder')"
      autofocus
    />
    <el-scrollbar max-height="320px">
      <div
        v-for="c in filtered"
        :key="c.id"
        class="cmd"
        @click="run(c)"
      >
        {{ c.label }}
        <small>{{ c.kind === 'nav' ? t('palette.navGroup') : t('palette.actionGroup') }}</small>
      </div>
    </el-scrollbar>
  </el-dialog>
</template>

<style lang="scss" scoped>
.cmd {
  display: flex; justify-content: space-between; align-items: center;
  padding: var(--dlw-space-2) var(--dlw-space-3); cursor: pointer;
  border-radius: var(--dlw-radius);
  small { color: var(--dlw-text-soft); }
  &:hover { background: var(--dlw-bg); }
}
</style>
```

- [ ] **Step 4: Run** `pnpm test:unit -- tests/unit/palette.spec.ts` → PASS. `pnpm lint:fix && pnpm lint && pnpm typecheck`.

- [ ] **Step 5: Commit**
```bash
git add frontend/src/components/palette.ts frontend/src/components/CommandPalette.vue frontend/tests/unit/palette.spec.ts
git commit -m "feat(ui-sp1): command palette (Ctrl/Cmd+K)"
```

---

### Task 10: router — add Dashboard/TaskList/TaskCreate routes

**Files:** Modify `src/router/index.ts`; Test `tests/unit/router.spec.ts`

- [ ] **Step 1: Write the failing test** — `frontend/tests/unit/router.spec.ts`:
```ts
import { describe, expect, test } from 'vitest'
import { routes } from '@/router'

describe('routes', () => {
  test('has dashboard / taskList / taskCreate / taskDetail / login', () => {
    const byName = Object.fromEntries(
      routes.filter((r) => r.name).map((r) => [r.name, r]))
    expect(byName.dashboard?.path).toBe('/')
    expect(byName.taskList?.path).toBe('/tasks')
    expect(byName.taskCreate?.path).toBe('/tasks/new')
    expect(byName.taskDetail?.path).toBe('/tasks/:id')
    expect(byName.login?.meta?.public).toBe(true)
  })
})
```

- [ ] **Step 2: Run** `pnpm test:unit -- tests/unit/router.spec.ts` → FAIL.

- [ ] **Step 3: Implement** `frontend/src/router/index.ts`:
```ts
import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

export const routes: RouteRecordRaw[] = [
  {
    path: '/login', name: 'login',
    component: () => import('@/pages/Login.vue'),
    meta: { public: true },
  },
  {
    path: '/', name: 'dashboard',
    component: () => import('@/pages/Dashboard.vue'),
  },
  {
    path: '/tasks', name: 'taskList',
    component: () => import('@/pages/TaskList.vue'),
  },
  {
    path: '/tasks/new', name: 'taskCreate',
    component: () => import('@/pages/TaskCreate.vue'),
  },
  {
    path: '/tasks/:id', name: 'taskDetail',
    component: () => import('@/pages/TaskDetail.vue'),
    props: true,
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach((to) => {
  if (to.meta.public) return true
  const auth = useAuthStore()
  if (!auth.isAuthenticated) return { path: '/login' }
  return true
})

export { router }
export default router
```

- [ ] **Step 2b: Run** `pnpm test:unit -- tests/unit/router.spec.ts` → still FAIL until Dashboard/TaskCreate pages exist (dynamic imports are lazy; the route-config test doesn't import them, so it PASSES now). Expected PASS.

- [ ] **Step 4: Run** `pnpm test:unit -- tests/unit/router.spec.ts` → PASS. `pnpm lint:fix && pnpm lint && pnpm typecheck` (typecheck tolerates not-yet-created lazy page imports? No — `vue-tsc` resolves `import('@/pages/Dashboard.vue')`. **Therefore create empty placeholder pages now**: `frontend/src/pages/Dashboard.vue` and `frontend/src/pages/TaskCreate.vue` each:
```vue
<script setup lang="ts"></script>
<template><div class="page-container" /></template>
```
Tasks 11/14 replace them.) Re-run `pnpm typecheck` → green.

- [ ] **Step 5: Commit**
```bash
git add frontend/src/router/index.ts frontend/src/pages/Dashboard.vue frontend/src/pages/TaskCreate.vue frontend/tests/unit/router.spec.ts
git commit -m "feat(ui-sp1): routes for dashboard/tasks/create (+ placeholders)"
```

---

# Milestone M3 — Pages

### Task 11: Sparkline + Dashboard

**Files:** Create `src/components/Sparkline.vue`, `src/composables/useQuota.ts`, `src/dashboard/aggregate.ts`; Modify `src/pages/Dashboard.vue`; Test `tests/unit/dashboard.spec.ts`

- [ ] **Step 1: Write the failing test** — `frontend/tests/unit/dashboard.spec.ts`:
```ts
import { describe, expect, test } from 'vitest'
import { aggregateKpis, bucket24h } from '@/dashboard/aggregate'
import type { TaskRead } from '@/api/types'

const mk = (status: TaskRead['status'], createdAt: string): TaskRead => ({
  id: Math.random().toString(36), repo_id: 'o/r', revision: 'a',
  status, priority: 1, created_at: createdAt, completed_at: null,
  error_message: null,
})

describe('dashboard aggregate', () => {
  test('aggregateKpis counts by bucket', () => {
    const k = aggregateKpis([
      mk('downloading', '2026-05-19T00:00:00Z'),
      mk('scheduling', '2026-05-19T00:00:00Z'),
      mk('succeeded', '2026-05-19T00:00:00Z'),
      mk('failed', '2026-05-19T00:00:00Z'),
    ])
    expect(k).toEqual({ inProgress: 2, completed: 1, failed: 1, total: 4 })
  })
  test('bucket24h returns 24 hourly counts within window', () => {
    const now = new Date('2026-05-19T12:00:00Z')
    const b = bucket24h([
      mk('succeeded', '2026-05-19T11:30:00Z'),
      mk('succeeded', '2026-05-19T11:45:00Z'),
      mk('succeeded', '2026-05-10T00:00:00Z'), // outside 24h → excluded
    ], now)
    expect(b).toHaveLength(24)
    expect(b.reduce((a, c) => a + c, 0)).toBe(2)
  })
})
```

- [ ] **Step 2: Run** `pnpm test:unit -- tests/unit/dashboard.spec.ts` → FAIL.

- [ ] **Step 3: Implement.** Create `frontend/src/dashboard/aggregate.ts`:
```ts
import type { TaskRead, TaskStatus } from '@/api/types'

const IN_PROGRESS: ReadonlySet<TaskStatus> = new Set([
  'pending', 'queued', 'scheduling', 'downloading',
])

export function aggregateKpis(tasks: TaskRead[]) {
  let inProgress = 0, completed = 0, failed = 0
  for (const t of tasks) {
    if (IN_PROGRESS.has(t.status)) inProgress++
    else if (t.status === 'succeeded') completed++
    else if (t.status === 'failed') failed++
  }
  return { inProgress, completed, failed, total: tasks.length }
}

export function bucket24h(tasks: TaskRead[], now: Date = new Date()): number[] {
  const buckets = new Array<number>(24).fill(0)
  const end = now.getTime()
  const start = end - 24 * 3600_000
  for (const t of tasks) {
    const ts = new Date(t.created_at).getTime()
    if (ts >= start && ts <= end) {
      const idx = Math.min(23, Math.floor((ts - start) / 3600_000))
      buckets[idx]++
    }
  }
  return buckets
}
```
Create `frontend/src/composables/useQuota.ts`:
```ts
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { QuotaCurrent } from '@/api/types'

export function useQuota() {
  return useLiveResource<QuotaCurrent>(
    ['quota'],
    async () => (await client.get<QuotaCurrent>('/api/v1/quota/current')).data,
    { baseIntervalMs: 30_000, staleTime: 30_000 },
  )
}
```
Create `frontend/src/components/Sparkline.vue`:
```vue
<script setup lang="ts">
import { computed } from 'vue'
const props = defineProps<{ data: number[]; width?: number; height?: number }>()
const w = computed(() => props.width ?? 240)
const h = computed(() => props.height ?? 48)
const points = computed(() => {
  const d = props.data
  const max = Math.max(1, ...d)
  const step = d.length > 1 ? w.value / (d.length - 1) : w.value
  return d.map((v, i) =>
    `${(i * step).toFixed(1)},${(h.value - (v / max) * h.value).toFixed(1)}`).join(' ')
})
</script>

<template>
  <svg
    :width="w"
    :height="h"
    class="sparkline"
  >
    <polyline
      :points="points"
      fill="none"
      :stroke="'var(--el-color-primary)'"
      stroke-width="2"
    />
  </svg>
</template>
```
Replace `frontend/src/pages/Dashboard.vue`:
```vue
<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import DataBoundary from '@/components/DataBoundary.vue'
import Sparkline from '@/components/Sparkline.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { useTaskList } from '@/composables/useTaskList'
import { useQuota } from '@/composables/useQuota'
import { aggregateKpis, bucket24h } from '@/dashboard/aggregate'

const { t } = useI18n()
const router = useRouter()
const { data, isLoading, isError } = useTaskList()
const { data: quota } = useQuota()

const tasks = computed(() => data.value?.items ?? [])
const kpi = computed(() => aggregateKpis(tasks.value))
const trend = computed(() => bucket24h(tasks.value))
const recent = computed(() => tasks.value.slice(0, 8))

function open(id: string) {
  router.push({ name: 'taskDetail', params: { id } })
}
</script>

<template>
  <div class="page-container">
    <h2>{{ t('dashboard.heading') }}</h2>
    <DataBoundary
      :loading="isLoading"
      :error="isError"
    >
      <div class="kpis">
        <el-card>{{ t('dashboard.inProgress') }}<b>{{ kpi.inProgress }}</b></el-card>
        <el-card>{{ t('dashboard.completed') }}<b>{{ kpi.completed }}</b></el-card>
        <el-card>{{ t('dashboard.failed') }}<b>{{ kpi.failed }}</b></el-card>
        <el-card>{{ t('dashboard.total') }}<b>{{ kpi.total }}</b></el-card>
      </div>

      <el-card style="margin-top: 16px">
        <template #header>{{ t('dashboard.trend') }}</template>
        <Sparkline :data="trend" />
      </el-card>

      <el-card
        v-if="quota"
        style="margin-top: 16px"
      >
        <template #header>{{ t('dashboard.quota') }}</template>
        <p>{{ t('dashboard.quotaBytes') }}: {{ quota.bytes_used_month }} /
          {{ quota.bytes_quota_month }}</p>
        <p>{{ t('dashboard.quotaConcurrent') }}: {{ quota.concurrent_tasks }} /
          {{ quota.concurrent_quota }}</p>
      </el-card>

      <el-card style="margin-top: 16px">
        <template #header>{{ t('dashboard.recent') }}</template>
        <el-table :data="recent">
          <el-table-column
            prop="repo_id"
            :label="t('tasks.columns.repo')"
          />
          <el-table-column
            :label="t('tasks.columns.status')"
            width="120"
          >
            <template #default="{ row }">
              <StatusBadge :status="row.status" />
            </template>
          </el-table-column>
          <el-table-column width="80">
            <template #default="{ row }">
              <el-button
                link
                type="primary"
                @click="open(row.id)"
              >
                {{ t('tasks.view') }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </DataBoundary>
  </div>
</template>

<style lang="scss" scoped>
.kpis {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--dlw-space-3);
  b { display: block; font-size: 28px; margin-top: 4px; }
}
</style>
```

- [ ] **Step 4: Run** `pnpm test:unit -- tests/unit/dashboard.spec.ts` → PASS. `pnpm lint:fix && pnpm lint && pnpm typecheck`.

- [ ] **Step 5: Commit**
```bash
git add frontend/src/dashboard frontend/src/composables/useQuota.ts frontend/src/components/Sparkline.vue frontend/src/pages/Dashboard.vue frontend/tests/unit/dashboard.spec.ts
git commit -m "feat(ui-sp1): Dashboard (KPIs + 24h sparkline + quota + recent)"
```

---

### Task 12: task mutations (cancel/delete, optimistic)

**Files:** Create `src/composables/useTaskMutations.ts`; Test `tests/unit/taskMutations.spec.ts`

- [ ] **Step 1: Write the failing test** — `frontend/tests/unit/taskMutations.spec.ts`:
```ts
import { describe, expect, test } from 'vitest'
import { canCancel, canDelete } from '@/composables/useTaskMutations'

describe('task action guards', () => {
  test('canCancel: only non-terminal', () => {
    expect(canCancel('downloading')).toBe(true)
    expect(canCancel('pending')).toBe(true)
    expect(canCancel('succeeded')).toBe(false)
    expect(canCancel('failed')).toBe(false)
    expect(canCancel('cancelled')).toBe(false)
  })
  test('canDelete: only terminal', () => {
    expect(canDelete('succeeded')).toBe(true)
    expect(canDelete('failed')).toBe(true)
    expect(canDelete('cancelled')).toBe(true)
    expect(canDelete('downloading')).toBe(false)
  })
})
```

- [ ] **Step 2: Run** `pnpm test:unit -- tests/unit/taskMutations.spec.ts` → FAIL.

- [ ] **Step 3: Implement** `frontend/src/composables/useTaskMutations.ts`:
```ts
import { useMutation, useQueryClient } from '@tanstack/vue-query'
import { client } from '@/api/client'
import { TERMINAL_STATUSES, type TaskStatus } from '@/api/types'

export function canCancel(status: TaskStatus): boolean {
  return !TERMINAL_STATUSES.has(status)
}
export function canDelete(status: TaskStatus): boolean {
  return TERMINAL_STATUSES.has(status)
}

export function useTaskMutations() {
  const qc = useQueryClient()
  const invalidate = () => qc.invalidateQueries({ queryKey: ['tasks'] })

  const cancel = useMutation({
    mutationFn: (id: string) =>
      client.post(`/api/v1/tasks/${id}/cancel`, {}),
    onSettled: invalidate,
  })
  const remove = useMutation({
    mutationFn: (id: string) => client.delete(`/api/v1/tasks/${id}`),
    onSettled: invalidate,
  })
  return { cancel, remove }
}
```

- [ ] **Step 4: Run** `pnpm test:unit -- tests/unit/taskMutations.spec.ts` → PASS. `pnpm lint:fix && pnpm lint && pnpm typecheck`.

- [ ] **Step 5: Commit**
```bash
git add frontend/src/composables/useTaskMutations.ts frontend/tests/unit/taskMutations.spec.ts
git commit -m "feat(ui-sp1): task cancel/delete mutations + guards"
```

---

### Task 13: TaskList upgrade (filter + actions)

**Files:** Create `src/tasks/filter.ts`; Modify `src/pages/TaskList.vue`; Test `tests/unit/taskFilter.spec.ts`

- [ ] **Step 1: Write the failing test** — `frontend/tests/unit/taskFilter.spec.ts`:
```ts
import { describe, expect, test } from 'vitest'
import { filterTasks } from '@/tasks/filter'
import type { TaskRead } from '@/api/types'

const mk = (id: string, repo: string, status: TaskRead['status']): TaskRead => ({
  id, repo_id: repo, revision: 'abc', status, priority: 1,
  created_at: '2026-05-19T00:00:00Z', completed_at: null, error_message: null,
})
const items = [
  mk('aaaa1111', 'org/alpha', 'downloading'),
  mk('bbbb2222', 'org/beta', 'succeeded'),
]

describe('filterTasks', () => {
  test('no filter → all', () => {
    expect(filterTasks(items, { status: '', q: '' })).toHaveLength(2)
  })
  test('status filter', () => {
    expect(filterTasks(items, { status: 'succeeded', q: '' }).map((t) => t.id))
      .toEqual(['bbbb2222'])
  })
  test('q matches repo or id (case-insensitive)', () => {
    expect(filterTasks(items, { status: '', q: 'ALPHA' }).map((t) => t.id))
      .toEqual(['aaaa1111'])
    expect(filterTasks(items, { status: '', q: 'bbbb' }).map((t) => t.id))
      .toEqual(['bbbb2222'])
  })
})
```

- [ ] **Step 2: Run** `pnpm test:unit -- tests/unit/taskFilter.spec.ts` → FAIL.

- [ ] **Step 3: Implement.** Create `frontend/src/tasks/filter.ts`:
```ts
import type { TaskRead } from '@/api/types'

export function filterTasks(
  items: TaskRead[], f: { status: string; q: string },
): TaskRead[] {
  const q = f.q.trim().toLowerCase()
  return items.filter((t) => {
    if (f.status && t.status !== f.status) return false
    if (q && !t.repo_id.toLowerCase().includes(q) &&
        !t.id.toLowerCase().includes(q)) return false
    return true
  })
}
```
Replace `frontend/src/pages/TaskList.vue`:
```vue
<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import DataBoundary from '@/components/DataBoundary.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { useTaskList } from '@/composables/useTaskList'
import { useTaskMutations, canCancel, canDelete } from '@/composables/useTaskMutations'
import { filterTasks } from '@/tasks/filter'
import type { TaskRead } from '@/api/types'

const { t } = useI18n()
const router = useRouter()
const { data, isLoading, isError } = useTaskList()
const { cancel, remove } = useTaskMutations()

const status = ref('')
const q = ref('')
const STATUSES = ['pending', 'scheduling', 'downloading', 'succeeded',
  'failed', 'cancelled']

const rows = computed(() =>
  filterTasks(data.value?.items ?? [], { status: status.value, q: q.value }))

function open(id: string) {
  router.push({ name: 'taskDetail', params: { id } })
}
async function doCancel(row: TaskRead) {
  await ElMessageBox.confirm(t('tasks.cancelConfirm')).catch(() => null)
    .then((ok) => ok && cancel.mutateAsync(row.id)
      .then(() => ElMessage.success(t('tasks.cancelled'))))
}
async function doDelete(row: TaskRead) {
  await ElMessageBox.confirm(t('tasks.deleteConfirm')).catch(() => null)
    .then((ok) => ok && remove.mutateAsync(row.id)
      .then(() => ElMessage.success(t('tasks.deleted'))))
}
function fmt(iso: string) { return new Date(iso).toLocaleString() }
</script>

<template>
  <div class="page-container">
    <div class="bar">
      <h2>{{ t('tasks.listHeading') }}</h2>
      <el-button
        type="primary"
        @click="router.push({ name: 'taskCreate' })"
      >
        {{ t('tasks.create') }}
      </el-button>
    </div>

    <div class="filters">
      <el-select
        v-model="status"
        clearable
        :placeholder="t('tasks.filterStatus')"
        style="width: 160px"
      >
        <el-option
          v-for="s in STATUSES"
          :key="s"
          :label="t(`status.${s}`)"
          :value="s"
        />
      </el-select>
      <el-input
        v-model="q"
        :placeholder="t('tasks.search')"
        style="width: 240px"
        clearable
      />
    </div>

    <DataBoundary
      :loading="isLoading"
      :error="isError"
      :is-empty="rows.length === 0"
      :empty-message="t('tasks.empty')"
    >
      <template #empty-action>
        <el-button
          type="primary"
          @click="router.push({ name: 'taskCreate' })"
        >
          {{ t('tasks.create') }}
        </el-button>
      </template>
      <el-table
        :data="rows"
        stripe
        @row-click="(r: TaskRead) => open(r.id)"
      >
        <el-table-column
          :label="t('tasks.columns.id')"
          width="120"
        >
          <template #default="{ row }">
            {{ row.id.slice(0, 8) }}…
          </template>
        </el-table-column>
        <el-table-column
          prop="repo_id"
          :label="t('tasks.columns.repo')"
          min-width="220"
        />
        <el-table-column
          :label="t('tasks.columns.status')"
          width="120"
        >
          <template #default="{ row }">
            <StatusBadge :status="row.status" />
          </template>
        </el-table-column>
        <el-table-column
          :label="t('tasks.columns.createdAt')"
          width="190"
        >
          <template #default="{ row }">
            {{ fmt(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column
          :label="t('tasks.columns.actions')"
          width="200"
        >
          <template #default="{ row }">
            <el-button
              link
              type="primary"
              @click.stop="open(row.id)"
            >
              {{ t('tasks.view') }}
            </el-button>
            <el-button
              v-if="canCancel(row.status)"
              link
              type="warning"
              @click.stop="doCancel(row)"
            >
              {{ t('tasks.cancel') }}
            </el-button>
            <el-button
              v-if="canDelete(row.status)"
              link
              type="danger"
              @click.stop="doDelete(row)"
            >
              {{ t('tasks.delete') }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </DataBoundary>
  </div>
</template>

<style lang="scss" scoped>
.bar { display: flex; justify-content: space-between; align-items: center; }
.filters { display: flex; gap: var(--dlw-space-3); margin: var(--dlw-space-3) 0; }
</style>
```

- [ ] **Step 4: Run** `pnpm test:unit -- tests/unit/taskFilter.spec.ts` → PASS; `pnpm test:unit` (full) → all PASS. `pnpm lint:fix && pnpm lint && pnpm typecheck`.

- [ ] **Step 5: Commit**
```bash
git add frontend/src/tasks/filter.ts frontend/src/pages/TaskList.vue frontend/tests/unit/taskFilter.spec.ts
git commit -m "feat(ui-sp1): TaskList filter + cancel/delete actions"
```

---

### Task 14: TaskCreate page (+ service-token guard)

**Files:** Create `src/tasks/createValidation.ts`; Modify `src/pages/TaskCreate.vue`; Test `tests/unit/createValidation.spec.ts`

- [ ] **Step 1: Write the failing test** — `frontend/tests/unit/createValidation.spec.ts`:
```ts
import { describe, expect, test } from 'vitest'
import { validateCreate, mapCreateError } from '@/tasks/createValidation'

describe('validateCreate', () => {
  test('valid', () => {
    expect(validateCreate({ repo_id: 'org/m', revision: 'a'.repeat(40),
      storage_id: 1 })).toEqual([])
  })
  test('errors', () => {
    const e = validateCreate({ repo_id: 'bad', revision: 'xyz', storage_id: 0 })
    expect(e).toContain('repoPattern')
    expect(e).toContain('revPattern')
    expect(e).toContain('storageRequired')
  })
})
describe('mapCreateError', () => {
  test('http status → i18n key', () => {
    expect(mapCreateError(409)).toBe('errors.conflict')
    expect(mapCreateError(422)).toBe('errors.validation')
    expect(mapCreateError(429)).toBe('errors.quota_exceeded')
    expect(mapCreateError(503)).toBe('errors.service_unavailable')
    expect(mapCreateError(500)).toBe('errors.service_unavailable')
  })
})
```

- [ ] **Step 2: Run** `pnpm test:unit -- tests/unit/createValidation.spec.ts` → FAIL.

- [ ] **Step 3: Implement.** Create `frontend/src/tasks/createValidation.ts`:
```ts
import type { TaskCreateBody } from '@/api/types'

const REPO_RE = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/
const SHA_RE = /^[0-9a-f]{40}$/

export function validateCreate(b: Partial<TaskCreateBody>): string[] {
  const e: string[] = []
  if (!b.repo_id) e.push('repoRequired')
  else if (!REPO_RE.test(b.repo_id)) e.push('repoPattern')
  if (!b.revision) e.push('revRequired')
  else if (!SHA_RE.test(b.revision)) e.push('revPattern')
  if (!b.storage_id || b.storage_id <= 0) e.push('storageRequired')
  return e
}

export function mapCreateError(status: number | undefined): string {
  if (status === 409) return 'errors.conflict'
  if (status === 422) return 'errors.validation'
  if (status === 429) return 'errors.quota_exceeded'
  return 'errors.service_unavailable'
}
```
Replace `frontend/src/pages/TaskCreate.vue`:
```vue
<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { client } from '@/api/client'
import { useSessionStore } from '@/stores/session'
import { validateCreate, mapCreateError } from '@/tasks/createValidation'
import type { TaskCreateBody, TaskRead } from '@/api/types'

const { t } = useI18n()
const router = useRouter()
const session = useSessionStore()
const submitting = ref(false)

const form = reactive<TaskCreateBody>({
  repo_id: '', revision: '', storage_id: 1, priority: 1,
  source_strategy: 'auto_balance', trust_non_hf_sha256: false,
  upgrade_from_revision: null,
})

async function submit() {
  const errs = validateCreate(form)
  if (errs.length) {
    ElMessage.error(t(`create.${errs[0]}`))
    return
  }
  submitting.value = true
  try {
    const body: TaskCreateBody = { ...form }
    if (!body.upgrade_from_revision) delete body.upgrade_from_revision
    const r = await client.post<TaskRead>('/api/v1/tasks', body)
    ElMessage.success(t('create.success'))
    router.push({ name: 'taskDetail', params: { id: r.data.id } })
  } catch (e) {
    const status = (e as { response?: { status?: number } })?.response?.status
    ElMessage.error(t(mapCreateError(status)))
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="page-container">
    <h2>{{ t('create.heading') }}</h2>

    <el-alert
      v-if="session.isServiceToken"
      type="warning"
      :closable="false"
      :title="t('create.serviceTokenWarn')"
      style="margin-bottom: 16px"
    />

    <el-form
      label-position="top"
      style="max-width: 560px"
    >
      <el-form-item :label="t('create.repo')">
        <el-input
          v-model="form.repo_id"
          placeholder="org/model"
        />
      </el-form-item>
      <el-form-item :label="t('create.revision')">
        <el-input
          v-model="form.revision"
          placeholder="40-hex sha"
        />
      </el-form-item>
      <el-form-item :label="t('create.storageId')">
        <el-input-number
          v-model="form.storage_id"
          :min="1"
        />
      </el-form-item>
      <el-form-item :label="t('create.priority')">
        <el-input-number
          v-model="form.priority"
          :min="0"
          :max="10"
        />
      </el-form-item>
      <el-form-item :label="t('create.strategy')">
        <el-select v-model="form.source_strategy">
          <el-option
            v-for="s in ['auto_balance', 'fastest_only', 'pin_huggingface']"
            :key="s"
            :label="s"
            :value="s"
          />
        </el-select>
      </el-form-item>
      <el-form-item :label="t('create.upgradeFrom')">
        <el-input
          v-model="form.upgrade_from_revision as string"
          placeholder="(optional) 40-hex sha"
        />
      </el-form-item>
      <el-form-item>
        <el-switch v-model="form.trust_non_hf_sha256" />
        <span style="margin-left: 8px">{{ t('create.trustNonHf') }}</span>
      </el-form-item>
      <el-form-item>
        <el-button
          type="primary"
          :loading="submitting"
          :disabled="session.isServiceToken"
          @click="submit"
        >
          {{ t('create.submit') }}
        </el-button>
      </el-form-item>
    </el-form>
  </div>
</template>
```

- [ ] **Step 4: Run** `pnpm test:unit -- tests/unit/createValidation.spec.ts` → PASS. `pnpm lint:fix && pnpm lint && pnpm typecheck`.

- [ ] **Step 5: Commit**
```bash
git add frontend/src/tasks/createValidation.ts frontend/src/pages/TaskCreate.vue frontend/tests/unit/createValidation.spec.ts
git commit -m "feat(ui-sp1): TaskCreate form + service-token preflight guard"
```

---

### Task 15: Login + OIDC button

**Files:** Modify `src/pages/Login.vue`; Test `tests/unit/oidc.spec.ts`

- [ ] **Step 1: Write the failing test** — `frontend/tests/unit/oidc.spec.ts`:
```ts
import { describe, expect, test } from 'vitest'
import { oidcLoginUrl } from '@/pages/oidc'

describe('oidcLoginUrl', () => {
  test('uses VITE_API_BASE when set', () => {
    expect(oidcLoginUrl('http://c:8001')).toBe('http://c:8001/api/v1/auth/login')
  })
  test('relative when base empty (vite proxy)', () => {
    expect(oidcLoginUrl('')).toBe('/api/v1/auth/login')
    expect(oidcLoginUrl(undefined)).toBe('/api/v1/auth/login')
  })
})
```

- [ ] **Step 2: Run** `pnpm test:unit -- tests/unit/oidc.spec.ts` → FAIL.

- [ ] **Step 3: Implement.** Create `frontend/src/pages/oidc.ts`:
```ts
export function oidcLoginUrl(base: string | undefined): string {
  return `${base ?? ''}/api/v1/auth/login`
}
```
Modify `frontend/src/pages/Login.vue` — add OIDC button + handler (keep everything else; add to `<script setup>` and template):
```ts
// add near the other imports
import { oidcLoginUrl } from '@/pages/oidc'

function loginOidc() {
  window.location.assign(oidcLoginUrl(import.meta.env.VITE_API_BASE))
}
```
Add to the template, right after the existing submit `el-form-item`:
```vue
        <el-form-item>
          <el-button
            link
            @click="loginOidc"
          >
            {{ t('login.oidc') }}
          </el-button>
        </el-form-item>
```
Also update `tokenPlaceholder` usage already keyed (`login.tokenPlaceholder` now "粘贴租户用户 JWT" / "Paste a tenant-user JWT" from Task 1) — no code change, just confirm the existing `:placeholder="t('login.tokenPlaceholder')"` still resolves (it does).

- [ ] **Step 4: Run** `pnpm test:unit -- tests/unit/oidc.spec.ts` → PASS; `pnpm test:unit` full → all PASS. `pnpm lint:fix && pnpm lint && pnpm typecheck`.

- [ ] **Step 5: Commit**
```bash
git add frontend/src/pages/oidc.ts frontend/src/pages/Login.vue frontend/tests/unit/oidc.spec.ts
git commit -m "feat(ui-sp1): Login OIDC button"
```

---

# Milestone M4 — Wire-up, verify, docs, PR

### Task 16: Full frontend CI gates green + cleanup

**Files:** Modify (cleanup) — remove now-unused `src/components/AppLayout.vue` if nothing imports it; `src/composables/*` consistency.

- [ ] **Step 1:** From `frontend/`: `grep -rl AppLayout src` — if only its own file, `git rm frontend/src/components/AppLayout.vue` (App.vue now uses AppShell). If still referenced, leave it.
- [ ] **Step 2:** Run the **exact CI gates** (must all pass):
```bash
cd frontend
pnpm lint
pnpm typecheck
pnpm test:unit
pnpm build
```
Expected: eslint 0 warnings/errors; vue-tsc no errors; all vitest specs pass; `dist/` built.
- [ ] **Step 3:** If lint fails on Vue templates (multi-attr formatting from the project's known lesson), run `pnpm lint:fix`, re-run gates, and **amend the relevant prior commit is NOT allowed — make a new fixup commit**:
```bash
git add -A frontend
git commit -m "chore(ui-sp1): lint:fix + remove unused AppLayout + CI gates green"
```
- [ ] **Step 4:** Confirm additive-only: `git diff --stat origin/main...HEAD` touches only `frontend/**` and `docs/superpowers/**`. No `src/dlw`, `api/openapi.yaml`, `alembic`, `tools`.

---

### Task 17: Headed Playwright smoke + operator doc + PR (controller-run)

**Files:** Create `.run/pw/ui-sp1.mjs` (not committed — `.run/` gitignored); Create `docs/operator/web-ui.md`

- [ ] **Step 1 (controller):** Ensure stack up: PG, minio, controller `:8001` (plain HTTP, browser path), Vite dev (`cd frontend && pnpm dev`, proxies `/api`→`:8001`). Mint a 30-day tenant JWT:
```bash
uv run python -c "from dlw.auth.principal import issue_system_jwt; print(issue_system_jwt(secret='dev-system-jwt-change-me', user_id=1, tenant_id=1, role='tenant_admin', project_ids=[], ttl_seconds=2592000))"
```
- [ ] **Step 2:** Write `.run/pw/ui-sp1.mjs` (headed; reuse `.run/pw/node_modules`):
```js
import { chromium } from 'playwright';
const UI='http://localhost:5173', TOK=process.env.PW_TOKEN, D='D:/download_weights/.run';
const b=await chromium.launch({headless:false,slowMo:700});
const p=await (await b.newContext({viewport:{width:1380,height:900}})).newPage();
try{
  await p.goto(UI+'/',{waitUntil:'networkidle'}); await p.waitForTimeout(1200);
  await p.screenshot({path:`${D}/sp1-01-login.png`});
  await p.locator('input:visible,textarea:visible').first().fill(TOK);
  await p.locator('button.el-button--primary,button[type=submit]').first().click().catch(()=>{});
  await p.waitForTimeout(2000);
  await p.screenshot({path:`${D}/sp1-02-dashboard.png`});            // shell + dashboard
  await p.goto(UI+'/tasks/new',{waitUntil:'networkidle'}); await p.waitForTimeout(1000);
  await p.locator('input').nth(0).fill('hf-internal-testing/tiny-random-bert');
  await p.locator('input').nth(1).fill('main'.padEnd(40,'0').replace(/[^0-9a-f]/g,'0'));
  await p.screenshot({path:`${D}/sp1-03-create.png`});
  await p.goto(UI+'/tasks',{waitUntil:'networkidle'}); await p.waitForTimeout(2000);
  await p.screenshot({path:`${D}/sp1-04-list.png`});
  console.log('SP1 smoke screenshots written'); await p.waitForTimeout(8000);
} finally { await b.close(); }
```
Run: `export PW_TOKEN=<jwt> && node /d/download_weights/.run/pw/ui-sp1.mjs`. Controller verifies shell renders, login works, dashboard/create/list pages load (screenshots `.run/sp1-0*.png`). This is a manual milestone check (not CI), mirroring the project's validated headed-Playwright habit.
- [ ] **Step 3:** Create `docs/operator/web-ui.md` (~70 lines): what UI-SP1 ships (shell + auth + dashboard + tasks list + create), how to run it (Vite dev → `:8001` proxy; the tenant-user-JWT-not-admin-token rule; dark/locale), the decomposition note (UI-SP2..SP5 deferred, what each adds + their backend deps), and a "known scope" list (no executors/search/quota-mgmt/audit/settings/copilot/realtime in SP1; task detail still the simple scaffold view until UI-SP2). Cross-ref the spec + `docs/getting-started.md`.
```bash
git add docs/operator/web-ui.md
git commit -m "docs(ui-sp1): web UI operator guide + decomposition note"
```
- [ ] **Step 4 (controller):** Final opus whole-impl review of `git diff origin/main...HEAD` (new public UI surface + shell wiring — the SP1-style "production wiring tests bypass" risk applies: AppShell/router/main.ts boot path). Address CRITICAL/HIGH; record-and-accept safe HIGHs.
- [ ] **Step 5 (controller):** `git push -u origin feat/ui-sp1-shell-tasks` → `gh pr create --title "UI-SP1 — App Shell + Auth + Tasks" --body "<frontend-only; shell+dashboard+task list+create; reuses scaffold; useLiveResource seam; no new dep; CI frontend-lint/build green; decomposition: UI-SP2..SP5 deferred>"` → `gh pr checks --watch` → squash-merge `--delete-branch` → sync main → update memory (reference_l17728_modelpull.md: UI decomposition + UI-SP1 merged; feedback_subagent_driven_dev.md: frontend-sub-project learnings).

---

## Self-Review

**Spec coverage:** decomposition (spec §2) → header + Task 17 doc; UI-SP1 goal/§3 frontend-only → all tasks `frontend/**` + Task 16 Step 4 assert; §4 backend surface → only those endpoints used (Task 5/11/12/13/14: tasks list/detail/cancel/delete, quota, create; Task 15 auth/login); §5 components → Tasks 2-15 one-file-one-responsibility; tokens/dark (T2/T3), session+isServiceToken (T4), useLiveResource single seam (T5, consumed everywhere), DataBoundary (T6), nav registry (T7), AppShell (T8), CommandPalette (T9), routes (T10), Dashboard client-agg (T11), TaskList filter+actions (T12/T13), TaskCreate + service-token guard (T14), Login OIDC (T15); §6 data flow/optimistic (T12); §7 testing (Vitest pure-fn + mount; headed Playwright T17; CI gates T16); §8 decisions (el-table not v2, no ECharts/sparkline, token-paste+OIDC, client-agg dashboard, tenant chip read-only, additive-only) all reflected. No spec requirement unmapped.

**Placeholder scan:** every code step has complete runnable code. The only intentional staged stubs are `CommandPalette.vue` (minimal in T8 → real in T9) and `Dashboard.vue`/`TaskCreate.vue` (empty in T10 so `vue-tsc` resolves lazy route imports → real in T11/T14), each with exact replacement code in the later task and the reason stated inline. No "TODO/add error handling/similar to".

**Type/name consistency:** `Principal`/`QuotaCurrent`/`TaskCreateBody` defined T4 (`api/types.ts`) used T5/T11/T12/T14 identically; `useLiveResource(key, fetcher, {baseIntervalMs,isTerminal,staleTime})` + pure `computeInterval({base,terminal,hidden,errored})` consistent T5↔T11↔T12; `decodePrincipal`/`useSessionStore.{principal,role,isServiceToken}` consistent T4↔T8↔T9↔T14; `visibleNav(role,items?)`/`NAV_ITEMS` T7↔T8↔T9; `buildCommands(role,t)`/`Command` T9; `validateCreate`/`mapCreateError` T14; `filterTasks` T13; `canCancel`/`canDelete` T12↔T13; `aggregateKpis`/`bucket24h` T11; route names (`dashboard/taskList/taskCreate/taskDetail/login`) consistent T10↔T7↔T8↔T9↔T13. i18n keys used in code all added to both locale files in T1 (parity test enforces). `ui` store `theme/locale/sidebarCollapsed/toggleTheme/toggleSidebar/setLocale/hydrate` consistent T3↔T8↔main.ts. Frontend-only invariant asserted in T16 Step 4.
