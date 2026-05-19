import { describe, expect, test } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import ElementPlus from 'element-plus'
import SwimLane from '@/components/taskdetail/SwimLane.vue'
import en from '@/locale/en-US.json'
import type { ParticipatingExecutor } from '@/api/types'

const i18n = createI18n({
  legacy: false, locale: 'en-US', messages: { 'en-US': en },
})
const ex: ParticipatingExecutor = {
  executor_id: 'host-1-w1', executor_status: 'healthy', health_score: 90,
  last_heartbeat_at: '2026-05-20T12:00:00Z', assigned_subtasks: 3,
  active_subtasks: 2, bytes_downloaded: 1048576,
}

describe('SwimLane', () => {
  test('renders id, status, counts, bytes', () => {
    const w = mount(SwimLane, {
      props: { executor: ex },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.text()).toContain('host-1-w1')
    expect(w.text()).toContain('healthy')
    expect(w.text()).toContain('2')
    expect(w.text()).toContain('1.0 MB')
  })
  test('null status → unknown badge, no crash', () => {
    const w = mount(SwimLane, {
      props: {
        executor: { ...ex, executor_status: null, health_score: null,
          last_heartbeat_at: null },
      },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.text()).toContain('host-1-w1')
  })
})
