import { describe, expect, test } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import ElementPlus from 'element-plus'
import AuditRow from '@/components/infra/AuditRow.vue'
import en from '@/locale/en-US.json'

const i18n = createI18n({
  legacy: false, locale: 'en-US', messages: { 'en-US': en },
})

describe('AuditRow', () => {
  test('success outcome → success tag, action visible', () => {
    const w = mount(AuditRow, {
      props: {
        entry: {
          id: 1, occurred_at: '2026-05-20T12:00:00Z', tenant_id: 1,
          actor_user_id: 7, actor_ip: '10.0.0.1', action: 'task.created',
          resource_type: 'task', resource_id: 'abcdef1234567890abcdef',
          outcome: 'success', payload: {}, trace_id: 't1',
          prev_hash: null, self_hash: 's',
        },
      },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.text()).toContain('task.created')
    expect(w.text()).toContain('7')
    expect(w.findComponent({ name: 'ElTag' }).props('type')).toBe('success')
    expect(w.text()).toContain('abcdef1234567890')
  })
  test('denied → danger', () => {
    const w = mount(AuditRow, {
      props: {
        entry: {
          id: 2, occurred_at: '2026-05-20T12:00:00Z', tenant_id: 1,
          actor_user_id: null, actor_ip: '', action: 'task.denied',
          resource_type: 'task', resource_id: null, outcome: 'denied',
          payload: {}, trace_id: '', prev_hash: null, self_hash: 's',
        },
      },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.findComponent({ name: 'ElTag' }).props('type')).toBe('danger')
    expect(w.text().toLowerCase()).toMatch(/system|audit\.systemactor/)
  })
})
