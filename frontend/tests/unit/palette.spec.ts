import { describe, expect, test } from 'vitest'
import { buildCommands } from '@/components/palette'

describe('buildCommands', () => {
  const t = (k: string) => k
  test('includes nav items + create + open-by-id', () => {
    const cmds = buildCommands('tenant_admin', t)
    const ids = cmds.map((c) => c.id)
    expect(ids).toContain('nav:dashboard')
    expect(ids).toContain('nav:taskList')
    expect(ids).toContain('action:createTask')
    expect(ids).toContain('action:openTaskById')
  })
  test('role-gates nav', () => {
    const cmds = buildCommands('guest', t)
    expect(cmds.find((c) => c.id === 'nav:dashboard')).toBeTruthy()
  })
})
