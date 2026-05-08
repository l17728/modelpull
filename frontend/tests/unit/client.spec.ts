import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import MockAdapter from 'axios-mock-adapter'

import { client } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { router } from '@/router'

describe('api client', () => {
  let mock: MockAdapter

  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    mock = new MockAdapter(client)
  })

  afterEach(() => {
    mock.restore()
  })

  test('attaches Authorization header from auth store', async () => {
    useAuthStore().login('alpha')
    mock.onGet('/api/v1/tasks').reply((cfg) => {
      expect(cfg.headers?.Authorization).toBe('Bearer alpha')
      return [200, { items: [], total: 0 }]
    })
    await client.get('/api/v1/tasks')
  })

  test('on 401 → logout + push /login?reason=invalid_token', async () => {
    useAuthStore().login('bad')
    const push = vi.spyOn(router, 'push').mockImplementation(async () => {})
    mock.onGet('/api/v1/tasks').reply(401, { detail: 'invalid' })

    await expect(client.get('/api/v1/tasks')).rejects.toBeDefined()
    expect(useAuthStore().accessToken).toBeNull()
    expect(push).toHaveBeenCalledWith({
      path: '/login',
      query: { reason: 'invalid_token' },
    })
  })
})
