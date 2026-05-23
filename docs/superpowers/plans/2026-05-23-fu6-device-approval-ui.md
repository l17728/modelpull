# FU6 follow-on — Browser device-approval page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development / executing-plans. Checkbox steps.

**Goal:** New `/device` Vue page so a human can approve/deny RFC 8628 device codes from the browser, completing the FU6 flow.

**Spec:** `docs/superpowers/specs/2026-05-23-fu6-device-approval-ui-design.md`

**Locked constraints:**
- Zero backend / migration / openapi / executor change (endpoint `POST /api/v1/auth/device/approve` already exists from FU6).
- Zero new runtime dep.
- Both `en-US.json` and `zh-CN.json` add the same `device.*` key set (project parity rule).
- `redirect` query param must be validated as same-origin path before honoring (open-redirect defense): `startsWith("/") && !startsWith("//")` AND `!== "/login"` AND `!startsWith("/login")` (loop defense).
- Service-token detection on client side: `useSessionStore().isServiceToken` — disable Approve button + alert.
- Test scripts: `pnpm test:unit` (full vitest), `pnpm exec vitest run <path>` (targeted). NOT `pnpm vitest run` (not a script).
- Lint gates (must remain green): `pnpm lint && pnpm typecheck && pnpm test:unit && pnpm build`.
- Test patterns (verified from existing specs):
  - **`tok()` JWT helper**: `frontend/tests/unit/session.spec.ts` lines 6-11.
  - **`b64()` + `useAuthStore().login(...)` JWT seeding**: `frontend/tests/unit/TaskCreate.spec.ts` lines 20, 26-27, 37.
  - **`vi.hoisted` + `vi.mock('@/api/client', ...)`** anti-TDZ pattern: `TaskCreate.spec.ts` lines 9-12.
  - **`vue-router` mock**: `AppShell.spec.ts` lines 11-19 (need to also mock `replace` for our redirect tests; AppShell only mocks `push`).
  - **`findAll('button').find(b => b.text() === KEY)` then `!.trigger()`**: `AuditPage.spec.ts` lines 83-89.
  - **`setActivePinia(createPinia())` in beforeEach ONLY** — never pass `createPinia()` as a plugin (would create a second store instance disconnected from `useAuthStore().login()` seed).
- `noUncheckedIndexedAccess: true` is on (`tsconfig.json:5`) — array index access returns `T | undefined`; use `!.trigger()` after asserting `expect(btn).toBeTruthy()`.
- Real router in tests must `await router.isReady()` after `router.push(initialUrl)` or `route.query` will be empty in `onMounted`.

---

## File Structure

