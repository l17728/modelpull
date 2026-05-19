import { describe, expect, test } from 'vitest'
import { NAV_ITEMS, visibleNav } from '@/nav/registry'

describe('nav registry', () => {
  test('all items have route + labelKey', () => {
    for (const i of NAV_ITEMS) {
      expect(i.route).toBeTruthy()
      expect(i.labelKey).toMatch(/^nav\./)
    }
  })
  test('visibleNav: no roles → visible to everyone', () => {
    const names = visibleNav('guest').map((i) => i.route)
    expect(names).toContain('taskList')
    expect(names).toContain('dashboard')
  })
  test('role-gated item hidden for wrong role', () => {
    const gated = { route: 'x', labelKey: 'nav.x', icon: 'i', roles: ['system_admin'] }
    expect(visibleNav('tenant_admin', [...NAV_ITEMS, gated]).map((i) => i.route))
      .not.toContain('x')
    expect(visibleNav('system_admin', [...NAV_ITEMS, gated]).map((i) => i.route))
      .toContain('x')
  })
})
