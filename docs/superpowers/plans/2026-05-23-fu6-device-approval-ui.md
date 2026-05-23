# FU6 follow-on — Browser device-approval page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development / executing-plans. Checkbox steps.

**Goal:** New `/device` Vue page so a human can approve/deny RFC 8628 device codes from the browser, completing the FU6 flow.

**Spec:** `docs/superpowers/specs/2026-05-23-fu6-device-approval-ui-design.md`

**Locked constraints:**
- Zero backend / migration / openapi / executor change (endpoint `POST /api/v1/auth/device/approve` already exists from FU6).
- Zero new runtime dep.
- Both `en-US.json` and `zh-CN.json` must add the same `device.*` key set (project parity rule).
- `redirect` query param must be validated as same-origin path before honoring (open-redirect defense): `startsWith("/") && !startsWith("//")`.
- Service-token detection on client side: `useSessionStore().isServiceToken` — disable Approve button + alert.
- Lint gates (must remain green): `frontend-lint` (eslint `--max-warnings=0` + `vue-tsc`), `frontend-build`, `vitest run`.

---

## File Structure

- **Create** `frontend/src/pages/Device.vue` — the approval page.
- **Modify** `frontend/src/router/index.ts` — add `/device` route + guard preserves `redirect`.
- **Modify** `frontend/src/pages/Login.vue` — honor validated `?redirect=`.
- **Modify** `frontend/src/locale/en-US.json` + `frontend/src/locale/zh-CN.json` — add `device.*`.
- **Create** `frontend/tests/unit/Device.spec.ts` — page tests.
- **Modify** `frontend/tests/unit/auth.spec.ts` — extend with redirect behavior.

---

## Milestone M1 — Router guard + Login redirect support

### Task 1: router guard `redirect` + Login.vue same-origin guard

**Files:** `frontend/src/router/index.ts`, `frontend/src/pages/Login.vue`, `frontend/tests/unit/auth.spec.ts`.

- [ ] **Step 1 (read existing auth.spec.ts):** open `frontend/tests/unit/auth.spec.ts` to see the current test conventions (vue-router setup, mounting Login.vue, mocking auth store). Add the new redirect tests in the same style — do not invent new test scaffolding.

- [ ] **Step 2 (failing tests):** in `frontend/tests/unit/auth.spec.ts`, append (adapt imports to match existing file conventions):

  ```ts
  describe('redirect after login', () => {
    test('router guard adds redirect query for protected routes', async () => {
      // Construct router exactly like the existing tests in this file.
      // Navigate to /executors while unauthenticated.
      // Assert the resolved target is /login with query.redirect === '/executors'.
    })

    test('Login.vue honors same-origin redirect query after login', async () => {
      // Mount Login.vue with route.query.redirect = '/device?user_code=ABCD-1234'.
      // Submit a valid token.
      // Assert router.replace was called with '/device?user_code=ABCD-1234'.
    })

    test('Login.vue rejects external redirect and falls back to /', async () => {
      // Mount with route.query.redirect = 'https://attacker.example/'.
      // Submit a valid token.
      // Assert router.replace was called with '/' (NOT the external URL).
    })

    test('Login.vue rejects protocol-relative // redirect and falls back to /', async () => {
      // route.query.redirect = '//attacker.example/'.
      // Same: falls back to '/'.
    })
  })
  ```

  Use the same vue-router / vue-i18n / Element Plus mounting helpers the existing tests use. Read the file first; do not duplicate setup.

- [ ] **Step 3: verify FAIL** — `cd "D:/download_weights/frontend" && pnpm vitest run tests/unit/auth.spec.ts` — new tests FAIL.

- [ ] **Step 4 (router guard):** in `frontend/src/router/index.ts`, change:
  ```ts
  if (!auth.isAuthenticated) return { path: '/login' }
  ```
  to:
  ```ts
  if (!auth.isAuthenticated) return { path: '/login', query: { redirect: to.fullPath } }
  ```

