import type { TaskRead, TaskStatus } from '@/api/types'

const IN_PROGRESS: ReadonlySet<TaskStatus> = new Set([
  'pending', 'queued', 'scheduling', 'downloading',
])

export function aggregateKpis(tasks: TaskRead[]) {
  let inProgress = 0, completed = 0, failed = 0
  for (const t of tasks) {
    if (IN_PROGRESS.has(t.status)) inProgress++
    else if (t.status === 'succeeded') completed++
    else if (t.status === 'failed') failed++
  }
  return { inProgress, completed, failed, total: tasks.length }
}

export function bucket24h(tasks: TaskRead[], now: Date = new Date()): number[] {
  const buckets = new Array<number>(24).fill(0)
  const end = now.getTime()
  const start = end - 24 * 3600_000
  for (const t of tasks) {
    const ts = new Date(t.created_at).getTime()
    if (ts >= start && ts <= end) {
      const idx = Math.min(23, Math.floor((ts - start) / 3600_000))
      buckets[idx] = (buckets[idx] ?? 0) + 1 // noUncheckedIndexedAccess
    }
  }
  return buckets
}
