import { describe, expect, test } from 'vitest'
import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createI18n } from 'vue-i18n'
import DataBoundary from '@/components/DataBoundary.vue'
import zh from '@/locale/zh-CN.json'

const i18n = createI18n({ legacy: false, locale: 'zh-CN', messages: { 'zh-CN': zh } })
const mountB = (props: Record<string, unknown>) =>
  mount(DataBoundary, {
    props, slots: { default: '<div class="content">DATA</div>' },
    global: { plugins: [ElementPlus, i18n] },
  })

describe('DataBoundary', () => {
  test('loading → skeleton, no content', () => {
    const w = mountB({ loading: true })
    expect(w.findComponent({ name: 'ElSkeleton' }).exists()).toBe(true)
    expect(w.find('.content').exists()).toBe(false)
  })
  test('forbidden → forbidden message', () => {
    const w = mountB({ loading: false, forbidden: true })
    expect(w.text()).toContain(zh.errors.forbidden)
    expect(w.find('.content').exists()).toBe(false)
  })
  test('error → alert', () => {
    const w = mountB({ loading: false, error: true })
    expect(w.findComponent({ name: 'ElAlert' }).exists()).toBe(true)
  })
  test('empty → EmptyState', () => {
    const w = mountB({ loading: false, isEmpty: true })
    expect(w.findComponent({ name: 'EmptyState' }).exists()).toBe(true)
  })
  test('ok → renders default slot', () => {
    const w = mountB({ loading: false })
    expect(w.find('.content').text()).toBe('DATA')
  })
})
