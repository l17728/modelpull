import { useQuery } from '@tanstack/vue-query'
import type { Ref } from 'vue'

import { client } from '@/api/client'
import { TERMINAL_STATUSES, type TaskDetail } from '@/api/types'

const POLL_INTERVAL_MS = 1_000
const ERROR_BACKOFF_MS = 5_000

type QueryStatus = 'pending' | 'error' | 'success'

/** Pure helper — exported so tests don't need vue-query plumbing. */
export function computeRefetchInterval(
  data: TaskDetail | undefined,
  status: QueryStatus,
): number | false {
  if (!data) {
    return status === 'error' ? ERROR_BACKOFF_MS : POLL_INTERVAL_MS
  }
  return TERMINAL_STATUSES.has(data.status) ? false : POLL_INTERVAL_MS
}

export function useTaskDetail(taskId: Ref<string>) {
  return useQuery({
    queryKey: ['task', taskId],
    queryFn: async () => {
      const r = await client.get<TaskDetail>(`/api/v1/tasks/${taskId.value}`)
      return r.data
    },
    refetchInterval: (query) =>
      computeRefetchInterval(query.state.data, query.state.status as QueryStatus),
    staleTime: 0,
  })
}