- [ ] **Step 5 (Login.vue):** in `frontend/src/pages/Login.vue`, replace `router.push('/')` on successful login. The current `onSubmit` ends with:
  ```ts
  authStore.login(form.token.trim())
  router.push('/')
  ```
  Change to (introduce a helper for clarity):
  ```ts
  function safeRedirect(raw: unknown): string {
    if (typeof raw !== 'string') return '/'
    // Same-origin path only: must start with '/' and NOT '//' (protocol-relative URL).
    if (!raw.startsWith('/') || raw.startsWith('//')) return '/'
    return raw
  }

  // … in onSubmit:
  authStore.login(form.token.trim())
  router.replace(safeRedirect(route.query.redirect))
  ```
  (`route` is already imported via `useRoute()`.)

  Also update the `onMounted` block: when already authenticated, redirect to the safe target too:
  ```ts
  if (authStore.isAuthenticated) {
    router.replace(safeRedirect(route.query.redirect))
  }
  ```

- [ ] **Step 6: verify PASS** — `cd "D:/download_weights/frontend" && pnpm vitest run tests/unit/auth.spec.ts` — all auth tests pass.

- [ ] **Step 7 (frontend gate):** `cd "D:/download_weights/frontend" && pnpm lint && pnpm typecheck && pnpm vitest run && pnpm build` — all green.

- [ ] **Step 8: commit:**
  ```bash
  cd "D:/download_weights"
  git add frontend/src/router/index.ts frontend/src/pages/Login.vue frontend/tests/unit/auth.spec.ts
  git commit -m "feat(fu6-ui): router redirect-after-login (same-origin) + Login.vue honor"
  ```

---

## Milestone M2 — Device.vue page + i18n + tests

### Task 2: Device.vue + i18n keys + tests

**Files:** `frontend/src/pages/Device.vue` (new), `frontend/src/router/index.ts` (route), `frontend/src/locale/en-US.json`, `frontend/src/locale/zh-CN.json`, `frontend/tests/unit/Device.spec.ts` (new).

- [ ] **Step 1 (i18n keys):** add the `device` block to BOTH `frontend/src/locale/en-US.json` and `frontend/src/locale/zh-CN.json` at exact parity. Place it after the `login` block.

  EN:
  ```json
  "device": {
    "heading": "Confirm device",
    "intro": "A command-line tool is requesting access to your account. Confirm the code below to continue.",
    "principalLabel": "Signing in as",
    "codeLabel": "Device code",
    "codePlaceholder": "XXXX-XXXX",
    "codeRequired": "Device code is required",
    "approve": "Approve",
    "deny": "Deny",
    "successApproved": "Device approved. You can close this tab and return to the CLI.",
    "successDenied": "Device denied. The CLI session will receive an access_denied error.",
    "errorInvalid": "Invalid or expired device code. Restart 'dlw login' to get a new one.",
    "errorService": "Service tokens cannot approve a device. Sign in as a real user.",
    "errorGeneric": "Approval failed. Please try again.",
    "serviceWarning": "You are signed in with a service token. Sign in as a regular user to approve a device."
  },
  ```

  ZH:
  ```json
  "device": {
    "heading": "确认设备",
    "intro": "一个命令行工具请求访问你的账号。请确认下方的设备代码以继续。",
    "principalLabel": "当前身份",
    "codeLabel": "设备代码",
    "codePlaceholder": "XXXX-XXXX",
    "codeRequired": "请输入设备代码",
    "approve": "批准",
    "deny": "拒绝",
    "successApproved": "设备已批准。可以关闭此标签页，返回 CLI。",
    "successDenied": "设备已拒绝。CLI 将收到 access_denied 错误。",
    "errorInvalid": "设备代码无效或已过期。请重新运行 'dlw login' 获取新代码。",
    "errorService": "服务 token 不能批准设备。请使用普通用户身份登录后再批准。",
    "errorGeneric": "批准失败，请重试。",
    "serviceWarning": "你目前以服务 token 身份登录，无法批准设备。请改用普通用户身份登录。"
  },
  ```

  **Verify parity**: every key in `device.*` is present in BOTH files; no extra keys in one file. Manual diff after edit.

