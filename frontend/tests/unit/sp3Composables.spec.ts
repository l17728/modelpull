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

import { useExecutors } from '@/composables/useExecutors'
import { useAuditLog, fetchOlderAudit } from '@/composables/useAuditLog'
import { useSystemHealth } from '@/composables/useSystemHealth'

describe('SP3 live composables', () => {
  test('useExecutors wires key + status query + interval', async () => {
    captured.length = 0
    get.mockResolvedValueOnce({ data: { items: [] } })
    const status = ref<string | null>(null)
    const q = useExecutors(status) as unknown as {
      __fetcher: () => Promise<unknown>
    }
    const last = captured[captured.length - 1]
    expect(last?.key).toEqual(['executors', status])
    expect((last?.opts as { baseIntervalMs: number }).baseIntervalMs).toBe(5_000)
    await q.__fetcher()
    expect(get).toHaveBeenCalledWith('/api/v1/executors')
    captured.length = 0
    status.value = 'healthy'
    get.mockResolvedValueOnce({ data: { items: [] } })
    const q2 = useExecutors(status) as unknown as {
      __fetcher: () => Promise<unknown>
    }
    await q2.__fetcher()
    expect(get).toHaveBeenCalledWith('/api/v1/executors?status=healthy')
  })

  test('useAuditLog builds query from filters', async () => {
    captured.length = 0
    get.mockResolvedValueOnce(
      { data: { items: [], next_cursor: null } })
    const filters = {
      action: ref('task.'),
      actor: ref<number | null>(42),
      from: ref<string | null>(null),
      to: ref<string | null>(null),
    }
    const q = useAuditLog(filters) as unknown as {
      __fetcher: () => Promise<unknown>
    }
    await q.__fetcher()
    const call = get.mock.calls[get.mock.calls.length - 1] as
      [string, ...unknown[]] | undefined
    const url = call?.[0] ?? ''
    expect(url).toContain('/api/v1/audit/log')
    expect(url).toContain('limit=50')
    expect(url).toContain('action=task.')
    expect(url).toContain('actor_user_id=42')
  })

  test('fetchOlderAudit appends cursor', async () => {
    get.mockResolvedValueOnce(
      { data: { items: [], next_cursor: null } })
    await fetchOlderAudit({ action: 'x', actor: null, from: null, to: null },
                          'CURSOR')
    const call = get.mock.calls[get.mock.calls.length - 1] as
      [string, ...unknown[]] | undefined
    const url = call?.[0] ?? ''
    expect(url).toContain('cursor=CURSOR')
    expect(url).toContain('action=x')
  })

  test('useSystemHealth hits /health/active', async () => {
    captured.length = 0
    get.mockResolvedValueOnce(
      { data: { status: 'active', controller_state: 'active' } })
    const q = useSystemHealth() as unknown as {
      __fetcher: () => Promise<unknown>
    }
    await q.__fetcher()
    expect(get).toHaveBeenCalledWith('/health/active')
    const last = captured[captured.length - 1]
    expect((last?.opts as { baseIntervalMs: number }).baseIntervalMs).toBe(10_000)
  })
})
