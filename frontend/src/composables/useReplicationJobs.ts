import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { ReplicationJobsResponse } from '@/api/types'

export function useReplicationJobs(statusFilter?: string) {
  const url = statusFilter
    ? `/api/v1/replication?status=${encodeURIComponent(statusFilter)}`
    : '/api/v1/replication'
  return useLiveResource<ReplicationJobsResponse>(
    ['replication', 'list', statusFilter ?? 'all'],
    async () => (await client.get<ReplicationJobsResponse>(url)).data,
    {
      baseIntervalMs: 5_000,
      staleTime: 5_000,
    },
  )
}
