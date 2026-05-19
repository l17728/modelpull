import { describe, expect, test } from 'vitest'
import { aggregateKpis, bucket24h } from '@/dashboard/aggregate'
import type { TaskRead } from '@/api/types'

const mk = (status: TaskRead['status'], createdAt: string): TaskRead => ({
  id: Math.random().toString(36), repo_id: 'o/r', revision: 'a',
  status, priority: 1, created_at: createdAt, completed_at: null,
  error_message: null,
})

describe('dashboard aggregate', () => {
  test('aggregateKpis counts by bucket', () => {
    const k = aggregateKpis([
      mk('downloading', '2026-05-19T00:00:00Z'),
      mk('scheduling', '2026-05-19T00:00:00Z'),
      mk('succeeded', '2026-05-19T00:00:00Z'),
      mk('failed', '2026-05-19T00:00:00Z'),
    ])
    expect(k).toEqual({ inProgress: 2, completed: 1, failed: 1, total: 4 })
  })
  test('bucket24h returns 24 hourly counts within window', () => {
    const now = new Date('2026-05-19T12:00:00Z')
    const b = bucket24h([
      mk('succeeded', '2026-05-19T11:30:00Z'),
      mk('succeeded', '2026-05-19T11:45:00Z'),
      mk('succeeded', '2026-05-10T00:00:00Z'),
    ], now)
    expect(b).toHaveLength(24)
    expect(b.reduce((a, c) => a + c, 0)).toBe(2)
  })
})
