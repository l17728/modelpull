// Hand-written DTOs — mirror src/dlw/schemas/{task,subtask}.py.
// Phase 2 plan: replace with openapi-typescript codegen.

export type TaskStatus =
  | 'pending'
  | 'queued'
  | 'scheduling'
  | 'downloading'
  | 'succeeded'
  | 'failed'
  | 'cancelled'

export interface SubTaskRead {
  id: string
  task_id: string
  filename: string
  file_size: number | null
  expected_sha256: string | null
  status: string
}

export interface TaskRead {
  id: string
  repo_id: string
  revision: string
  status: TaskStatus
  priority: number
  created_at: string
  completed_at: string | null
  error_message: string | null
}

export interface TaskDetail extends TaskRead {
  subtasks: SubTaskRead[]
}

export interface TaskListResponse {
  items: TaskRead[]
  total: number
}

export const TERMINAL_STATUSES: ReadonlySet<TaskStatus> = new Set([
  'succeeded',
  'failed',
  'cancelled',
])
