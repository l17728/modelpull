import { computed, type Ref } from 'vue'
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { SubtaskChunkReport } from '@/api/types'

export function useSubtaskChunks(
  taskId: Ref<string>, enabled: Ref<boolean>, terminal: Ref<boolean>,
) {
  const streamUrl = computed(
    () => `/api/v1/tasks/${taskId.value}/subtask-chunks/stream`)
  return useLiveResource<SubtaskChunkReport>(
    ['task-chunks', taskId],
    async () => (await client.get<SubtaskChunkReport>(
      `/api/v1/tasks/${taskId.value}/subtask-chunks`)).data,
    {
      baseIntervalMs: 1_500,
      enabled,
      isTerminal: () => terminal.value,
      streamUrl,
      applyEvent: (_prev, ev) =>
        JSON.parse(ev.data) as SubtaskChunkReport,
    },
  )
}
