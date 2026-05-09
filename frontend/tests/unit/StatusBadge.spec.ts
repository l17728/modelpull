import { describe, expect, test } from 'vitest'
import { createI18n } from 'vue-i18n'
import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'

import StatusBadge from '@/components/StatusBadge.vue'
import zhCN from '@/locale/zh-CN.json'

const i18n = createI18n({
  legacy: false,
  locale: 'zh-CN',
  messages: { 'zh-CN': zhCN },
})

const cases: ReadonlyArray<[string, 'info' | 'warning' | 'primary' | 'success' | 'danger']> = [
  ['pending', 'info'],
  ['queued', 'info'],
  ['scheduling', 'warning'],
  ['downloading', 'primary'],
  ['succeeded', 'success'],
  ['failed', 'danger'],
  ['cancelled', 'info'],
  ['unknown', 'info'],
]

describe('StatusBadge', () => {
  test.each(cases)('status %s → tag type %s', (status, expectedType) => {
    const wrapper = mount(StatusBadge, {
      props: { status },
      global: { plugins: [ElementPlus, i18n] },
    })
    const tag = wrapper.findComponent({ name: 'ElTag' })
    expect(tag.exists()).toBe(true)
    expect(tag.props('type')).toBe(expectedType)
  })
})
