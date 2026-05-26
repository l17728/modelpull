import { describe, expect, test } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import ElementPlus from 'element-plus'
import en from '@/locale/en-US.json'
import CopilotToolCard from '@/components/copilot/CopilotToolCard.vue'

const i18n = createI18n({ legacy: false, locale: 'en-US', messages: { 'en-US': en } })

function mountCard(props: Record<string, unknown>) {
  return mount(CopilotToolCard, {
    props,
    global: { plugins: [ElementPlus, i18n] },
  })
}

describe('CopilotToolCard', () => {
  test('renders pending state when ok is undefined', () => {
    const w = mountCard({ tool: 'web_search', input: { query: 'test' } })
    expect(w.text()).toContain('running')
    expect(w.text()).toContain('web_search')
    expect(w.text()).toContain('query="test"')
  })

  test('renders success state and items count summary', () => {
    const w = mountCard({
      tool: 'dlw_list_tasks',
      input: { limit: 5 },
      ok: true,
      output: { items: [{}, {}, {}] },
    })
    expect(w.text()).toContain('ok')
    expect(w.text()).toContain('3 item(s)')
  })

  test('renders failure state and error message', () => {
    const w = mountCard({
      tool: 'web_search',
      input: { query: 'q' },
      ok: false,
      output: { error: 'rate_limited' },
    })
    expect(w.text()).toContain('failed')
    expect(w.text()).toContain('rate_limited')
  })

  test('truncates long string args', () => {
    const long = 'x'.repeat(200)
    const w = mountCard({ tool: 'web_search', input: { query: long } })
    expect(w.text()).toContain('…')
  })

  test('picks tool-specific icon', () => {
    const w = mountCard({ tool: 'search_modelscope_models', input: { query: 'q' } })
    expect(w.text()).toContain('🔍')
  })

  test('falls back to generic icon for unknown tools', () => {
    const w = mountCard({ tool: 'unknown_tool', input: {} })
    expect(w.text()).toContain('🛠')
  })

  test('summarizes results array as N result(s)', () => {
    const w = mountCard({
      tool: 'web_search',
      input: { query: 'q' },
      ok: true,
      output: { query: 'q', results: [{}, {}] },
    })
    expect(w.text()).toContain('2 result(s)')
  })
})
