import { describe, expect, test } from 'vitest'
import { oidcLoginUrl } from '@/pages/oidc'

describe('oidcLoginUrl', () => {
  test('uses VITE_API_BASE when set', () => {
    expect(oidcLoginUrl('http://c:8001')).toBe('http://c:8001/api/v1/auth/login')
  })
  test('relative when base empty (vite proxy)', () => {
    expect(oidcLoginUrl('')).toBe('/api/v1/auth/login')
    expect(oidcLoginUrl(undefined)).toBe('/api/v1/auth/login')
  })
})