- **Create** `frontend/src/pages/Device.vue` — the approval page.
- **Modify** `frontend/src/router/index.ts` — add `/device` route + guard preserves `redirect`.
- **Modify** `frontend/src/pages/Login.vue` — honor validated `?redirect=`.
- **Modify** `frontend/src/locale/en-US.json` + `frontend/src/locale/zh-CN.json` — add `device.*`.
- **Create** `frontend/tests/unit/Device.spec.ts` — page tests.
- **Create** `frontend/tests/unit/loginRedirect.spec.ts` — auth-flow redirect tests (new file; `auth.spec.ts` is store-only and intentionally minimal — don't pollute it).

---

## Milestone M1 — Router guard `redirect` + Login.vue same-origin honor

### Task 1: redirect-after-login

**Files:** `frontend/src/router/index.ts`, `frontend/src/pages/Login.vue`, `frontend/tests/unit/loginRedirect.spec.ts` (new).

- [ ] **Step 1 (failing tests):** create `frontend/tests/unit/loginRedirect.spec.ts`. Mirror the `AppShell.spec.ts` pattern: mock `vue-router` to capture `push` AND `replace`, mock `useRoute()` to return the desired query, seed Pinia with auth store.

  ```ts
  import { beforeEach, describe, expect, test, vi } from 'vitest'
  import { mount } from '@vue/test-utils'
  import { createPinia, setActivePinia } from 'pinia'
  import ElementPlus from 'element-plus'
  import { createI18n } from 'vue-i18n'
  import en from '@/locale/en-US.json'
  import Login from '@/pages/Login.vue'

  const replace = vi.fn()
  const push = vi.fn()
  let mockQuery: Record<string, unknown> = {}
  vi.mock('vue-router', async (importOriginal) => ({
    ...(await importOriginal<typeof import('vue-router')>()),
    useRouter: () => ({ push, replace }),
    useRoute: () => ({ query: mockQuery }),
  }))

  const i18n = createI18n({ legacy: false, locale: 'en-US', messages: { 'en-US': en } })
  function mountLogin() {
    return mount(Login, { global: { plugins: [ElementPlus, i18n] } })
  }

  describe('Login redirect-after-login (FU6-UI)', () => {
    beforeEach(() => {
      setActivePinia(createPinia())
      replace.mockClear(); push.mockClear()
      mockQuery = {}
    })

    test('honors same-origin /device?user_code=... redirect', async () => {
      mockQuery = { redirect: '/device?user_code=ABCD-1234' }
      const w = mountLogin()
      await (w.vm as unknown as { form: { token: string } }).form
      ;(w.vm as unknown as { form: { token: string } }).form.token = 'tok'
      await (w.vm as unknown as { onSubmit: () => Promise<void> }).onSubmit()
      expect(replace).toHaveBeenCalledWith('/device?user_code=ABCD-1234')
    })

    test('rejects external https:// redirect, falls back to /', async () => {
      mockQuery = { redirect: 'https://attacker.example/' }
      const w = mountLogin()
      ;(w.vm as unknown as { form: { token: string } }).form.token = 'tok'
      await (w.vm as unknown as { onSubmit: () => Promise<void> }).onSubmit()
      expect(replace).toHaveBeenCalledWith('/')
    })

    test('rejects protocol-relative // redirect, falls back to /', async () => {
      mockQuery = { redirect: '//attacker.example/' }
      const w = mountLogin()
      ;(w.vm as unknown as { form: { token: string } }).form.token = 'tok'
      await (w.vm as unknown as { onSubmit: () => Promise<void> }).onSubmit()
      expect(replace).toHaveBeenCalledWith('/')
    })

    test('rejects /login redirect (loop defense), falls back to /', async () => {
      mockQuery = { redirect: '/login?redirect=/login' }
      const w = mountLogin()
      ;(w.vm as unknown as { form: { token: string } }).form.token = 'tok'
      await (w.vm as unknown as { onSubmit: () => Promise<void> }).onSubmit()
      expect(replace).toHaveBeenCalledWith('/')
    })

    test('rejects array-typed redirect (vue-router types it string|string[]|null)', async () => {
      mockQuery = { redirect: ['/a', '/b'] }
      const w = mountLogin()
      ;(w.vm as unknown as { form: { token: string } }).form.token = 'tok'
      await (w.vm as unknown as { onSubmit: () => Promise<void> }).onSubmit()
      expect(replace).toHaveBeenCalledWith('/')
    })

    test('no redirect query → defaults to /', async () => {
      mockQuery = {}
      const w = mountLogin()
      ;(w.vm as unknown as { form: { token: string } }).form.token = 'tok'
      await (w.vm as unknown as { onSubmit: () => Promise<void> }).onSubmit()
      expect(replace).toHaveBeenCalledWith('/')
    })
  })
  ```

  The `Login.vue` `onSubmit` will need to be `expose`d (Vue 3 setup defaults to not exposing; check whether `<script setup>` auto-exposes via `defineExpose`). If `(w.vm as any).onSubmit` is undefined, add `defineExpose({ onSubmit })` in Login.vue. The `form` ref needs similar exposure. Pattern: `TaskCreate.spec.ts` calls `(w.vm as ...).submit()` and `(w.vm as ...).form.x = ...` — that file's TaskCreate.vue must `defineExpose({form, submit})` for this to work. **Verify** by reading `frontend/src/pages/TaskCreate.vue` for the `defineExpose` pattern, then apply the same to Login.vue.

- [ ] **Step 2: verify FAIL** — `cd "D:/download_weights/frontend" && pnpm exec vitest run tests/unit/loginRedirect.spec.ts` — all FAIL (safeRedirect not implemented yet).

- [ ] **Step 3 (router guard):** in `frontend/src/router/index.ts`, change line 51:
  ```ts
  if (!auth.isAuthenticated) return { path: '/login' }
  ```
  to:
  ```ts
  if (!auth.isAuthenticated) return { path: '/login', query: { redirect: to.fullPath } }
  ```
  `to.meta.public` short-circuit at line 49 prevents `/login → /login` recursion.

- [ ] **Step 4 (Login.vue):** in `frontend/src/pages/Login.vue`:

  a) Add the `safeRedirect` helper (top of `<script setup>`, after imports):
  ```ts
  function safeRedirect(raw: unknown): string {
    if (typeof raw !== 'string') return '/'
    // Same-origin path only: must start with '/' and NOT '//' (protocol-relative).
    if (!raw.startsWith('/') || raw.startsWith('//')) return '/'
    // Loop defense: never redirect back to /login.
    if (raw === '/login' || raw.startsWith('/login?') || raw.startsWith('/login/')) return '/'
    return raw
  }
  ```

  b) In `onMounted` (line 21-28), change:
  ```ts
  if (authStore.isAuthenticated) {
    router.replace(safeRedirect(route.query.redirect))
  }
  ```
  (replaces `router.replace('/')`).

  c) In `onSubmit` (line 30-36), change `router.push('/')` to:
  ```ts
  router.replace(safeRedirect(route.query.redirect))
  ```

  d) If `Login.vue` does NOT already `defineExpose({form, onSubmit})`, add it at the end of `<script setup>`:
  ```ts
  defineExpose({ form, onSubmit })
  ```
  (Required so `(w.vm as any).form.token = ...` and `(w.vm as any).onSubmit()` work in the test — Vue 3 `<script setup>` does not auto-expose.)

