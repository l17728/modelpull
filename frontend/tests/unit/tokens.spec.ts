import { describe, expect, test } from 'vitest'
import { readFileSync } from 'node:fs'

// vitest runs with cwd = frontend/ (CI working-directory + no vitest root
// override), so a cwd-relative path is robust. Reading via import.meta.url +
// fileURLToPath fails under vitest/happy-dom (import.meta.url is http://).
const css = readFileSync('src/styles/tokens.scss', 'utf-8')

describe('design tokens', () => {
  test('defines status color tokens for all 9 task statuses', () => {
    for (const s of ['pending', 'queued', 'scheduling', 'downloading',
      'succeeded', 'failed', 'cancelled', 'assigned', 'in_progress'])
      expect(css).toContain(`--dlw-status-${s}`)
  })
  test('defines a dark theme block', () => {
    expect(css).toMatch(/(:root\.dark|html\.dark|\.dark)\s*\{/)
  })
  test('defines spacing + radius tokens', () => {
    expect(css).toContain('--dlw-space-')
    expect(css).toContain('--dlw-radius')
  })
})
