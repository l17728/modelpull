import { beforeEach, describe, expect, test, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { createI18n } from 'vue-i18n'
import en from '@/locale/en-US.json'

vi.mock('vue-router', async (importOriginal) => ({
  ...(await importOriginal<typeof import('vue-router')>()),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useRoute: () => ({ query: {} }),
}))

const { quotaData, healthData } = vi.hoisted(() => ({
  quotaData: { value: null as unknown },
  healthData: { value: null as unknown },
}))

vi.mock('@/composables/useQuota', async () => {
  const { ref } = await import('vue')
  return {
    useQuota: () => ({
      data: ref(quotaData.value), isLoading: ref(false),
      isError: ref(false), error: ref(null),
    }),
  }
})
vi.mock('@/composables/useSystemHealth', async () => {
  const { ref } = await import('vue')
  return {
    useSystemHealth: () => ({
      data: ref(healthData.value), isLoading: ref(false),
      isError: ref(false), error: ref(null),
    }),
  }
})

const b64 = (o: unknown) => btoa(JSON.stringify(o)).replace(/=+$/, '')
const i18n = createI18n({
  legacy: false, locale: 'en-US', messages: { 'en-US': en },
})

describe('Quota + Settings pages', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    quotaData.value = null
    healthData.value = null
  })
  test('QuotaPage with data → 3 QuotaCards', async () => {
    quotaData.value = {
      tenant_id: 1, bytes_used_month: 1024, bytes_quota_month: 2048,
      storage_gb_used: 1, storage_gb_quota: 10,
      concurrent_tasks: 1, concurrent_quota: 5,
    }
    const m = await import('@/pages/QuotaPage.vue')
    const w = mount(m.default, {
      global: { plugins: [ElementPlus, i18n] },
    })
    await flushPromises()
    expect(w.findAllComponents({ name: 'QuotaCard' }).length).toBe(3)
  })
  test('QuotaPage no data → empty', async () => {
    const m = await import('@/pages/QuotaPage.vue')
    const w = mount(m.default, {
      global: { plugins: [ElementPlus, i18n] },
    })
    await flushPromises()
    expect(w.findComponent({ name: 'EmptyState' }).exists()).toBe(true)
  })
  test('Settings shows principal + system state', async () => {
    const { useAuthStore } = await import('@/stores/auth')
    useAuthStore().login(`h.${b64({ sub: '7', tid: 3, role: 'tenant_admin',
      pids: [9, 11] })}.s`)
    healthData.value = { status: 'active', controller_state: 'active' }
    const m = await import('@/pages/Settings.vue')
    const w = mount(m.default, {
      global: { plugins: [ElementPlus, i18n] },
    })
    await flushPromises()
    expect(w.text()).toContain('7')
    expect(w.text()).toContain('3')
    expect(w.text()).toContain('tenant_admin')
    expect(w.findComponent({ name: 'HealthPill' }).exists()).toBe(true)
  })
})
