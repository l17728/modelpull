import { beforeEach, describe, expect, test } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { decodePrincipal } from '@/stores/session'

// JWT = header.<base64url payload>.sig ; only payload matters. tok() strips
// padding (like real JWTs) — exercises decodePrincipal's re-pad path.
function tok(payload: Record<string, unknown>): string {
  const b64 = (o: unknown) =>
    btoa(JSON.stringify(o)).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_')
  return `${b64({ alg: 'HS256' })}.${b64(payload)}.sig`
}

describe('decodePrincipal', () => {
  beforeEach(() => setActivePinia(createPinia()))
  test('tenant user', () => {
    const p = decodePrincipal(tok({ sub: '1', tid: 1, role: 'tenant_admin', pids: [] }))
    expect(p).toEqual({ userId: 1, tenantId: 1, role: 'tenant_admin',
      projectIds: [], isServiceToken: false })
  })
  test('service token (sub=0) → isServiceToken', () => {
    const p = decodePrincipal(tok({ sub: '0', tid: 1, role: 'system_admin', pids: [] }))
    expect(p?.isServiceToken).toBe(true)
  })
  test('role system_admin → isServiceToken even if sub != 0', () => {
    const p = decodePrincipal(tok({ sub: '5', tid: 1, role: 'system_admin', pids: [] }))
    expect(p?.isServiceToken).toBe(true)
  })
  test('null / malformed → null', () => {
    expect(decodePrincipal(null)).toBeNull()
    expect(decodePrincipal('garbage')).toBeNull()
    expect(decodePrincipal('a.notbase64!.c')).toBeNull()
  })
})