- [ ] **Step 2 (failing tests):** create `frontend/tests/unit/Device.spec.ts`. Read `frontend/tests/unit/Login.vue.spec.ts` first (or another page spec like `frontend/tests/unit/QuotaPage.spec.ts` / `Settings.spec.ts`) to copy the exact mount pattern (Element Plus + i18n + Pinia + vue-router mocks).

  The test set:
  ```ts
  import { describe, test, expect, vi, beforeEach } from 'vitest'
  import { flushPromises, mount } from '@vue/test-utils'
  import { createPinia, setActivePinia } from 'pinia'
  import { createI18n } from 'vue-i18n'
  import ElementPlus from 'element-plus'
  import { createRouter, createWebHistory } from 'vue-router'

  // Mock axios client used by the page. Adapt to however other page specs mock it.
  vi.mock('@/api/client', () => ({
    client: { post: vi.fn() },
  }))
  import { client } from '@/api/client'
  import Device from '@/pages/Device.vue'
  import enUS from '@/locale/en-US.json'

  function makeRouter(initialUrl: string) {
    const r = createRouter({
      history: createWebHistory(),
      routes: [
        { path: '/device', name: 'device', component: Device },
        { path: '/login', name: 'login', component: { template: '<div/>' } },
      ],
    })
    r.push(initialUrl)
    return r
  }

  function mountPage(initialUrl = '/device?user_code=ABCD-1234') {
    const i18n = createI18n({
      legacy: false, locale: 'en-US', messages: { 'en-US': enUS as any },
    })
    const router = makeRouter(initialUrl)
    return mount(Device, {
      global: { plugins: [ElementPlus, i18n, router, createPinia()] },
    })
  }

  describe('Device.vue', () => {
    beforeEach(() => {
      setActivePinia(createPinia())
      // Seed an authenticated, non-service token (real user_id != 0).
      // Use the auth store API or localStorage as the other specs do.
      // e.g. localStorage.setItem('dlw_token', <some JWT with sub=42, role='tenant_operator'>)
      vi.clearAllMocks()
    })

    test('pre-fills user_code from ?user_code= query', async () => {
      const w = mountPage('/device?user_code=ABCD-1234')
      await flushPromises()
      const input = w.find('input')
      expect((input.element as HTMLInputElement).value).toBe('ABCD-1234')
    })

    test('Approve posts to /auth/device/approve and shows success', async () => {
      vi.mocked(client.post).mockResolvedValueOnce({ data: { status: 'approved' } })
      const w = mountPage('/device?user_code=ABCD-1234')
      await flushPromises()
      // Click the Approve button (text === 'Approve').
      const buttons = w.findAll('button').filter((b) => b.text() === 'Approve')
      await buttons[0].trigger('click')
      await flushPromises()
      expect(client.post).toHaveBeenCalledWith(
        '/api/v1/auth/device/approve',
        { user_code: 'ABCD-1234', action: 'approve' },
      )
      expect(w.text()).toContain('Device approved')
    })

    test('Deny posts with action="deny"', async () => {
      vi.mocked(client.post).mockResolvedValueOnce({ data: { status: 'denied' } })
      const w = mountPage()
      await flushPromises()
      const buttons = w.findAll('button').filter((b) => b.text() === 'Deny')
      await buttons[0].trigger('click')
      await flushPromises()
      expect(client.post).toHaveBeenCalledWith(
        '/api/v1/auth/device/approve',
        { user_code: 'ABCD-1234', action: 'deny' },
      )
      expect(w.text()).toContain('Device denied')
    })

    test('404 DEVICE_CODE_INVALID shows invalid-code error', async () => {
      vi.mocked(client.post).mockRejectedValueOnce({
        response: { status: 404, data: { detail: { code: 'DEVICE_CODE_INVALID' } } },
      })
      const w = mountPage()
      await flushPromises()
      const buttons = w.findAll('button').filter((b) => b.text() === 'Approve')
      await buttons[0].trigger('click')
      await flushPromises()
      expect(w.text()).toContain('Invalid or expired device code')
    })

    test('403 SERVICE_CANNOT_APPROVE shows service-token error', async () => {
      vi.mocked(client.post).mockRejectedValueOnce({
        response: { status: 403, data: { detail: { code: 'SERVICE_CANNOT_APPROVE' } } },
      })
      const w = mountPage()
      await flushPromises()
      const buttons = w.findAll('button').filter((b) => b.text() === 'Approve')
      await buttons[0].trigger('click')
      await flushPromises()
      expect(w.text()).toContain('Service tokens cannot approve')
    })

    test('service token: Approve disabled + warning alert', async () => {
      // Seed a service-token JWT (sub=0). The exact JWT must decode to userId=0
      // per stores/session.ts decodePrincipal.
      // Refer to how other specs seed service-token state — likely auth.spec.ts.
      // … mount, await flushPromises, assert:
      // - el-alert with serviceWarning text visible
      // - Approve button has disabled attribute
    })
  })
  ```

  **Service-token test scaffolding note**: read `frontend/tests/unit/auth.spec.ts` for the exact pattern to seed a `sub=0` JWT into `localStorage` (or use the auth store directly). Mirror its idiom; do not invent.

