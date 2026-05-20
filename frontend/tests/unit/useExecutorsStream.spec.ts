import { describe, expect, test, vi } from 'vitest'
import { ref } from 'vue'

const { captured } = vi.hoisted(() => ({
  captured: [] as Array<{ key: unknown; opts: Record<string, unknown> }>,
}))
vi.mock('@/composables/useLiveResource', () => ({
  useLiveResource: (key: unknown, fetcher: () => unknown,
                   opts: Record<string, unknown>) => {
    captured.push({ key, opts })
    return { __fetcher: fetcher }
  },
}))
vi.mock('@/api/client', () => ({ client: { get: vi.fn() } }))

import { useExecutors } from '@/composables/useExecutors'

describe('useExecutors SSE opt-in (SP5b)', () => {
  test('passes streamUrl + applyEvent to the seam', () => {
    captured.length = 0
    const status = ref<string | null>(null)
    useExecutors(status)
    const last = captured[captured.length - 1]
    expect(last?.opts.streamUrl).toBeDefined()
    expect(typeof last?.opts.applyEvent).toBe('function')
    expect(last?.opts.baseIntervalMs).toBe(5_000)
  })
  test('streamUrl is reactive to the status filter', () => {
    captured.length = 0
    const status = ref<string | null>('healthy')
    useExecutors(status)
    const last = captured[captured.length - 1]
    const url = (last?.opts.streamUrl as { value: string }).value
    expect(url).toBe('/api/v1/executors/stream?status=healthy')
    status.value = null
    const url2 = (last?.opts.streamUrl as { value: string }).value
    expect(url2).toBe('/api/v1/executors/stream')
  })
  test('applyEvent parses ev.data as JSON', () => {
    captured.length = 0
    useExecutors(ref<string | null>(null))
    const apply = (captured[captured.length - 1]?.opts.applyEvent) as
      (prev: unknown, ev: { data: string }) => { items: unknown[] }
    const out = apply(undefined, { data: '{"items":[{"id":"x"}]}' })
    expect(out.items).toEqual([{ id: 'x' }])
  })
})
