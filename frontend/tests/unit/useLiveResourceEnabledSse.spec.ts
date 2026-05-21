import { describe, expect, test, vi } from 'vitest'
import { ref, nextTick, defineComponent, type Ref } from 'vue'
import { mount } from '@vue/test-utils'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { createPinia, setActivePinia } from 'pinia'

const { streamSseMock, signals } = vi.hoisted(() => ({
  streamSseMock: vi.fn((_opts: { url: string; signal: AbortSignal }) =>
    new Promise<void>(() => {})),
  signals: [] as AbortSignal[],
}))
vi.mock('@/api/sse', async () => {
  const actual = await vi.importActual<typeof import('@/api/sse')>('@/api/sse')
  return {
    ...actual,
    streamSse: (o: { url: string; signal: AbortSignal }) => {
      signals.push(o.signal)
      return streamSseMock(o)
    },
  }
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

async function waitForStream(target: number) {
  for (let i = 0; i < 30 && streamSseMock.mock.calls.length < target; i++) {
    await new Promise((r) => setTimeout(r, 20))
  }
}

describe('useLiveResource seam — close-on-disable lifecycle (SP5i)', () => {
  test('enabled=false at mount: streamSse NOT called', async () => {
    streamSseMock.mockClear(); signals.length = 0
    const enabled = ref(false)
    const w = mountWith(enabled)
    await nextTick()
    await new Promise((r) => setTimeout(r, 50))
    expect(streamSseMock).not.toHaveBeenCalled()
    w.unmount()
  })
  test('enabled flips false→true: streamSse called once', async () => {
    streamSseMock.mockClear(); signals.length = 0
    const enabled = ref(false)
    const w = mountWith(enabled)
    await nextTick()
    enabled.value = true
    await waitForStream(1)
    expect(streamSseMock).toHaveBeenCalledTimes(1)
    w.unmount()
  })
  test('enabled=true at mount: streamSse called once (always-on path)', async () => {
    streamSseMock.mockClear(); signals.length = 0
    const enabled = ref(true)
    const w = mountWith(enabled)
    await waitForStream(1)
    expect(streamSseMock).toHaveBeenCalledTimes(1)
    w.unmount()
  })
  test('enabled true→false: the open stream is aborted', async () => {
    streamSseMock.mockClear(); signals.length = 0
    const enabled = ref(true)
    const w = mountWith(enabled)
    await waitForStream(1)
    expect(signals[0]?.aborted).toBe(false)
    enabled.value = false
    await nextTick()
    await new Promise((r) => setTimeout(r, 20))
    expect(signals[0]?.aborted).toBe(true)
    w.unmount()
  })
  test('enabled true→false→true: streamSse called twice (open, close, reopen)', async () => {
    streamSseMock.mockClear(); signals.length = 0
    const enabled = ref(true)
    const w = mountWith(enabled)
    await waitForStream(1)
    enabled.value = false
    await nextTick()
    await new Promise((r) => setTimeout(r, 20))
    enabled.value = true
    await waitForStream(2)
    expect(streamSseMock).toHaveBeenCalledTimes(2)
    expect(signals[0]?.aborted).toBe(true)   // first stream was closed
    expect(signals[1]?.aborted).toBe(false)  // reopened stream is live
    w.unmount()
  })
  test('self-abort does NOT trigger giveup (identity guard): abort-resolving mock, reopen succeeds', async () => {
    // The default mock never resolves, so the .then identity guard is never
    // exercised. Here streamSse RESOLVES when its signal aborts (mirroring
    // sse.ts:94). A correct `if (ac === controller)` guard skips the giveup
    // branch on self-abort → gaveUp stays false → reopen succeeds (2 calls).
    // A broken guard would set gaveUp=true after closeStream, blocking the
    // reopen → only 1 call → this test fails.
    streamSseMock.mockClear(); signals.length = 0
    streamSseMock.mockImplementation(({ signal }) =>
      new Promise<void>((resolve) => {
        signal.addEventListener('abort', () => resolve(), { once: true })
      }))
    const enabled = ref(true)
    const w = mountWith(enabled)
    await waitForStream(1)
    enabled.value = false
    await nextTick()
    // Let the aborted stream's promise resolve and its .then microtask run.
    await new Promise((r) => setTimeout(r, 40))
    enabled.value = true
    await waitForStream(2)
    expect(streamSseMock).toHaveBeenCalledTimes(2)
    streamSseMock.mockReset()
    streamSseMock.mockImplementation(() => new Promise<void>(() => {}))
    w.unmount()
  })
})
