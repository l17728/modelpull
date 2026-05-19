import { describe, expect, test } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import ElementPlus from 'element-plus'
import EventRow from '@/components/taskdetail/EventRow.vue'
import en from '@/locale/en-US.json'

const i18n = createI18n({
  legacy: false, locale: 'en-US', messages: { 'en-US': en },
})

describe('EventRow', () => {
  test('renders ts, message, level tag', () => {
    const w = mount(EventRow, {
      props: {
        event: {
          ts: '2026-05-20T12:00:00Z', type: 'task.denied',
          message: 'task.denied (denied)', details: {},
        },
      },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.text()).toContain('task.denied (denied)')
    expect(w.findComponent({ name: 'ElTag' }).exists()).toBe(true)
  })
})
