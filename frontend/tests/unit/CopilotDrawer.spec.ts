import { beforeEach, describe, expect, test, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { createI18n } from 'vue-i18n'
import { nextTick } from 'vue'
import en from '@/locale/en-US.json'

const { copilotState } = vi.hoisted(() => ({
  copilotState: { value: null as unknown },
}))

vi.mock('@/composables/useCopilot', async () => {
  const { ref } = await import('vue')
  return {
    useCopilot: () => copilotState.value ?? {
      messages: ref([]), streaming: ref(false), conversationId: ref(null),
      conversations: ref([]), hasPendingConfirm: ref(false),
      send: vi.fn(), confirm: vi.fn(), refreshConversations: vi.fn(),
      loadConversation: vi.fn(), newConversation: vi.fn(),
    },
  }
})

const i18n = createI18n({
  legacy: false, locale: 'en-US', messages: { 'en-US': en },
})

async function mountDrawer() {
  const { useUiStore } = await import('@/stores/ui')
  const ui = useUiStore()
  ui.copilotOpen = true
  const m = await import('@/components/copilot/CopilotDrawer.vue')
  const w = mount(m.default, {
    global: {
      plugins: [ElementPlus, i18n],
      // el-drawer teleports to body; stub it to a passthrough so the
      // default slot renders inside the wrapper for assertions.
      stubs: { 'el-drawer': { template: '<div><slot /></div>' } },
    },
  })
  await flushPromises(); await nextTick()
  return w
}

describe('CopilotDrawer (SP4a)', () => {
  beforeEach(() => { setActivePinia(createPinia()); copilotState.value = null })

  test('renders user bubble, assistant bubble, and tool card', async () => {
    const { ref } = await import('vue')
    copilotState.value = {
      messages: ref([
        { role: 'user', text: 'list my tasks', toolCards: [], steps: [] },
        { role: 'assistant', text: 'You have 0 tasks.',
          toolCards: [
            { id: 'c1', tool: 'dlw_list_tasks', ok: true,
              output: { items: [] } }],
          steps: [
            { kind: 'tool', toolCard: {
              id: 'c1', tool: 'dlw_list_tasks', ok: true,
              output: { items: [] } } }] },
      ]),
      streaming: ref(false), conversationId: ref('conv-1'),
      conversations: ref([]), hasPendingConfirm: ref(false),
      send: vi.fn(), confirm: vi.fn(), refreshConversations: vi.fn(),
      loadConversation: vi.fn(), newConversation: vi.fn(),
    }
    const w = await mountDrawer()
    const txt = w.text()
    expect(txt).toContain('list my tasks')
    expect(txt).toContain('You have 0 tasks.')
    expect(txt).toContain('dlw_list_tasks')
  })
})
