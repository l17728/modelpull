import { describe, expect, test } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import en from '@/locale/en-US.json'
import CopilotMessageBubble from '@/components/copilot/CopilotMessageBubble.vue'

const i18n = createI18n({ legacy: false, locale: 'en-US', messages: { 'en-US': en } })

interface CardLike { tool: string; ok?: boolean }
function mountBubble(props: {
  role: 'user' | 'assistant'; text: string; toolCards: CardLike[]
}) {
  return mount(CopilotMessageBubble, {
    props, global: { plugins: [i18n] },
  })
}

describe('CopilotMessageBubble', () => {
  test('user message renders as plain text, no source footer', () => {
    const w = mountBubble({ role: 'user', text: 'hello **world**', toolCards: [] })
    expect(w.find('.plain').text()).toBe('hello **world**')
    expect(w.find('.src').exists()).toBe(false)
  })

  test('assistant message renders markdown bold', () => {
    const w = mountBubble({ role: 'assistant', text: 'hello **world**', toolCards: [] })
    expect(w.find('.md').html()).toContain('<strong>world</strong>')
  })

  test('assistant with no tools shows "from model knowledge" badge', () => {
    const w = mountBubble({ role: 'assistant', text: 'an answer', toolCards: [] })
    expect(w.text()).toContain('model knowledge')
    expect(w.find('.src-model').exists()).toBe(true)
  })

  test('assistant with web_search shows "via web search" badge', () => {
    const w = mountBubble({
      role: 'assistant', text: 'searched',
      toolCards: [{ tool: 'web_search', ok: true }],
    })
    expect(w.text()).toContain('via web search')
    expect(w.find('.src-web').exists()).toBe(true)
    expect(w.find('.src-model').exists()).toBe(false)
  })

  test('assistant with ModelScope tool shows "via ModelScope" badge', () => {
    const w = mountBubble({
      role: 'assistant', text: 'looked up',
      toolCards: [{ tool: 'search_modelscope_models', ok: true }],
    })
    expect(w.text()).toContain('via ModelScope')
  })

  test('assistant with hf tool shows "via Hugging Face" badge', () => {
    const w = mountBubble({
      role: 'assistant', text: 'fetched meta',
      toolCards: [{ tool: 'hf_api_metadata', ok: true }],
    })
    expect(w.text()).toContain('via Hugging Face')
    expect(w.find('.src-hf').exists()).toBe(true)
  })

  test('assistant with internal dlw tool shows "internal data" badge', () => {
    const w = mountBubble({
      role: 'assistant', text: 'task list',
      toolCards: [{ tool: 'dlw_list_tasks', ok: true }],
    })
    expect(w.text()).toContain('internal data')
  })

  test('multiple tools dedupe into category badges', () => {
    const w = mountBubble({
      role: 'assistant', text: 'mixed',
      toolCards: [
        { tool: 'hf_api_metadata', ok: true },
        { tool: 'hf_model_card', ok: true },
      ],
    })
    const hfBadges = w.findAll('.src-hf')
    expect(hfBadges.length).toBe(1)
  })

  test('failed tool surfaces error badge', () => {
    const w = mountBubble({
      role: 'assistant', text: 'tried',
      toolCards: [{ tool: 'web_search', ok: false }],
    })
    expect(w.find('.src-error').exists()).toBe(true)
    expect(w.text()).toContain('tool call failed')
  })

  test('strips <script> tags from assistant text before parsing', () => {
    const w = mountBubble({
      role: 'assistant',
      text: 'safe <script>alert(1)</script> text',
      toolCards: [],
    })
    expect(w.find('.md').html()).not.toContain('<script')
    expect(w.find('.md').text()).toContain('safe')
    expect(w.find('.md').text()).toContain('text')
  })

  test('renders code fences as <pre><code>', () => {
    const w = mountBubble({
      role: 'assistant',
      text: '```js\nconsole.log(1)\n```',
      toolCards: [],
    })
    expect(w.find('pre').exists()).toBe(true)
    expect(w.find('pre code').text()).toContain('console.log(1)')
  })
})
