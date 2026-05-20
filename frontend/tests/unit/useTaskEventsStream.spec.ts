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

import { useTaskEvents } from '@/composables/useTaskEvents'

describe('useTaskEvents SSE opt-in (SP5f)', () => {
  test('passes streamUrl + applyEvent + enabled + isTerminal to the seam', () => {
    captured.length = 0
    const taskId = ref('aaa')
    const enabled = ref(true)
    const terminal = ref(false)
    useTaskEvents(taskId, enabled, terminal)
    const last = captured[captured.length - 1]
    expect((last?.opts.streamUrl as { value: string }).value)
      .toBe('/api/v1/tasks/aaa/events/stream')
    expect(typeof last?.opts.applyEvent).toBe('function')
    expect(last?.opts.baseIntervalMs).toBe(5_000)
    expect(last?.opts.enabled).toBe(enabled)
    expect(typeof last?.opts.isTerminal).toBe('function')
  })
  test('streamUrl is reactive to taskId ref', () => {
    captured.length = 0
    const taskId = ref('bbb')
    useTaskEvents(taskId, ref(true), ref(false))
    const last = captured[captured.length - 1]
    const url0 = (last?.opts.streamUrl as { value: string }).value
    expect(url0).toBe('/api/v1/tasks/bbb/events/stream')
    taskId.value = 'ccc'
    const url1 = (last?.opts.streamUrl as { value: string }).value
    expect(url1).toBe('/api/v1/tasks/ccc/events/stream')
  })
  test('applyEvent parses ev.data as JSON', () => {
    captured.length = 0
    useTaskEvents(ref('xxx'), ref(true), ref(false))
    const apply = (captured[captured.length - 1]?.opts.applyEvent) as
      (prev: unknown, ev: { data: string }) =>
        { items: unknown[]; next_cursor: string | null }
    const out = apply(undefined, {
      data: '{"items":[{"id":1}],"next_cursor":null}',
    })
    expect(out.items).toEqual([{ id: 1 }])
    expect(out.next_cursor).toBeNull()
  })
})
