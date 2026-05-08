import { beforeEach, describe, expect, test } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useAuthStore } from '@/stores/auth'

describe('useAuthStore', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
  })

  test('starts unauthenticated when localStorage is empty', () => {
    const auth = useAuthStore()
    expect(auth.accessToken).toBeNull()
    expect(auth.isAuthenticated).toBe(false)
  })

  test('hydrates accessToken from localStorage on creation', () => {
    localStorage.setItem('dlw_token', 'persisted-token')
    const auth = useAuthStore()
    expect(auth.accessToken).toBe('persisted-token')
    expect(auth.isAuthenticated).toBe(true)
  })

  test('login persists token + sets accessToken', () => {
    const auth = useAuthStore()
    auth.login('new-token')
    expect(auth.accessToken).toBe('new-token')
    expect(localStorage.getItem('dlw_token')).toBe('new-token')
    expect(auth.isAuthenticated).toBe(true)
  })

  test('logout clears localStorage + resets accessToken', () => {
    localStorage.setItem('dlw_token', 'live-token')
    const auth = useAuthStore()
    auth.logout()
    expect(auth.accessToken).toBeNull()
    expect(localStorage.getItem('dlw_token')).toBeNull()
    expect(auth.isAuthenticated).toBe(false)
  })
})
