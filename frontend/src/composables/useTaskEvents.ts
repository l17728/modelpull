import { computed, type Ref } from 'vue'
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { TaskEventsResponse } from '@/api/types'

export function useTaskEvents(
  taskId: Ref<string>, enabled: Ref<boolean>, terminal: Ref<boolean>,
) {
  const streamUrl = computed(
    () => `/api/v1/tasks/${taskId.value}/events/stream`)
  return useLiveResource<TaskEventsResponse>(
    ['task-events', taskId],
    async () => (await client.get<TaskEventsResponse>(
      `/api/v1/tasks/${taskId.value}/events?limit=50`)).data,
    {
      baseIntervalMs: 5_000,
      enabled,
      isTerminal: () => terminal.value,
      streamUrl,
      applyEvent: (_prev, ev) =>
        JSON.parse(ev.data) as TaskEventsResponse,
    },
  )
}

/** One-shot "load older" page (not live; appended in the page). */
export async function fetchOlderEvents(
  taskId: string, cursor: string,
): Promise<TaskEventsResponse> {
  return (await client.get<TaskEventsResponse>(
    `/api/v1/tasks/${taskId}/events?limit=50&cursor=${encodeURIComponent(cursor)}`,
  )).data
}
