import { describe, expect, test } from 'vitest'
import type {
  ExecutorRead, ExecutorListResponse,
  AuditEntry, AuditSearchResponse,
  HealthActive,
} from '@/api/types'

describe('SP3 DTO types', () => {
  test('shapes compile', () => {
    const ex: ExecutorRead = {
      id: 'e', status: 'healthy', health_score: 100, epoch: 1,
      host_id: 'h', tenant_id: 1, last_heartbeat_at: null,
      nic_speed_gbps: 10, disk_free_gb: 100, disk_total_gb: 200,
      created_at: null,
    }
    const exList: ExecutorListResponse = { items: [ex] }
    const ent: AuditEntry = {
      id: 1, occurred_at: 'now', tenant_id: 1, actor_user_id: 1,
      actor_ip: '', action: 'task.note', resource_type: 'task',
      resource_id: 'r', outcome: 'success', payload: {}, trace_id: '',
      prev_hash: null, self_hash: 's',
    }
    const audit: AuditSearchResponse = { items: [ent], next_cursor: null }
    const h: HealthActive = { status: 'active', controller_state: 'active' }
    expect(exList.items[0]?.id).toBe('e')
    expect(audit.items[0]?.action).toBe('task.note')
    expect(h.controller_state).toBe('active')
  })
})
