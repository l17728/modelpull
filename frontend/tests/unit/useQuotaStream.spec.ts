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

import { useQuota } from '@/composables/useQuota'

describe('useQuota SSE opt-in (SP5e)', () => {
  test('passes streamUrl + applyEvent to the seam', () => {
    captured.length = 0
    useQuota()
    const last = captured[captured.length - 1]
    expect(last?.opts.streamUrl).toBe('/api/v1/quota/current/stream')
    expect(typeof last?.opts.applyEvent).toBe('function')
    expect(last?.opts.baseIntervalMs).toBe(30_000)
    expect(last?.opts.staleTime).toBe(30_000)
  })
  test('applyEvent parses ev.data as JSON', () => {
    captured.length = 0
    useQuota()
    const apply = (captured[captured.length - 1]?.opts.applyEvent) as
      (prev: unknown, ev: { data: string }) => Record<string, unknown>
    const out = apply(undefined, {
      data: '{"tenant_id":1,"bytes_used_month":42,"bytes_quota_month":1000}',
    })
    expect(out.tenant_id).toBe(1)
    expect(out.bytes_used_month).toBe(42)
  })
  test('key stays ["quota"] (no filter axes)', () => {
    captured.length = 0
    useQuota()
    const last = captured[captured.length - 1]
    expect(last?.key).toEqual(['quota'])
  })
})
