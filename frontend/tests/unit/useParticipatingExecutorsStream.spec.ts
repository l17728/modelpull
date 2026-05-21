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

import { useParticipatingExecutors } from '@/composables/useParticipatingExecutors'

describe('useParticipatingExecutors SSE opt-in (SP5i)', () => {
  test('passes streamUrl + applyEvent + enabled + isTerminal to the seam', () => {
    captured.length = 0
    const taskId = ref('aaa')
    const enabled = ref(true)
    const terminal = ref(false)
    useParticipatingExecutors(taskId, enabled, terminal)
    const last = captured[captured.length - 1]
    expect((last?.opts.streamUrl as { value: string }).value)
      .toBe('/api/v1/tasks/aaa/participating-executors/stream')
    expect(typeof last?.opts.applyEvent).toBe('function')
    expect(last?.opts.baseIntervalMs).toBe(2_000)
    expect(last?.opts.enabled).toBe(enabled)
    expect(typeof last?.opts.isTerminal).toBe('function')
  })
  test('streamUrl is reactive to taskId ref', () => {
    captured.length = 0
    const taskId = ref('bbb')
    useParticipatingExecutors(taskId, ref(true), ref(false))
    const last = captured[captured.length - 1]
    expect((last?.opts.streamUrl as { value: string }).value)
      .toBe('/api/v1/tasks/bbb/participating-executors/stream')
    taskId.value = 'ccc'
    expect((last?.opts.streamUrl as { value: string }).value)
      .toBe('/api/v1/tasks/ccc/participating-executors/stream')
  })
  test('applyEvent parses ev.data as JSON', () => {
    captured.length = 0
    useParticipatingExecutors(ref('xxx'), ref(true), ref(false))
    const apply = (captured[captured.length - 1]?.opts.applyEvent) as
      (prev: unknown, ev: { data: string }) => { items: unknown[] }
    const out = apply(undefined, { data: '{"items":[{"executor_id":"e1"}]}' })
    expect(out.items).toEqual([{ executor_id: 'e1' }])
  })
})
