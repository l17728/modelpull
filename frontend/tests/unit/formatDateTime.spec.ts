import { describe, expect, test } from 'vitest'
import { formatDateTime } from '@/utils/format'

describe('formatDateTime', () => {
  test('null → em-dash', () => {
    expect(formatDateTime(null)).toBe('—')
    expect(formatDateTime(undefined)).toBe('—')
  })
  test('valid ISO → locale string (not the raw ISO)', () => {
    const out = formatDateTime('2026-05-20T12:00:00Z')
    expect(out).not.toBe('—')
    expect(out).not.toBe('2026-05-20T12:00:00Z')
  })
  test('invalid → falls back to the input', () => {
    expect(formatDateTime('not-a-date')).toBe('not-a-date')
  })
})
