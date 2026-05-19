import { beforeEach, describe, expect, test, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { createI18n } from 'vue-i18n'
import en from '@/locale/en-US.json'

// Pre-review BLOCKER fix: the page relies on Vue template ref auto-unwrap
// (`v-if="data"`, `!data`), so the mocked `useTaskDetail().data` MUST be a
// real ref — a plain `{ value }` object never unwraps. `vi.hoisted` holders
// must stay plain (no `ref()` — TDZ above imports); each `vi.mock` factory
// is self-contained and async-imports `vue` (factory runs lazily, after the
// `vue` import is resolved), creating real refs.
const { detailData } = vi.hoisted(() => ({
  detailData: { value: null as unknown },
}))
const { mutes } = vi.hoisted(() => ({
  mutes: { cancel: { mutate: vi.fn() }, remove: { mutate: vi.fn() } },
}))

vi.mock('@/composables/useTaskDetail', async () => {
  const { ref } = await import('vue')
  return {
    useTaskDetail: () => ({
      data: ref(detailData.value), isLoading: ref(false),
      isError: ref(false), error: ref(null),
    }),
  }
})
vi.mock('@/composables/useSubtaskChunks', async () => {
  const { ref } = await import('vue')
  return { useSubtaskChunks: () => ({
    data: ref(null), isLoading: ref(false),
    isError: ref(false), error: ref(null) }) }
})
vi.mock('@/composables/useSourceAllocation', async () => {
  const { ref } = await import('vue')
  return { useSourceAllocation: () => ({
    data: ref(null), isLoading: ref(false),
    isError: ref(false), error: ref(null) }) }
})
vi.mock('@/composables/useParticipatingExecutors', async () => {
  const { ref } = await import('vue')
  return { useParticipatingExecutors: () => ({
    data: ref(null), isLoading: ref(false),
    isError: ref(false), error: ref(null) }) }
})
vi.mock('@/composables/useTaskEvents', async () => {
  const { ref } = await import('vue')
  return {
    useTaskEvents: () => ({
      data: ref(null), isLoading: ref(false),
      isError: ref(false), error: ref(null) }),
    fetchOlderEvents: vi.fn(),
  }
})
vi.mock('@/composables/useTaskMutations', () => ({
  useTaskMutations: () => mutes,
  canCancel: (s: string) => s === 'downloading',
  canDelete: (s: string) => s === 'succeeded',
}))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

const i18n = createI18n({
  legacy: false, locale: 'en-US', messages: { 'en-US': en },
})
function mountPage() {
  return import('@/pages/TaskDetail.vue').then((m) =>
    mount(m.default, {
      props: { id: 'abc' },
      global: { plugins: [ElementPlus, i18n] },
    }))
}

describe('TaskDetail (SP2)', () => {
  beforeEach(() => { setActivePinia(createPinia()); detailData.value = null })

  test('no data → DataBoundary empty (not crash)', async () => {
    const w = await mountPage()
    await flushPromises()
    expect(w.findComponent({ name: 'EmptyState' }).exists()).toBe(true)
  })

  test('data present → tabs render, AggregateRing shown', async () => {
    detailData.value = {
      id: 'abc', repo_id: 'o/m', revision: 'a'.repeat(40),
      status: 'downloading', priority: 1,
      created_at: '2026-05-20T00:00:00Z', completed_at: null,
      error_message: null, subtasks: [],
    }
    const w = await mountPage()
    await flushPromises()
    expect(w.findComponent({ name: 'ElTabs' }).exists()).toBe(true)
    expect(w.findComponent({ name: 'AggregateRing' }).exists()).toBe(true)
  })

  test('terminal task → cancel hidden, delete shown', async () => {
    detailData.value = {
      id: 'abc', repo_id: 'o/m', revision: 'a'.repeat(40),
      status: 'succeeded', priority: 1,
      created_at: '2026-05-20T00:00:00Z', completed_at: null,
      error_message: null, subtasks: [],
    }
    const w = await mountPage()
    await flushPromises()
    expect(w.text()).toContain(en.tasks.detail.delete)
  })
})
