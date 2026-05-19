import { describe, expect, test } from 'vitest'
import { canCancel, canDelete } from '@/composables/useTaskMutations'

describe('task action guards', () => {
  test('canCancel: only non-terminal', () => {
    expect(canCancel('downloading')).toBe(true)
    expect(canCancel('pending')).toBe(true)
    expect(canCancel('succeeded')).toBe(false)
    expect(canCancel('failed')).toBe(false)
    expect(canCancel('cancelled')).toBe(false)
  })
  test('canDelete: only terminal', () => {
    expect(canDelete('succeeded')).toBe(true)
    expect(canDelete('failed')).toBe(true)
    expect(canDelete('cancelled')).toBe(true)
    expect(canDelete('downloading')).toBe(false)
  })
})
