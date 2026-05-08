import { describe, expect, test } from 'vitest'

import { computeRefetchInterval } from '@/composables/useTaskDetail'
import type { TaskDetail } from '@/api/types'

function fakeTask(status: TaskDetail['status']): TaskDetail {
  return {
    id: 'x',
    repo_id: 'o/r',
    revision: 'a',
    status,
    priority: 1,
    created_at: '2026-01-01T00:00:00Z',
    completed_at: null,
    error_message: null,
    subtasks: [],
  }
}

describe('computeRefetchInterval', () => {
  test('non-terminal status → 1000ms', () => {
    expect(computeRefetchInterval(fakeTask('downloading'), 'success')).toBe(1000)
    expect(computeRefetchInterval(fakeTask('queued'), 'success')).toBe(1000)
    expect(computeRefetchInterval(fakeTask('scheduling'), 'success')).toBe(1000)
    expect(computeRefetchInterval(fakeTask('pending'), 'success')).toBe(1000)
  })

  test('terminal status → false (stop polling)', () => {
    expect(computeRefetchInterval(fakeTask('succeeded'), 'success')).toBe(false)
    expect(computeRefetchInterval(fakeTask('failed'), 'success')).toBe(false)
    expect(computeRefetchInterval(fakeTask('cancelled'), 'success')).toBe(false)
  })

  test('undefined data + pending status (first fetch in flight) → 1000ms', () => {
    expect(computeRefetchInterval(undefined, 'pending')).toBe(1000)
  })

  test('undefined data + error status → 5000ms backoff', () => {
    // 5xx / network error during initial fetch — back off so we don't hammer
    // a struggling backend at 1 req/sec.
    expect(computeRefetchInterval(undefined, 'error')).toBe(5000)
  })
})
