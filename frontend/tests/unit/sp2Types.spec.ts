import { describe, expect, test } from 'vitest'
import type {
  SubtaskChunkReport, SourceAllocation,
  ParticipatingExecutors, TaskEventsResponse,
} from '@/api/types'

describe('SP2 DTO types', () => {
  test('shapes compile and round-trip', () => {
    const chunks: SubtaskChunkReport = {
      items: [{
        subtask_id: 's', filename: 'f', file_size: 10, status: 'pending',
        bytes_downloaded: 0, is_chunked: true, chunks_total: 1,
        chunks_completed: 0,
        chunks: [{
          chunk_index: 0, byte_start: 0, byte_end: 9, source_id: 'hf',
          status: 'pending', bytes_done: 0,
        }],
      }],
    }
    const alloc: SourceAllocation = {
      task_id: 't', sources_used: [{
        source_id: 'hf', bytes_assigned: 10, percent: 100,
        measured_speed_bps: 0,
      }], chunk_level_routing: [],
    }
    const ex: ParticipatingExecutors = {
      items: [{
        executor_id: 'e', executor_status: 'healthy', health_score: 90,
        last_heartbeat_at: null, assigned_subtasks: 1, active_subtasks: 1,
        bytes_downloaded: 5,
      }],
    }
    const ev: TaskEventsResponse = {
      items: [{ ts: 'now', type: 'task.note', message: 'm', details: {} }],
      next_cursor: null,
    }
    expect(chunks.items[0]?.chunks[0]?.source_id).toBe('hf')
    expect(alloc.sources_used[0]?.percent).toBe(100)
    expect(ex.items[0]?.executor_status).toBe('healthy')
    expect(ev.items[0]?.type).toBe('task.note')
  })
})
