import { describe, expect, test } from 'vitest'
import { ringDash } from '@/components/taskdetail/ringMath'

describe('ringDash', () => {
  const C = 100
  test('0% → no fill', () => {
    expect(ringDash(0, C)).toBe('0 100')
  })
  test('50% → half', () => {
    expect(ringDash(50, C)).toBe('50 50')
  })
  test('clamps over/under', () => {
    expect(ringDash(150, C)).toBe('100 0')
    expect(ringDash(-5, C)).toBe('0 100')
  })
})
