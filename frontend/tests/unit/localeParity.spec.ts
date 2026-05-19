import { describe, expect, test } from 'vitest'
import en from '@/locale/en-US.json'
import zh from '@/locale/zh-CN.json'

function keys(o: Record<string, unknown>, prefix = ''): string[] {
  return Object.entries(o).flatMap(([k, v]) =>
    v && typeof v === 'object'
      ? keys(v as Record<string, unknown>, `${prefix}${k}.`)
      : [`${prefix}${k}`])
}

describe('locale parity', () => {
  test('en and zh have identical key sets', () => {
    expect(keys(en).sort()).toEqual(keys(zh).sort())
  })
  test('tasks.detail subtree exists', () => {
    expect((en as { tasks: { detail?: unknown } }).tasks.detail)
      .toBeTruthy()
  })
})
