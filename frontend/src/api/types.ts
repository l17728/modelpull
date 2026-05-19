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

export interface Principal {
  userId: number
  tenantId: number
  role: string
  projectIds: number[]
  isServiceToken: boolean
}

export interface QuotaCurrent {
  tenant_id: number
  bytes_used_month: number
  bytes_quota_month: number
  storage_gb_used: number
  storage_gb_quota: number
  concurrent_tasks: number
  concurrent_quota: number
}

export interface TaskCreateBody {
  repo_id: string
  revision: string
  storage_id: number
  priority?: number
  source_strategy?: string
  source_blacklist?: string[]
  trust_non_hf_sha256?: boolean
  upgrade_from_revision?: string
}
