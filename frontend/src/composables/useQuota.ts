import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { QuotaCurrent } from '@/api/types'

export function useQuota() {
  return useLiveResource<QuotaCurrent>(
    ['quota'],
    async () => (await client.get<QuotaCurrent>('/api/v1/quota/current')).data,
    { baseIntervalMs: 30_000, staleTime: 30_000 },
  )
}
