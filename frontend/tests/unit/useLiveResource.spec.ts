import { describe, expect, test } from 'vitest'
import { computeInterval } from '@/composables/useLiveResource'

describe('computeInterval', () => {
  const base = 2000
  test('active + visible → base', () => {
    expect(computeInterval({ base, terminal: false, hidden: false, errored: false })).toBe(2000)
  })
  test('terminal → false (stop)', () => {
    expect(computeInterval({ base, terminal: true, hidden: false, errored: false })).toBe(false)
  })
  test('hidden → base × 3', () => {
    expect(computeInterval({ base, terminal: false, hidden: true, errored: false })).toBe(6000)
  })
  test('errored (no data) → 5000 backoff', () => {
    expect(computeInterval({ base, terminal: false, hidden: false, errored: true })).toBe(5000)
  })
  test('terminal beats hidden/errored', () => {
    expect(computeInterval({ base, terminal: true, hidden: true, errored: true })).toBe(false)
  })
})
