import { computed } from 'vue'
import { defineStore } from 'pinia'
import { useAuthStore } from '@/stores/auth'
import type { Principal } from '@/api/types'

export function decodePrincipal(token: string | null): Principal | null {
  if (!token) return null
  const parts = token.split('.')
  if (parts.length !== 3) return null
  const payload = parts[1] // bind (noUncheckedIndexedAccess)
  if (!payload) return null
  try {
    const raw = payload.replace(/-/g, '+').replace(/_/g, '/')
    // JWT base64url is unpadded — re-pad before atob (atob is
    // length-dependent / throws on `len % 4 === 1` without padding).
    const b64 = raw + '='.repeat((4 - (raw.length % 4)) % 4)
    const json = decodeURIComponent(
      atob(b64).split('').map(
        (c) => '%' + c.charCodeAt(0).toString(16).padStart(2, '0')).join(''),
    )
    const c = JSON.parse(json) as Record<string, unknown>
    const userId = Number(c.sub)
    const role = String(c.role ?? '')
    if (!Number.isFinite(userId)) return null
    return {
      userId,
      tenantId: Number(c.tid ?? 0),
      role,
      projectIds: Array.isArray(c.pids) ? (c.pids as number[]) : [],
      isServiceToken: userId === 0 || role === 'system_admin',
    }
  } catch {
    return null
  }
}

export const useSessionStore = defineStore('session', () => {
  const auth = useAuthStore()
  const principal = computed(() => decodePrincipal(auth.accessToken))
  const role = computed(() => principal.value?.role ?? 'guest')
  const isServiceToken = computed(() => principal.value?.isServiceToken ?? false)
  return { principal, role, isServiceToken }
})
