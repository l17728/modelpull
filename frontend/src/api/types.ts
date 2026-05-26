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
  /** v2.1 SP1: 'critical' | 'standard' | 'bulk'. Default 'standard'. */
  sla_tier?: 'critical' | 'standard' | 'bulk'
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

export interface ChunkSeg {
  chunk_index: number
  byte_start: number
  byte_end: number
  source_id: string
  status: string
  bytes_done: number
}

export interface SubtaskChunkRow {
  subtask_id: string
  filename: string
  file_size: number | null
  status: string
  bytes_downloaded: number
  is_chunked: boolean
  chunks_total: number | null
  chunks_completed: number
  chunks: ChunkSeg[]
}

export interface SubtaskChunkReport {
  items: SubtaskChunkRow[]
}

export interface SourceUsed {
  source_id: string
  bytes_assigned: number
  percent: number
  measured_speed_bps: number
}

export interface ChunkRouting {
  filename: string
  chunks: ChunkSeg[]
}

export interface SourceAllocation {
  task_id: string
  sources_used: SourceUsed[]
  chunk_level_routing: ChunkRouting[]
}

export interface ParticipatingExecutor {
  executor_id: string
  executor_status: string | null
  health_score: number | null
  last_heartbeat_at: string | null
  assigned_subtasks: number
  active_subtasks: number
  bytes_downloaded: number
}

export interface ParticipatingExecutors {
  items: ParticipatingExecutor[]
}

export interface TaskEventItem {
  ts: string
  type: string
  message: string
  details: Record<string, unknown>
}

export interface TaskEventsResponse {
  items: TaskEventItem[]
  next_cursor: string | null
}

export interface ExecutorRead {
  id: string
  status: string
  health_score: number
  epoch: number
  host_id: string | null
  tenant_id: number | null
  last_heartbeat_at: string | null
  nic_speed_gbps: number | null
  disk_free_gb: number | null
  disk_total_gb: number | null
  created_at: string | null
}

export interface ExecutorListResponse {
  items: ExecutorRead[]
}

export interface AuditEntry {
  id: number
  occurred_at: string
  tenant_id: number | null
  actor_user_id: number | null
  actor_ip: string
  action: string
  resource_type: string
  resource_id: string | null
  outcome: string
  payload: Record<string, unknown>
  trace_id: string
  prev_hash: string | null
  self_hash: string
}

export interface AuditSearchResponse {
  items: AuditEntry[]
  next_cursor: string | null
}

export interface HealthActive {
  status: string
  controller_state: string
}
