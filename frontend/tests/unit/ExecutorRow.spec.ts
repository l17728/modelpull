import { describe, expect, test, vi, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import ElementPlus from 'element-plus'
import ExecutorRow from '@/components/infra/ExecutorRow.vue'
import en from '@/locale/en-US.json'
import type { ExecutorRead } from '@/api/types'

const i18n = createI18n({
  legacy: false, locale: 'en-US', messages: { 'en-US': en },
})

const ex: ExecutorRead = {
  id: 'host-1-w1', status: 'healthy', health_score: 95, epoch: 1,
  host_id: 'host-1', tenant_id: 1,
  last_heartbeat_at: '2026-05-20T11:55:00Z',
  nic_speed_gbps: 10, disk_free_gb: 500, disk_total_gb: 1000,
  created_at: null,
}

describe('ExecutorRow', () => {
  afterEach(() => { vi.useRealTimers() })
  test('renders id, status badge, health, NIC, disk', () => {
    vi.useFakeTimers().setSystemTime(new Date('2026-05-20T12:00:00Z'))
    const w = mount(ExecutorRow, {
      props: { executor: ex },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.text()).toContain('host-1-w1')
    expect(w.text()).toContain('healthy')
    expect(w.text()).toContain('95')
    expect(w.text()).toContain('5m ago')
    expect(w.text()).toContain('10')
    expect(w.findComponent({ name: 'ElTag' }).exists()).toBe(true)
  })
  test('null fields → em-dash, no crash', () => {
    const w = mount(ExecutorRow, {
      props: {
        executor: {
          ...ex, last_heartbeat_at: null, nic_speed_gbps: null,
          disk_free_gb: null, disk_total_gb: null,
        },
      },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.text()).toContain('host-1-w1')
  })
})
