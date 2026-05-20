import type { Ref } from 'vue'
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { ExecutorListResponse } from '@/api/types'

export function useExecutors(status: Ref<string | null>) {
  return useLiveResource<ExecutorListResponse>(
    ['executors', status],
    async () => {
      const q = status.value ? `?status=${encodeURIComponent(status.value)}` : ''
      return (await client.get<ExecutorListResponse>(
        `/api/v1/executors${q}`)).data
    },
    { baseIntervalMs: 5_000 },
  )
}
