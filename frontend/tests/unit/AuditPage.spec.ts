import { beforeEach, describe, expect, test, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { createI18n } from 'vue-i18n'
import en from '@/locale/en-US.json'

const { auditData } = vi.hoisted(() => ({
  auditData: { value: null as unknown },
}))

vi.mock('@/composables/useAuditLog', async () => {
  const { ref } = await import('vue')
  return {
    useAuditLog: () => ({
      data: ref(auditData.value), isLoading: ref(false),
      isError: ref(false), error: ref(null),
    }),
    fetchOlderAudit: vi.fn(),
  }
})

const i18n = createI18n({
  legacy: false, locale: 'en-US', messages: { 'en-US': en },
})
function mountPage() {
  return import('@/pages/Audit.vue').then((m) =>
    mount(m.default, { global: { plugins: [ElementPlus, i18n] } }))
}

describe('Audit page', () => {
  beforeEach(() => { setActivePinia(createPinia()); auditData.value = null })
  test('no data → empty', async () => {
    const w = await mountPage()
    await flushPromises()
    expect(w.findComponent({ name: 'EmptyState' }).exists()).toBe(true)
  })
  test('data present → audit rows render + load older shown', async () => {
    auditData.value = {
      items: [
        { id: 1, occurred_at: '2026-05-20T12:00:00Z', tenant_id: 1,
          actor_user_id: 7, actor_ip: '', action: 'task.created',
          resource_type: 'task', resource_id: 'r', outcome: 'success',
          payload: {}, trace_id: '', prev_hash: null, self_hash: 's' },
      ],
      next_cursor: 'NEXT',
    }
    const w = await mountPage()
    await flushPromises()
    expect(w.findAllComponents({ name: 'AuditRow' }).length).toBe(1)
    expect(w.text()).toContain(en.audit.loadOlder)
  })
})
