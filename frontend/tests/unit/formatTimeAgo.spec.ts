import { describe, expect, test, vi, afterEach } from 'vitest'
import { formatTimeAgo } from '@/utils/format'

const NOW = new Date('2026-05-20T12:00:00Z').getTime()

describe('formatTimeAgo', () => {
  afterEach(() => { vi.useRealTimers() })
  test('null → —', () => {
    expect(formatTimeAgo(null)).toBe('—')
  })
  test('30s ago → "30s ago"', () => {
    vi.useFakeTimers().setSystemTime(NOW)
    expect(formatTimeAgo('2026-05-20T11:59:30Z')).toBe('30s ago')
  })
  test('5m ago → "5m ago"', () => {
    vi.useFakeTimers().setSystemTime(NOW)
    expect(formatTimeAgo('2026-05-20T11:55:00Z')).toBe('5m ago')
  })
  test('2h ago → "2h ago"', () => {
    vi.useFakeTimers().setSystemTime(NOW)
    expect(formatTimeAgo('2026-05-20T10:00:00Z')).toBe('2h ago')
  })
  test('3d ago → "3d ago"', () => {
    vi.useFakeTimers().setSystemTime(NOW)
    expect(formatTimeAgo('2026-05-17T12:00:00Z')).toBe('3d ago')
  })
  test('invalid → —', () => {
    expect(formatTimeAgo('not-a-date')).toBe('—')
  })
})
