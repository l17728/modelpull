import { describe, expect, test } from 'vitest'
import { ref } from 'vue'
import { shouldStream } from '@/api/sse'

describe('shouldStream', () => {
  test('no streamUrl → false', () => {
    expect(shouldStream({ streamUrl: undefined, applyEvent: () => 1,
      enabled: true })).toBe(false)
  })
  test('no applyEvent → false', () => {
    expect(shouldStream({ streamUrl: '/x', applyEvent: undefined,
      enabled: true })).toBe(false)
  })
  test('enabled: false → false', () => {
    expect(shouldStream({ streamUrl: '/x', applyEvent: () => 1,
      enabled: false })).toBe(false)
  })
  test('enabled: Ref<false> → false (unwrapped)', () => {
    expect(shouldStream({ streamUrl: '/x', applyEvent: () => 1,
      enabled: ref(false) })).toBe(false)
  })
  test('all conditions met → true', () => {
    expect(shouldStream({ streamUrl: '/x', applyEvent: () => 1,
      enabled: true })).toBe(true)
  })
  test('enabled: Ref<true> → true', () => {
    expect(shouldStream({ streamUrl: '/x', applyEvent: () => 1,
      enabled: ref(true) })).toBe(true)
  })
  test('enabled: undefined defaults to true', () => {
    expect(shouldStream({ streamUrl: '/x', applyEvent: () => 1 })).toBe(true)
  })
})
