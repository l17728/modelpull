import { describe, expect, test } from 'vitest'
import { eventLevel } from '@/components/taskdetail/eventLevel'

describe('eventLevel', () => {
  test('denied / failed → error', () => {
    expect(eventLevel('task.denied', 'task.denied (denied)')).toBe('error')
    expect(eventLevel('subtask.failed', 'subtask.failed')).toBe('error')
  })
  test('quota / paused / retry → warn', () => {
    expect(eventLevel('quota.exceeded', 'quota.exceeded')).toBe('warn')
    expect(eventLevel('subtask.paused_external', 'x')).toBe('warn')
  })
  test('default → info', () => {
    expect(eventLevel('task.created', 'task.created')).toBe('info')
  })
})
