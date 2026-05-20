import { describe, expect, test } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import ElementPlus from 'element-plus'
import HealthPill from '@/components/infra/HealthPill.vue'
import en from '@/locale/en-US.json'

const i18n = createI18n({
  legacy: false, locale: 'en-US', messages: { 'en-US': en },
})

describe('HealthPill', () => {
  test('active → success ElTag', () => {
    const w = mount(HealthPill, {
      props: { state: 'active' },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.findComponent({ name: 'ElTag' }).props('type')).toBe('success')
    expect(w.text()).toContain('active')
  })
  test('recovering → warning', () => {
    const w = mount(HealthPill, {
      props: { state: 'recovering' },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.findComponent({ name: 'ElTag' }).props('type')).toBe('warning')
  })
  test('standby → info', () => {
    const w = mount(HealthPill, {
      props: { state: 'standby' },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.findComponent({ name: 'ElTag' }).props('type')).toBe('info')
  })
  test('unknown → danger', () => {
    const w = mount(HealthPill, {
      props: { state: 'broken' },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.findComponent({ name: 'ElTag' }).props('type')).toBe('danger')
  })
})
