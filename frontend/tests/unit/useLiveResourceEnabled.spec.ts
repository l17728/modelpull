import { describe, expect, test } from 'vitest'
import type { LiveOptions } from '@/composables/useLiveResource'
import { computeInterval } from '@/composables/useLiveResource'

describe('useLiveResource enabled option', () => {
  test('LiveOptions accepts enabled: boolean', () => {
    const o: LiveOptions<number> = { baseIntervalMs: 1000, enabled: false }
    expect(o.enabled).toBe(false)
  })
  test('computeInterval still pure & unchanged', () => {
    expect(computeInterval({
      base: 1000, terminal: false, hidden: false, errored: false,
    })).toBe(1000)
  })
})
