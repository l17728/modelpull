import type { TaskRead } from '@/api/types'

export function filterTasks(
  items: TaskRead[], f: { status: string; q: string },
): TaskRead[] {
  const q = f.q.trim().toLowerCase()
  return items.filter((t) => {
    if (f.status && t.status !== f.status) return false
    if (q && !t.repo_id.toLowerCase().includes(q) &&
        !t.id.toLowerCase().includes(q)) return false
    return true
  })
}
