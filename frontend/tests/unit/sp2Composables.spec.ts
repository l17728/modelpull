import { describe, expect, test, vi } from 'vitest'
import { ref } from 'vue'

const { get } = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('@/api/client', () => ({ client: { get } }))

const { captured } = vi.hoisted(() => ({
  captured: [] as Array<{ key: unknown; opts: unknown }>,
}))
vi.mock('@/composables/useLiveResource', () => ({
  useLiveResource: (key: unknown, fetcher: () => unknown, opts: unknown) => {
    captured.push({ key, opts })
    return { __fetcher: fetcher }
  },
}))

import { useSubtaskChunks } from '@/composables/useSubtaskChunks'
import { useTaskEvents } from '@/composables/useTaskEvents'

describe('SP2 live composables', () => {
  test('useSubtaskChunks wires key, path, enabled, terminal', async () => {
    captured.length = 0
    get.mockResolvedValueOnce({ data: { items: [] } })
    const id = ref('abc')
    const enabled = ref(true)
    const terminal = ref(false)
    const q = useSubtaskChunks(id, enabled, terminal) as unknown as {
      __fetcher: () => Promise<unknown>
    }
    const last = captured[captured.length - 1]
    expect(last?.key).toEqual(['task-chunks', id])
    expect((last?.opts as { enabled: unknown }).enabled).toBe(enabled)
    await q.__fetcher()
    expect(get).toHaveBeenCalledWith('/api/v1/tasks/abc/subtask-chunks')
  })

  test('useTaskEvents path + limit', async () => {
    captured.length = 0
    get.mockResolvedValueOnce({ data: { items: [], next_cursor: null } })
    const id = ref('xyz')
    const q = useTaskEvents(id, ref(true), ref(false)) as unknown as {
      __fetcher: () => Promise<unknown>
    }
    await q.__fetcher()
    expect(get).toHaveBeenCalledWith('/api/v1/tasks/xyz/events?limit=50')
  })
})
