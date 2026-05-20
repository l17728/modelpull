import { describe, expect, test, vi } from 'vitest'

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

import { useTaskList } from '@/composables/useTaskList'

describe('useTaskList SSE opt-in (SP5c)', () => {
  test('passes streamUrl + applyEvent to the seam', () => {
    captured.length = 0
    useTaskList()
    const last = captured[captured.length - 1]
    expect(last?.opts.streamUrl).toBe('/api/v1/tasks/stream')
    expect(typeof last?.opts.applyEvent).toBe('function')
    expect(last?.opts.baseIntervalMs).toBe(5_000)
    expect(last?.opts.staleTime).toBe(5_000)
  })
  test('applyEvent parses ev.data as JSON', () => {
    captured.length = 0
    useTaskList()
    const apply = (captured[captured.length - 1]?.opts.applyEvent) as
      (prev: unknown, ev: { data: string }) => { items: unknown[]; total: number }
    const out = apply(undefined, {
      data: '{"items":[{"id":"x"}],"total":1}',
    })
    expect(out.items).toEqual([{ id: 'x' }])
    expect(out.total).toBe(1)
  })
})
