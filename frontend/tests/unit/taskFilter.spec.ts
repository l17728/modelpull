import { describe, expect, test } from 'vitest'
import { filterTasks } from '@/tasks/filter'
import type { TaskRead } from '@/api/types'

const mk = (id: string, repo: string, status: TaskRead['status']): TaskRead => ({
  id, repo_id: repo, revision: 'abc', status, priority: 1,
  created_at: '2026-05-19T00:00:00Z', completed_at: null, error_message: null,
})
const items = [
  mk('aaaa1111', 'org/alpha', 'downloading'),
  mk('bbbb2222', 'org/beta', 'succeeded'),
]

describe('filterTasks', () => {
  test('no filter → all', () => {
    expect(filterTasks(items, { status: '', q: '' })).toHaveLength(2)
  })
  test('status filter', () => {
    expect(filterTasks(items, { status: 'succeeded', q: '' }).map((t) => t.id))
      .toEqual(['bbbb2222'])
  })
  test('q matches repo or id (case-insensitive)', () => {
    expect(filterTasks(items, { status: '', q: 'ALPHA' }).map((t) => t.id))
      .toEqual(['aaaa1111'])
    expect(filterTasks(items, { status: '', q: 'bbbb' }).map((t) => t.id))
      .toEqual(['bbbb2222'])
  })
})
