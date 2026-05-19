import { describe, expect, test } from 'vitest'
import { formatBytes, formatRate, formatDuration } from '@/utils/format'

describe('format utils', () => {
  test('formatBytes', () => {
    expect(formatBytes(null)).toBe('—')
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(1024)).toBe('1.0 KB')
    expect(formatBytes(1024 * 1024 * 3)).toBe('3.0 MB')
  })
  test('formatRate', () => {
    expect(formatRate(0)).toBe('—')
    expect(formatRate(2048)).toBe('2.0 KB/s')
  })
  test('formatDuration', () => {
    expect(formatDuration(null)).toBe('—')
    expect(formatDuration(0)).toBe('0s')
    expect(formatDuration(65)).toBe('1m 5s')
    expect(formatDuration(3661)).toBe('1h 1m')
  })
})
