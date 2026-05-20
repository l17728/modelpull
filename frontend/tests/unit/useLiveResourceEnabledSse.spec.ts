import { describe, expect, test, vi } from 'vitest'
import { ref, nextTick, defineComponent, type Ref } from 'vue'
import { mount } from '@vue/test-utils'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { createPinia, setActivePinia } from 'pinia'

const { streamSseMock } = vi.hoisted(() => ({
  streamSseMock: vi.fn((_opts: { url: string }) => new Promise(() => {})),
}))
vi.mock('@/api/sse', async () => {
  const actual = await vi.importActual<typeof import('@/api/sse')>('@/api/sse')
  return { ...actual, streamSse: streamSseMock }
})

import { useLiveResource } from '@/composables/useLiveResource'

function mountWith(enabled: Ref<boolean>) {
  setActivePinia(createPinia())
  const Comp = defineComponent({
    setup() {
      const q = useLiveResource<{ v: number }>(
        ['k'],
        async () => ({ v: 1 }),
        {
          baseIntervalMs: 5_000,
          enabled,
          streamUrl: '/api/v1/stream',
          applyEvent: (_p, ev) => JSON.parse(ev.data),
        },
      )
      return { q }
    },
    template: '<div>{{ q.data?.v }}</div>',
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return mount(Comp, {
    global: { plugins: [[VueQueryPlugin, { queryClient: qc }]] },
  })
}

describe('useLiveResource seam — enabled flips true (SP5f regression)', () => {
  test('enabled=false at mount: streamSse NOT called', async () => {
    streamSseMock.mockClear()
    const enabled = ref(false)
    const w = mountWith(enabled)
    await nextTick()
    await new Promise((r) => setTimeout(r, 50))
    expect(streamSseMock).not.toHaveBeenCalled()
    w.unmount()
  })
  test('enabled flips false→true: streamSse called once data arrives', async () => {
    streamSseMock.mockClear()
    const enabled = ref(false)
    const w = mountWith(enabled)
    await nextTick()
    enabled.value = true
    for (let i = 0; i < 20 && streamSseMock.mock.calls.length === 0; i++) {
      await new Promise((r) => setTimeout(r, 20))
    }
    expect(streamSseMock).toHaveBeenCalledTimes(1)
    expect(streamSseMock.mock.calls[0]?.[0]?.url).toBe('/api/v1/stream')
    w.unmount()
  })
  test('enabled=true at mount: streamSse called once data arrives (no regression)', async () => {
    streamSseMock.mockClear()
    const enabled = ref(true)
    const w = mountWith(enabled)
    for (let i = 0; i < 20 && streamSseMock.mock.calls.length === 0; i++) {
      await new Promise((r) => setTimeout(r, 20))
    }
    expect(streamSseMock).toHaveBeenCalledTimes(1)
    w.unmount()
  })
})
