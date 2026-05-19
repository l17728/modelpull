import { describe, expect, test } from 'vitest'
import zh from '@/locale/zh-CN.json'
import en from '@/locale/en-US.json'

function keys(o: unknown, p = ''): string[] {
  if (o && typeof o === 'object' && !Array.isArray(o)) {
    return Object.entries(o as Record<string, unknown>)
      .flatMap(([k, v]) => keys(v, p ? `${p}.${k}` : k))
  }
  return [p]
}

describe('locale parity', () => {
  test('en-US and zh-CN have identical key sets', () => {
    expect(keys(en).sort()).toEqual(keys(zh).sort())
  })
  test('new keys present', () => {
    for (const k of ['nav.dashboard', 'nav.tasks', 'tasks.create',
      'tasks.filterStatus', 'create.heading', 'create.serviceTokenWarn',
      'shell.theme', 'shell.language', 'palette.placeholder',
      'dashboard.heading', 'errors.quota_exceeded'])
      expect(keys(zh)).toContain(k)
  })
})
