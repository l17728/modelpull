import { beforeEach, describe, expect, test, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { createI18n } from 'vue-i18n'
import en from '@/locale/en-US.json'

const { execData } = vi.hoisted(() => ({
  execData: { value: null as unknown },
}))

vi.mock('@/composables/useExecutors', async () => {
  const { ref } = await import('vue')
  return {
    useExecutors: () => ({
      data: ref(execData.value), isLoading: ref(false),
      isError: ref(false), error: ref(null),
    }),
  }
})

const i18n = createI18n({
  legacy: false, locale: 'en-US', messages: { 'en-US': en },
})
function mountPage() {
  return import('@/pages/Executors.vue').then((m) =>
    mount(m.default, { global: { plugins: [ElementPlus, i18n] } }))
}

describe('Executors page', () => {
  beforeEach(() => { setActivePinia(createPinia()); execData.value = null })
  test('no data → empty', async () => {
    const w = await mountPage()
    await flushPromises()
    expect(w.findComponent({ name: 'EmptyState' }).exists()).toBe(true)
  })
  test('data present → host-grouped rows render', async () => {
    execData.value = {
      items: [
        { id: 'h1-w1', status: 'healthy', health_score: 95, epoch: 1,
          host_id: 'h1', tenant_id: 1, last_heartbeat_at: null,
          nic_speed_gbps: 10, disk_free_gb: null, disk_total_gb: null,
          created_at: null },
        { id: 'h1-w2', status: 'degraded', health_score: 60, epoch: 1,
          host_id: 'h1', tenant_id: 1, last_heartbeat_at: null,
          nic_speed_gbps: 10, disk_free_gb: null, disk_total_gb: null,
          created_at: null },
        { id: 'h2-w1', status: 'healthy', health_score: 100, epoch: 1,
          host_id: 'h2', tenant_id: null, last_heartbeat_at: null,
          nic_speed_gbps: null, disk_free_gb: null, disk_total_gb: null,
          created_at: null },
      ],
    }
    const w = await mountPage()
    await flushPromises()
    expect(w.findAllComponents({ name: 'ExecutorRow' }).length).toBe(3)
    expect(w.text()).toContain('h1')
    expect(w.text()).toContain('h2')
  })
})
