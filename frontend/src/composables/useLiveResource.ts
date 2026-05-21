import { onScopeDispose, ref, computed, toValue, watch, type MaybeRefOrGetter, type Ref } from 'vue'
import { useQuery, useQueryClient, type QueryKey } from '@tanstack/vue-query'
import { shouldStream, streamSse, type SseEvent } from '@/api/sse'
import { useAuthStore } from '@/stores/auth'

const ERROR_BACKOFF_MS = 5_000
const HIDDEN_MULTIPLIER = 3

export function computeInterval(o: {
  base: number; terminal: boolean; hidden: boolean; errored: boolean
}): number | false {
  if (o.terminal) return false
  if (o.errored) return ERROR_BACKOFF_MS
  return o.hidden ? o.base * HIDDEN_MULTIPLIER : o.base
}

export interface LiveOptions<T> {
  baseIntervalMs: number
  isTerminal?: (data: T) => boolean
  staleTime?: number
  enabled?: Ref<boolean> | boolean
  /** UI-SP5: opt in to SSE. SP5f: streaming is now reactive — an
   * enabled Ref that starts false will lazy-open the SSE on first
   * enabled === true (after useQuery has produced data). */
  streamUrl?: string | Ref<string>
  applyEvent?: (prev: T | undefined, ev: SseEvent) => T
}

/**
 * Single realtime seam. Today: adaptive polling on vue-query, with an
 * additive opt-in SSE swap (UI-SP5). SP5f evolution: the streaming
 * gate is reactive so consumers whose `enabled` starts false (e.g.
 * tab-gated SP2 sub-resources) can lazy-open the SSE on first
 * activation. UI-SP4 (AI-Copilot) and future transports plug in here
 * without view changes.
 */
export function useLiveResource<T>(
  key: QueryKey,
  fetcher: () => Promise<T>,
  opts: LiveOptions<T>,
) {
  // SP5f: reactive streaming gate. Was a `const` (evaluated once at
  // call-time) in SP5-SP5e because no consumer passed an `enabled`
  // Ref that started false. `useTaskEvents` (SP5f) is the first; the
  // computed makes streamUrl lazy-open when enabled flips true.
  const streaming = computed(() => shouldStream({
    streamUrl: opts.streamUrl,
    applyEvent: opts.applyEvent as
      ((prev: unknown, ev: SseEvent) => unknown) | undefined,
    enabled: opts.enabled,
  }))

  const pollingFallback = ref(false)

  const q = useQuery<T>({
    queryKey: key,
    queryFn: fetcher,
    enabled: opts.enabled,
    staleTime: opts.staleTime ?? 0,
    refetchInterval: (query) => {
      const data = query.state.data as T | undefined
      const errored = query.state.status === 'error'
      const terminal = data !== undefined && !!opts.isTerminal?.(data)
      const hidden = typeof document !== 'undefined'
        && document.visibilityState === 'hidden'
      if (streaming.value && !pollingFallback.value) {
        return false
      }
      return computeInterval({
        base: opts.baseIntervalMs, terminal, hidden, errored,
      })
    },
  })

  if (opts.streamUrl && opts.applyEvent) {
    const qc = useQueryClient()
    const auth = useAuthStore()
    const apply = opts.applyEvent
    // SP5i: open/close lifecycle (was a permanent `started` latch). The
    // stream closes when `streaming` flips false (e.g. a tab-gated consumer
    // deactivates) and reopens when it flips true again — bounding the
    // concurrent SSE count to (always-on streams) + (1 per active gated
    // tab). `ac` is non-null iff a stream is currently live.
    let ac: AbortController | null = null
    let gaveUp = false

    const openStream = () => {
      if (ac) return
      if (gaveUp) return
      if (!streaming.value) return
      if (q.data.value === undefined) return
      const controller = new AbortController()
      ac = controller
      const url = toValue(opts.streamUrl as MaybeRefOrGetter<string>)
      void streamSse({
        url, token: auth.accessToken, signal: controller.signal,
        onEvent: (ev) => {
          const prev = qc.getQueryData<T>(key)
          const next = apply(prev, ev)
          qc.setQueryData(key, next)
        },
        onUnauthorized: () => {
          auth.logout()
        },
      }).then(() => {
        // streamSse RESOLVES on BOTH abort and giveup (sse.ts). Identity
        // guard: if `ac` no longer references THIS controller, we aborted it
        // ourselves (closeStream / reopen) — do nothing. Else it gave up
        // after 3 consecutive failures → fall back to polling.
        if (ac === controller) {
          ac = null
          gaveUp = true
          pollingFallback.value = true
          void q.refetch()
        }
      }).catch(() => {
        // 401 path — onUnauthorized already invoked.
        if (ac === controller) ac = null
      })
    }

    const closeStream = () => {
      if (ac) { ac.abort(); ac = null }
      // Reset the giveup latch so reactivating the tab retries SSE fresh —
      // a transient outage on one visit must not permanently downgrade this
      // consumer to polling. Always-on consumers never call closeStream
      // (except on dispose), so their giveup fallback stays permanent.
      gaveUp = false
    }

    // Watch handle intentionally not captured: useLiveResource always runs
    // in a component setup scope, so Vue auto-stops this watcher on unmount;
    // onScopeDispose→closeStream aborts any live stream first.
    watch(
      [streaming, () => q.data.value] as const,
      ([isOn, data]) => {
        if (isOn && data !== undefined) openStream()
        else if (!isOn) closeStream()
      },
      { immediate: true },
    )
    onScopeDispose(closeStream)
  }

  return q
}
