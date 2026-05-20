import { computed, type Ref } from 'vue'
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { AuditSearchResponse } from '@/api/types'

export interface AuditFilters {
  action: Ref<string>
  actor: Ref<number | null>
  from: Ref<string | null>
  to: Ref<string | null>
}
export interface AuditFiltersPlain {
  action: string
  actor: number | null
  from: string | null
  to: string | null
}

function buildQuery(
  f: AuditFiltersPlain, cursor: string | null,
): string {
  const p = new URLSearchParams()
  p.set('limit', '50')
  if (f.action) p.set('action', f.action)
  if (typeof f.actor === 'number' && Number.isFinite(f.actor)) {
    p.set('actor_user_id', String(f.actor))
  }
  if (f.from) p.set('from', f.from)
  if (f.to) p.set('to', f.to)
  if (cursor) p.set('cursor', cursor)
  return `/api/v1/audit/log?${p.toString()}`
}

function buildStreamUrl(f: AuditFiltersPlain): string {
  const p = new URLSearchParams()
  if (f.action) p.set('action', f.action)
  if (typeof f.actor === 'number' && Number.isFinite(f.actor)) {
    p.set('actor_user_id', String(f.actor))
  }
  if (f.from) p.set('from', f.from)
  if (f.to) p.set('to', f.to)
  const qs = p.toString()
  return `/api/v1/audit/log/stream${qs ? '?' + qs : ''}`
}

export function useAuditLog(f: AuditFilters) {
  const streamUrl = computed(() => buildStreamUrl({
    action: f.action.value, actor: f.actor.value,
    from: f.from.value, to: f.to.value,
  }))
  return useLiveResource<AuditSearchResponse>(
    ['audit', f.action, f.actor, f.from, f.to],
    async () => (await client.get<AuditSearchResponse>(buildQuery({
      action: f.action.value, actor: f.actor.value,
      from: f.from.value, to: f.to.value,
    }, null))).data,
    {
      baseIntervalMs: 10_000,
      streamUrl,
      applyEvent: (_prev, ev) => JSON.parse(ev.data) as AuditSearchResponse,
    },
  )
}

export async function fetchOlderAudit(
  f: AuditFiltersPlain, cursor: string,
): Promise<AuditSearchResponse> {
  return (await client.get<AuditSearchResponse>(buildQuery(f, cursor))).data
}