- [ ] **Step 3: verify FAIL** — `cd "D:/download_weights/frontend" && pnpm vitest run tests/unit/Device.spec.ts` — FAILs (Device.vue doesn't exist yet).

- [ ] **Step 4 (Device.vue):** create `frontend/src/pages/Device.vue`. Mirror `Login.vue`'s SFC structure (`<script setup lang="ts">`, `el-card`, `el-form`, `<style lang="scss" scoped>`).

  ```vue
  <script setup lang="ts">
  import { computed, onMounted, ref } from 'vue'
  import { useRoute } from 'vue-router'
  import { useI18n } from 'vue-i18n'
  import { ElMessage } from 'element-plus'

  import { client } from '@/api/client'
  import { useSessionStore } from '@/stores/session'

  const { t } = useI18n()
  const route = useRoute()
  const session = useSessionStore()

  const userCode = ref('')
  const status = ref<'idle' | 'submitting' | 'approved' | 'denied'>('idle')
  const errorMsg = ref<string | null>(null)

  const isServiceToken = computed(() => session.isServiceToken)
  const principal = computed(() => session.principal)

  onMounted(() => {
    const q = route.query.user_code
    if (typeof q === 'string') userCode.value = q
  })

  function mapError(err: unknown): string {
    const e = err as { response?: { status?: number; data?: { detail?: { code?: string } } } }
    const status = e?.response?.status
    const code = e?.response?.data?.detail?.code
    if (status === 404 && code === 'DEVICE_CODE_INVALID') return t('device.errorInvalid')
    if (status === 403 && code === 'SERVICE_CANNOT_APPROVE') return t('device.errorService')
    return t('device.errorGeneric')
  }

  async function submit(action: 'approve' | 'deny') {
    if (!userCode.value.trim()) {
      ElMessage.error(t('device.codeRequired'))
      return
    }
    status.value = 'submitting'
    errorMsg.value = null
    try {
      await client.post('/api/v1/auth/device/approve', {
        user_code: userCode.value.trim(),
        action,
      })
      status.value = action === 'approve' ? 'approved' : 'denied'
    } catch (err) {
      errorMsg.value = mapError(err)
      status.value = 'idle'
    }
  }
  </script>

  <template>
    <div class="device-page">
      <el-card class="device-card">
        <template #header>
          <h2>{{ t('device.heading') }}</h2>
        </template>

        <p class="intro">{{ t('device.intro') }}</p>

        <div v-if="principal" class="principal">
          <strong>{{ t('device.principalLabel') }}:</strong>
          user #{{ principal.userId }} ({{ principal.role }})
        </div>

        <el-alert
          v-if="isServiceToken"
          type="warning"
          show-icon
          :closable="false"
          class="service-warning"
        >
          {{ t('device.serviceWarning') }}
        </el-alert>

        <el-alert
          v-if="errorMsg"
          type="error"
          show-icon
          :closable="false"
          class="error-alert"
        >
          {{ errorMsg }}
        </el-alert>

        <template v-if="status === 'idle' || status === 'submitting'">
          <el-form label-position="top" @submit.prevent="submit('approve')">
            <el-form-item :label="t('device.codeLabel')">
              <el-input
                v-model="userCode"
                :placeholder="t('device.codePlaceholder')"
                autocomplete="off"
              />
            </el-form-item>
            <el-form-item>
              <el-button
                type="primary"
                :disabled="isServiceToken || status === 'submitting'"
                @click="submit('approve')"
              >
                {{ t('device.approve') }}
              </el-button>
              <el-button
                :disabled="status === 'submitting'"
                @click="submit('deny')"
              >
                {{ t('device.deny') }}
              </el-button>
            </el-form-item>
          </el-form>
        </template>

        <el-alert
          v-else-if="status === 'approved'"
          type="success"
          show-icon
          :closable="false"
        >
          {{ t('device.successApproved') }}
        </el-alert>

        <el-alert
          v-else-if="status === 'denied'"
          type="info"
          show-icon
          :closable="false"
        >
          {{ t('device.successDenied') }}
        </el-alert>
      </el-card>
    </div>
  </template>

  <style lang="scss" scoped>
  .device-page {
    display: flex;
    justify-content: center;
    padding-top: 64px;

    .device-card {
      width: 560px;

      h2 { margin: 0; font-size: 18px; }
      .intro { margin: 0 0 16px; color: var(--el-text-color-secondary); }
      .principal { margin-bottom: 16px; font-family: var(--el-font-family); }
      .service-warning, .error-alert { margin-bottom: 16px; }
    }
  }
  </style>
  ```

- [ ] **Step 5 (route registration):** add the new route to `frontend/src/router/index.ts` in `routes`, after `/settings`:
  ```ts
  {
    path: '/device', name: 'device',
    component: () => import('@/pages/Device.vue'),
  },
  ```

- [ ] **Step 6: verify PASS** — `cd "D:/download_weights/frontend" && pnpm vitest run tests/unit/Device.spec.ts` — all pass.

- [ ] **Step 7 (full frontend gate):** `cd "D:/download_weights/frontend" && pnpm lint && pnpm typecheck && pnpm vitest run && pnpm build` — all green.

- [ ] **Step 8: commit:**
  ```bash
  cd "D:/download_weights"
  git add frontend/src/pages/Device.vue frontend/src/router/index.ts frontend/src/locale/en-US.json frontend/src/locale/zh-CN.json frontend/tests/unit/Device.spec.ts
  git commit -m "feat(fu6-ui): browser device-approval page (/device)"
  ```

### Task 3: M2 full gate
- [ ] Full pytest unchanged but run anyway: `cd "D:/download_weights" && uv run pytest -q` — all pass (no backend change so should be identical to main). `uv run python tools/lint_invariants.py --strict` OK. No commit.

---

## Self-Review

- **Spec coverage:** §0 route + guard redirect → Task 1 ✓; §0 Device.vue page → Task 2 ✓; §0 service-token detection → Step 4 `:disabled="isServiceToken"` + `el-alert` ✓; §0 error mapping → `mapError` covers 404/403/other ✓; §1 open-redirect defense → `safeRedirect` validates `startsWith("/") && !startsWith("//")` ✓; §2 tests 6 page tests + 4 auth tests ✓.
- **Placeholder scan:** all code blocks concrete. The "Read X first" notes are TDD pattern guidance (the implementer must read existing test scaffolding to match conventions exactly), NOT TODO placeholders.
- **Type consistency:** `userCode: Ref<string>`; `status: Ref<'idle'|'submitting'|'approved'|'denied'>`; `errorMsg: Ref<string|null>`; `mapError(unknown) => string`; `safeRedirect(unknown) => string`.
- **Open risks for reviewers:** (a) `route.query.redirect` could be `string[]` (vue-router types it as `string | string[] | null`) — `safeRedirect` only checks `typeof === 'string'`, arrays fall back to `/` (correct). (b) `safeRedirect` allows any path starting with `/` (including `/admin`) — this is intentional; any logged-in user can land anywhere they could navigate to manually. (c) Service-token check is client-side hint; backend's 403 is authoritative. (d) The page does not auto-refresh principal when user changes session — operator must reload page after switching identity (acceptable for v1).
