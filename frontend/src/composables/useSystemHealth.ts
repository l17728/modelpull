import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { HealthActive } from '@/api/types'

export function useSystemHealth() {
  return useLiveResource<HealthActive>(
    ['health-active'],
    async () => (await client.get<HealthActive>('/health/active')).data,
    { baseIntervalMs: 10_000 },
  )
}
