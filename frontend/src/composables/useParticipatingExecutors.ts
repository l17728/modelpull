import { computed, type Ref } from 'vue'
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { ParticipatingExecutors } from '@/api/types'

export function useParticipatingExecutors(
  taskId: Ref<string>, enabled: Ref<boolean>, terminal: Ref<boolean>,
) {
  const streamUrl = computed(
    () => `/api/v1/tasks/${taskId.value}/participating-executors/stream`)
  return useLiveResource<ParticipatingExecutors>(
    ['task-executors', taskId],
    async () => (await client.get<ParticipatingExecutors>(
      `/api/v1/tasks/${taskId.value}/participating-executors`)).data,
    {
      baseIntervalMs: 2_000,
      enabled,
      isTerminal: () => terminal.value,
      streamUrl,
      applyEvent: (_prev, ev) => JSON.parse(ev.data) as ParticipatingExecutors,
    },
  )
}
