import axios from 'axios'

import { router } from '@/router'
import { useAuthStore } from '@/stores/auth'

export const client = axios.create({
  baseURL: import.meta.env.VITE_API_BASE ?? '',
  timeout: 10_000,
})

client.interceptors.request.use((config) => {
  const auth = useAuthStore()
  if (auth.accessToken) {
    config.headers.Authorization = `Bearer ${auth.accessToken}`
  }
  return config
})

client.interceptors.response.use(
  (response) => response,
  (error) => {
    const status: number | undefined = error?.response?.status
    if (status === 401) {
      const auth = useAuthStore()
      // Burst guard: when N in-flight requests all 401, only the first one
      // performs the logout + redirect (auth.logout() is idempotent but a
      // single push is cleaner than N deduplicated by vue-router).
      if (auth.isAuthenticated) {
        auth.logout()
        router.push({ path: '/login', query: { reason: 'invalid_token' } })
      }
    }
    return Promise.reject(error)
  },
)
