import { describe, expect, test } from 'vitest'
import { computeInterval } from '@/composables/useLiveResource'

describe('task-detail polling via computeInterval', () => {
  test('non-terminal → 1000', () => {
    expect(computeInterval({ base: 1000, terminal: false, hidden: false, errored: false })).toBe(1000)
  })
  test('terminal → false', () => {
    expect(computeInterval({ base: 1000, terminal: true, hidden: false, errored: false })).toBe(false)
  })
  test('errored first fetch → 5000', () => {
    expect(computeInterval({ base: 1000, terminal: false, hidden: false, errored: true })).toBe(5000)
  })
})
