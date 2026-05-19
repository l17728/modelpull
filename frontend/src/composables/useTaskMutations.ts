import { useMutation, useQueryClient } from '@tanstack/vue-query'
import { client } from '@/api/client'
import { TERMINAL_STATUSES, type TaskStatus } from '@/api/types'

export function canCancel(status: TaskStatus): boolean {
  return !TERMINAL_STATUSES.has(status)
}
export function canDelete(status: TaskStatus): boolean {
  return TERMINAL_STATUSES.has(status)
}

export function useTaskMutations() {
  const qc = useQueryClient()
  const invalidate = () => qc.invalidateQueries({ queryKey: ['tasks'] })

  const cancel = useMutation({
    mutationFn: (id: string) => client.post(`/api/v1/tasks/${id}/cancel`, {}),
    onSettled: invalidate,
  })
  const remove = useMutation({
    mutationFn: (id: string) => client.delete(`/api/v1/tasks/${id}`),
    onSettled: invalidate,
  })
  return { cancel, remove }
}
