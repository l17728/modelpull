import { beforeEach, describe, expect, test, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { createI18n } from 'vue-i18n'
import en from '@/locale/en-US.json'

const { jobsData, refetch } = vi.hoisted(() => ({
  jobsData: { value: null as unknown },
  refetch: vi.fn(),
}))
const { post } = vi.hoisted(() => ({ post: vi.fn() }))

vi.mock('@/composables/useReplicationJobs', async () => {
  const { ref } = await import('vue')
  return {
    useReplicationJobs: () => ({
      data: ref(jobsData.value),
      isLoading: ref(false),
      isError: ref(false),
      refetch,
    }),
  }
})

vi.mock('@/api/client', () => ({
  client: { post },
}))

// ElMessageBox.confirm and ElMessage will be ambient via ElementPlus —
// stub their side effects so we don't open real toasts/modals in jsdom.
vi.mock('element-plus', async () => {
  const actual = await vi.importActual<typeof import('element-plus')>(
    'element-plus')
  return {
    ...actual,
    ElMessageBox: { ...actual.ElMessageBox,
                     confirm: vi.fn().mockResolvedValue('confirm') },
    ElMessage: { ...actual.ElMessage,
                  success: vi.fn(), error: vi.fn(), warning: vi.fn() },
  }
})

const i18n = createI18n({
  legacy: false, locale: 'en-US', messages: { 'en-US': en },
})

function mountPage() {
  return import('@/pages/Replication.vue').then((m) =>
    mount(m.default, { global: { plugins: [ElementPlus, i18n] } }))
}

const sampleJob = {
  id: 1, tenant_id: 1, source_object_id: 11, target_storage_id: 2,
  status: 'pending', created_at: '2026-05-27T00:00:00Z',
  started_at: null, completed_at: null,
  bytes_transferred: 0, retry_count: 0, error_message: null,
}

const succeededJob = { ...sampleJob, id: 2, status: 'succeeded',
                       bytes_transferred: 1024 * 1024 * 2, // 2 MB
                       completed_at: '2026-05-27T00:01:00Z' }

describe('Replication page', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    jobsData.value = null
    refetch.mockReset()
    post.mockReset()
  })

  test('empty data → shows empty state', async () => {
    jobsData.value = { items: [] }
    const w = await mountPage()
    await flushPromises()
    expect(w.text()).toContain(en.replication.empty)
  })

  test('renders job rows', async () => {
    jobsData.value = { items: [sampleJob, succeededJob] }
    const w = await mountPage()
    await flushPromises()
    const text = w.text()
    expect(text).toContain('pending')
    expect(text).toContain('succeeded')
    expect(text).toContain('2.0 MB')  // formatBytes formats 2MB as "2.0 MB"
  })

  test('cancel button posts to /cancel and refetches', async () => {
    jobsData.value = { items: [sampleJob] }
    post.mockResolvedValueOnce({ data: { ...sampleJob, status: 'cancelled' } })
    const w = await mountPage()
    await flushPromises()
    const cancelBtn = w.find('[data-test="replication-cancel"]')
    expect(cancelBtn.exists()).toBe(true)
    await cancelBtn.trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/v1/replication/1/cancel')
    expect(refetch).toHaveBeenCalled()
  })

  test('terminal jobs hide cancel button', async () => {
    jobsData.value = { items: [succeededJob] }
    const w = await mountPage()
    await flushPromises()
    expect(w.find('[data-test="replication-cancel"]').exists()).toBe(false)
  })

  test('create dialog opens then posts', async () => {
    jobsData.value = { items: [] }
    post.mockResolvedValueOnce({ data: sampleJob })
    const w = await mountPage()
    await flushPromises()
    await w.find('[data-test="replication-create-button"]').trigger('click')
    await flushPromises()
    // el-input-number renders an <input>; set values directly on the model
    // via the dialog's exposed form refs instead.
    const vm = w.vm as unknown as {
      createForm: { source_object_id: number; target_storage_id: number }
    }
    vm.createForm.source_object_id = 11
    vm.createForm.target_storage_id = 2
    await w.find('[data-test="replication-create-submit"]').trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/v1/replication',
                                       { source_object_id: 11,
                                         target_storage_id: 2 })
  })
})
