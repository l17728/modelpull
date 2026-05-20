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

import { useAuditLog } from '@/composables/useAuditLog'

const mkFilters = () => ({
  action: ref(''),
  actor: ref<number | null>(null),
  from: ref<string | null>(null),
  to: ref<string | null>(null),
})

describe('useAuditLog SSE opt-in (SP5d)', () => {
  test('passes streamUrl + applyEvent to the seam', () => {
    captured.length = 0
    useAuditLog(mkFilters())
    const last = captured[captured.length - 1]
    expect(last?.opts.streamUrl).toBeDefined()
    expect(typeof last?.opts.applyEvent).toBe('function')
    expect(last?.opts.baseIntervalMs).toBe(10_000)
  })
  test('streamUrl is reactive to filters', () => {
    captured.length = 0
    const f = mkFilters()
    useAuditLog(f)
    const last = captured[captured.length - 1]
    const url0 = (last?.opts.streamUrl as { value: string }).value
    expect(url0).toBe('/api/v1/audit/log/stream')
    f.action.value = 'task.'
    const url1 = (last?.opts.streamUrl as { value: string }).value
    expect(url1).toBe('/api/v1/audit/log/stream?action=task.')
    f.actor.value = 42
    const url2 = (last?.opts.streamUrl as { value: string }).value
    expect(url2).toContain('action=task.')
    expect(url2).toContain('actor_user_id=42')
  })
  test('applyEvent parses ev.data as JSON', () => {
    captured.length = 0
    useAuditLog(mkFilters())
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
