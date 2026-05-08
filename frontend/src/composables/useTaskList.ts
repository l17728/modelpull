import { useQuery } from '@tanstack/vue-query'

import { client } from '@/api/client'
import type { TaskListResponse } from '@/api/types'

export function useTaskList() {
  return useQuery({
    queryKey: ['tasks'],
    queryFn: async () => {
      const r = await client.get<TaskListResponse>('/api/v1/tasks')
      return r.data
    },
    refetchInterval: 5_000,
    staleTime: 5_000,
  })
}
