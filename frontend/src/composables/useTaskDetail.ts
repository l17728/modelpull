import { computed, type Ref } from 'vue'
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import { TERMINAL_STATUSES, type TaskDetail } from '@/api/types'

export function useTaskDetail(taskId: Ref<string>) {
  const streamUrl = computed(() => `/api/v1/tasks/${taskId.value}/stream`)
  return useLiveResource<TaskDetail>(
    ['task', taskId],
    async () => (await client.get<TaskDetail>(
      `/api/v1/tasks/${taskId.value}`)).data,
    {
      baseIntervalMs: 1_000,
      isTerminal: (d) => TERMINAL_STATUSES.has(d.status),
      streamUrl,
      applyEvent: (_prev, ev) => JSON.parse(ev.data) as TaskDetail,
    },
  )
}