- [ ] **Step 5: verify PASS** — `pnpm exec vitest run tests/unit/loginRedirect.spec.ts` — all pass.

- [ ] **Step 6 (frontend M1 gate):** `cd "D:/download_weights/frontend" && pnpm lint && pnpm typecheck && pnpm test:unit && pnpm build` — all green.

- [ ] **Step 7: commit:**
  ```bash
  cd "D:/download_weights"
  git add frontend/src/router/index.ts frontend/src/pages/Login.vue frontend/tests/unit/loginRedirect.spec.ts
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

  **Verify parity manually**: every key in `device.*` is present in BOTH files; same key count. The existing `localeParity.spec.ts` (if present) will catch drift at gate time.

- [ ] **Step 2 (failing tests):** create `frontend/tests/unit/Device.spec.ts`. Pattern: `TaskCreate.spec.ts` for `vi.hoisted` + `useAuthStore().login()` + `tok()`/`b64()` helpers; `AuditPage.spec.ts` for the `findBtn()!.trigger()` button-find idiom. Mock `vue-router` with mutable `useRoute().query` (so each test can set `user_code`).

  ```ts
  import { beforeEach, describe, expect, test, vi } from 'vitest'
  import { flushPromises, mount } from '@vue/test-utils'
  import { createPinia, setActivePinia } from 'pinia'
  import ElementPlus from 'element-plus'
  import { createI18n } from 'vue-i18n'
  import en from '@/locale/en-US.json'
  import { useAuthStore } from '@/stores/auth'

  // Hoisted mock for axios client (TDZ-safe).
  const { post } = vi.hoisted(() => ({ post: vi.fn() }))
  vi.mock('@/api/client', () => ({ client: { post } }))

  // Mutable route mock so each test can set ?user_code=.
  let mockQuery: Record<string, unknown> = {}
  vi.mock('vue-router', async (importOriginal) => ({
    ...(await importOriginal<typeof import('vue-router')>()),
    useRoute: () => ({ query: mockQuery }),
    useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  }))

  const b64 = (o: unknown) =>
    btoa(JSON.stringify(o)).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_')
  const tok = (payload: Record<string, unknown>) =>
    `h.${b64(payload)}.s`

  const i18n = createI18n({ legacy: false, locale: 'en-US', messages: { 'en-US': en } })
  function mountDevice() {
    return import('@/pages/Device.vue').then((m) =>
      mount(m.default, { global: { plugins: [ElementPlus, i18n] } }))
  }

  function findBtnByText(w: ReturnType<typeof mount>, text: string) {
    return w.findAll('button').find((b) => b.text() === text)
  }

  describe('Device.vue (FU6-UI)', () => {
    beforeEach(() => {
      setActivePinia(createPinia())
      post.mockReset()
      mockQuery = { user_code: 'ABCD-1234' }
      // Seed a real (non-service) authenticated principal by default.
      useAuthStore().login(tok({ sub: '42', tid: 1, role: 'tenant_operator', pids: [] }))
    })

    test('pre-fills user_code from ?user_code= query', async () => {
      const w = await mountDevice()
      await flushPromises()
      const input = w.find('input')
      expect((input.element as HTMLInputElement).value).toBe('ABCD-1234')
    })

    test('Approve posts to /api/v1/auth/device/approve and shows success', async () => {
      post.mockResolvedValueOnce({ data: { status: 'approved' } })
      const w = await mountDevice()
      await flushPromises()
      const btn = findBtnByText(w, en.device.approve)
      expect(btn).toBeTruthy()
      await btn!.trigger('click')
      await flushPromises()
      expect(post).toHaveBeenCalledWith(
        '/api/v1/auth/device/approve',
        { user_code: 'ABCD-1234', action: 'approve' },
      )
      expect(w.text()).toContain(en.device.successApproved)
    })

    test('Deny posts with action="deny" and shows denied message', async () => {
      post.mockResolvedValueOnce({ data: { status: 'denied' } })
      const w = await mountDevice()
      await flushPromises()
      const btn = findBtnByText(w, en.device.deny)
      expect(btn).toBeTruthy()
      await btn!.trigger('click')
      await flushPromises()
      expect(post).toHaveBeenCalledWith(
        '/api/v1/auth/device/approve',
        { user_code: 'ABCD-1234', action: 'deny' },
      )
      expect(w.text()).toContain(en.device.successDenied)
    })

    test('404 DEVICE_CODE_INVALID → invalid-code error', async () => {
      post.mockRejectedValueOnce({
        response: { status: 404, data: { detail: { code: 'DEVICE_CODE_INVALID' } } },
      })
      const w = await mountDevice()
      await flushPromises()
      const btn = findBtnByText(w, en.device.approve)
      await btn!.trigger('click')
      await flushPromises()
      expect(w.text()).toContain(en.device.errorInvalid)
    })

    test('403 SERVICE_CANNOT_APPROVE → service-token error', async () => {
      post.mockRejectedValueOnce({
        response: { status: 403, data: { detail: { code: 'SERVICE_CANNOT_APPROVE' } } },
      })
      const w = await mountDevice()
      await flushPromises()
      const btn = findBtnByText(w, en.device.approve)
      await btn!.trigger('click')
      await flushPromises()
      expect(w.text()).toContain(en.device.errorService)
    })

    test('service-token principal → Approve disabled + warning alert', async () => {
      // Replace the default real user with a service-token user.
      useAuthStore().logout()
      useAuthStore().login(tok({ sub: '0', tid: 1, role: 'system_admin', pids: [] }))
      const w = await mountDevice()
      await flushPromises()
      // Warning alert visible (renders en.device.serviceWarning text).
      expect(w.text()).toContain(en.device.serviceWarning)
      // Approve button disabled.
      const btns = w.findAllComponents({ name: 'ElButton' })
      const approveBtn = btns.find((b) => b.text() === en.device.approve)
      expect(approveBtn).toBeTruthy()
      expect(approveBtn!.props('disabled')).toBe(true)
    })
  })
  ```

- [ ] **Step 3: verify FAIL** — `pnpm exec vitest run tests/unit/Device.spec.ts` — FAILs (Device.vue doesn't exist).

- [ ] **Step 4 (Device.vue):** create `frontend/src/pages/Device.vue`. Mirror Login.vue's SFC structure. Use `<script setup lang="ts">`. `defineExpose` is NOT required (tests don't drive form via `vm.x`; they drive via button clicks).

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
    const e = err as {
      response?: { status?: number; data?: { detail?: { code?: string } } }
    }
    const httpStatus = e?.response?.status
    const code = e?.response?.data?.detail?.code
    if (httpStatus === 404 && code === 'DEVICE_CODE_INVALID') return t('device.errorInvalid')
    if (httpStatus === 403 && code === 'SERVICE_CANNOT_APPROVE') return t('device.errorService')
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
          user #{{ principal.userId }} ({{ principal.role }}, tenant {{ principal.tenantId }})
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

- [ ] **Step 5 (route registration):** add to `frontend/src/router/index.ts` in `routes`, after `/settings` (line ~42):
  ```ts
  {
    path: '/device', name: 'device',
    component: () => import('@/pages/Device.vue'),
  },
  ```

- [ ] **Step 6: verify PASS** — `pnpm exec vitest run tests/unit/Device.spec.ts` — all pass.

- [ ] **Step 7 (full frontend gate):** `cd "D:/download_weights/frontend" && pnpm lint && pnpm typecheck && pnpm test:unit && pnpm build` — all green.

- [ ] **Step 8: commit:**
  ```bash
  cd "D:/download_weights"
  git add frontend/src/pages/Device.vue frontend/src/router/index.ts frontend/src/locale/en-US.json frontend/src/locale/zh-CN.json frontend/tests/unit/Device.spec.ts
  git commit -m "feat(fu6-ui): browser device-approval page (/device)"
  ```

### Task 3: M2 full backend gate
- [ ] Backend untouched, but verify nothing regressed: `cd "D:/download_weights" && uv run pytest -q` — all pass. `uv run python tools/lint_invariants.py --strict` OK. No commit.

---

## Self-Review

- **Spec coverage:** §0 route + guard redirect → Task 1 ✓; §0 Device.vue page → Task 2 ✓; §0 service-token detection → Step 4 `:disabled="isServiceToken"` + `el-alert` ✓; §0 error mapping → `mapError` covers 404/403/other ✓; §1 open-redirect defense → `safeRedirect` validates path-only + rejects `/login*` loop + rejects array ✓; §2 tests 6 page tests + 6 redirect tests ✓.
- **Placeholder scan:** all code blocks concrete. No TBDs. References to existing specs (`TaskCreate.spec.ts`, `AppShell.spec.ts`, `AuditPage.spec.ts`, `session.spec.ts`) are verified to exist with the cited patterns.
- **Type consistency:** `userCode: Ref<string>`; `status: Ref<'idle'|'submitting'|'approved'|'denied'>`; `errorMsg: Ref<string|null>`; `mapError(unknown) => string`; `safeRedirect(unknown) => string`. `findBtn` returns `DOMWrapper | undefined` → assert truthy → `!.trigger()` per `noUncheckedIndexedAccess`.
- **Open risks for reviewers:** (a) `defineExpose({form, onSubmit})` in Login.vue — TaskCreate.vue already does this for its `form`/`submit`; Login.vue may not, and the redirect tests reach into `vm.form.token` + `vm.onSubmit()`. Step 4d adds it. (b) Service-token check is client-side hint; backend's 403 is authoritative. (c) `safeRedirect` rejects any path matching `/login*` — covers `/login`, `/login?...`, `/login/...`; does NOT cover URL-encoded `%2Flogin` (browsers normalize before routing, vue-router resolves before guard sees it — accepted). (d) Two `el-alert` siblings (warning + error) can appear simultaneously if a service-token user somehow clicks Approve (button disabled, but defense-in-depth) — visually stacked, no overlap.
