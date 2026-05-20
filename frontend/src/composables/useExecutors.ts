import { computed, type Ref } from 'vue'
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { ExecutorListResponse } from '@/api/types'

export function useExecutors(status: Ref<string | null>) {
  const streamUrl = computed(() => {
    const q = status.value ? `?status=${encodeURIComponent(status.value)}` : ''
    return `/api/v1/executors/stream${q}`
  })
  return useLiveResource<ExecutorListResponse>(
    ['executors', status],
    async () => {
      const q = status.value ? `?status=${encodeURIComponent(status.value)}` : ''
      return (await client.get<ExecutorListResponse>(
        `/api/v1/executors${q}`)).data
    },
    {
      baseIntervalMs: 5_000,
      streamUrl,
      applyEvent: (_prev, ev) => JSON.parse(ev.data) as ExecutorListResponse,
    },
  )
}
