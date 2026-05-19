import { beforeEach, describe, expect, test, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus, { ElMessage } from 'element-plus'
import { createI18n } from 'vue-i18n'
import zh from '@/locale/zh-CN.json'
import { useAuthStore } from '@/stores/auth'

// vi.mock factories are hoisted — use vi.hoisted so they don't reference
// an outer const (TDZ).
const { post } = vi.hoisted(() => ({ post: vi.fn() }))
vi.mock('@/api/client', () => ({ client: { post } }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

const i18n = createI18n({ legacy: false, locale: 'zh-CN', messages: { 'zh-CN': zh } })
function mountCreate() {
  return import('@/pages/TaskCreate.vue').then((m) =>
    mount(m.default, { global: { plugins: [ElementPlus, i18n] } }))
}
const b64 = (o: unknown) => btoa(JSON.stringify(o)).replace(/=+$/, '')

describe('TaskCreate wiring', () => {
  beforeEach(() => { setActivePinia(createPinia()); post.mockReset() })

  test('429 → quota_exceeded error message', async () => {
    useAuthStore().login(`h.${b64({ sub: '1', tid: 1, role: 'tenant_admin', pids: [] })}.s`)
    const spy = vi.spyOn(ElMessage, 'error')
    post.mockRejectedValueOnce({ response: { status: 429 } })
    const w = await mountCreate()
    ;(w.vm as unknown as { form: Record<string, unknown> }).form.repo_id = 'o/m'
    ;(w.vm as unknown as { form: Record<string, unknown> }).form.revision = 'a'.repeat(40)
    await (w.vm as unknown as { submit: () => Promise<void> }).submit()
    expect(spy).toHaveBeenCalledWith(zh.errors.quota_exceeded)
  })

  test('service token → submit disabled', async () => {
    useAuthStore().login(`h.${b64({ sub: '0', tid: 1, role: 'system_admin', pids: [] })}.s`)
    const w = await mountCreate()
    const btn = w.findComponent({ name: 'ElButton' })
    expect(btn.props('disabled')).toBe(true)
  })
})
