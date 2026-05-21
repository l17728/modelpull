import { computed, type Ref } from 'vue'
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { SourceAllocation } from '@/api/types'

export function useSourceAllocation(
  taskId: Ref<string>, enabled: Ref<boolean>, terminal: Ref<boolean>,
) {
  const streamUrl = computed(
    () => `/api/v1/tasks/${taskId.value}/source-allocation/stream`)
  return useLiveResource<SourceAllocation>(
    ['task-source-alloc', taskId],
    async () => (await client.get<SourceAllocation>(
      `/api/v1/tasks/${taskId.value}/source-allocation`)).data,
    {
      baseIntervalMs: 2_000,
      enabled,
      isTerminal: () => terminal.value,
      streamUrl,
      applyEvent: (_prev, ev) => JSON.parse(ev.data) as SourceAllocation,
    },
  )
}
