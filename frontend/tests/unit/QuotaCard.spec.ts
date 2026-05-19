import { describe, expect, test } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import ElementPlus from 'element-plus'
import QuotaCard from '@/components/infra/QuotaCard.vue'
import en from '@/locale/en-US.json'

const i18n = createI18n({
  legacy: false, locale: 'en-US', messages: { 'en-US': en },
})

describe('QuotaCard', () => {
  test('renders label, formatted used/quota, percent', () => {
    const w = mount(QuotaCard, {
      props: { label: 'bytes', used: 1024 * 1024, quota: 2 * 1024 * 1024,
        format: 'bytes' },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.text()).toContain('1.0 MB')
    expect(w.text()).toContain('2.0 MB')
    expect(w.text()).toContain('50%')
  })
  test('over-threshold → warning chip', () => {
    const w = mount(QuotaCard, {
      props: { label: 'concurrent', used: 9, quota: 10, format: 'count' },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.text()).toContain('90%')
    // Pre-review BLOCKER fix: Task 11 (this component) runs BEFORE Task 12
    // adds the i18n keys; assert against the literal key path that vue-i18n
    // returns on a missing key.
    expect(w.text()).toContain('quotaPage.threshold.warn')
  })
  test('over-cap → over chip + 100%', () => {
    const w = mount(QuotaCard, {
      props: { label: 'bytes', used: 200, quota: 100, format: 'bytes' },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.text()).toContain('100%')
    expect(w.text()).toContain('quotaPage.threshold.over')
  })
  test('zero quota → renders 0% (no NaN)', () => {
    const w = mount(QuotaCard, {
      props: { label: 'x', used: 0, quota: 0, format: 'count' },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.text()).toContain('0%')
  })
})
