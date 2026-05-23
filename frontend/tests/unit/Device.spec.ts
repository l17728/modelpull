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
