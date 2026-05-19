import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { TaskListResponse } from '@/api/types'

export function useTaskList() {
  return useLiveResource<TaskListResponse>(
    ['tasks'],
    async () => (await client.get<TaskListResponse>('/api/v1/tasks')).data,
    { baseIntervalMs: 5_000, staleTime: 5_000 },
  )
}
