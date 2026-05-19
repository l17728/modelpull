import { describe, expect, test } from 'vitest'
import { computeRate } from '@/composables/useDownloadRate'

describe('computeRate', () => {
  test('empty / single sample → zero rate, null eta', () => {
    expect(computeRate([], 100)).toEqual({
      currentBps: 0, avgBps: 0, etaSeconds: null,
    })
    expect(computeRate([{ t: 0, bytes: 10 }], 100)).toEqual({
      currentBps: 0, avgBps: 0, etaSeconds: null,
    })
  })
  test('linear progress → rate + eta', () => {
    const r = computeRate(
      [{ t: 0, bytes: 0 }, { t: 1000, bytes: 100 },
        { t: 2000, bytes: 200 }], 400)
    expect(r.avgBps).toBeCloseTo(100, 5)
    expect(r.currentBps).toBeGreaterThan(0)
    expect(r.etaSeconds).not.toBeNull()
    expect(r.etaSeconds as number).toBeGreaterThan(0)
  })
  test('no total → null eta but rate still computed', () => {
    const r = computeRate(
      [{ t: 0, bytes: 0 }, { t: 1000, bytes: 50 }], null)
    expect(r.avgBps).toBeCloseTo(50, 5)
    expect(r.etaSeconds).toBeNull()
  })
})
