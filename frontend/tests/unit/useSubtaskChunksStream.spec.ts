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

import { useSubtaskChunks } from '@/composables/useSubtaskChunks'

describe('useSubtaskChunks SSE opt-in (SP5g)', () => {
  test('passes streamUrl + applyEvent + enabled + isTerminal to the seam', () => {
    captured.length = 0
    const taskId = ref('aaa')
    const enabled = ref(true)
    const terminal = ref(false)
    useSubtaskChunks(taskId, enabled, terminal)
    const last = captured[captured.length - 1]
    expect((last?.opts.streamUrl as { value: string }).value)
      .toBe('/api/v1/tasks/aaa/subtask-chunks/stream')
    expect(typeof last?.opts.applyEvent).toBe('function')
    expect(last?.opts.baseIntervalMs).toBe(1_500)
    expect(last?.opts.enabled).toBe(enabled)
    expect(typeof last?.opts.isTerminal).toBe('function')
  })
  test('streamUrl is reactive to taskId ref', () => {
    captured.length = 0
    const taskId = ref('bbb')
    useSubtaskChunks(taskId, ref(true), ref(false))
    const last = captured[captured.length - 1]
    expect((last?.opts.streamUrl as { value: string }).value)
      .toBe('/api/v1/tasks/bbb/subtask-chunks/stream')
    taskId.value = 'ccc'
    expect((last?.opts.streamUrl as { value: string }).value)
      .toBe('/api/v1/tasks/ccc/subtask-chunks/stream')
  })
  test('applyEvent parses ev.data as JSON', () => {
    captured.length = 0
    useSubtaskChunks(ref('xxx'), ref(true), ref(false))
    const apply = (captured[captured.length - 1]?.opts.applyEvent) as
      (prev: unknown, ev: { data: string }) => { items: unknown[] }
    const out = apply(undefined, { data: '{"items":[{"filename":"a"}]}' })
    expect(out.items).toEqual([{ filename: 'a' }])
  })
})
