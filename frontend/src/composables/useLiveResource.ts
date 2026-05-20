import { onScopeDispose, ref, computed, toValue, watch, type MaybeRefOrGetter, type Ref, type WatchStopHandle } from 'vue'
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
    const ac = new AbortController()
    const auth = useAuthStore()
    const apply = opts.applyEvent
    let started = false
    let stopDataWatch: WatchStopHandle | undefined
    let stopStreamingWatch: WatchStopHandle | undefined

    const tryStart = () => {
      if (started) return
      if (!streaming.value) return
      if (q.data.value === undefined) return
      started = true
      stopDataWatch?.()
      stopStreamingWatch?.()
      const url = toValue(opts.streamUrl as MaybeRefOrGetter<string>)
      void streamSse({
        url, token: auth.accessToken, signal: ac.signal,
        onEvent: (ev) => {
          const prev = qc.getQueryData<T>(key)
          const next = apply(prev, ev)
          qc.setQueryData(key, next)
        },
        onUnauthorized: () => {
          auth.logout()
        },
      }).then(() => {
        // streamSse resolved without abort → it gave up (3 consecutive
        // failures). Fall back to polling.
        pollingFallback.value = true
        void q.refetch()
      }).catch(() => {
        // 401 path — onUnauthorized already invoked.
      })
    }

    stopDataWatch = watch(() => q.data.value, tryStart, { immediate: true })
    stopStreamingWatch = watch(streaming, tryStart)
    onScopeDispose(() => {
      ac.abort()
      // Explicit watcher cleanup. tryStart already stops both on first
      // successful open; this catches the never-opened paths (e.g.
      // enabled-permanent-false consumer) so Vue's scope-dispose isn't
      // the only reaper. (Final-review MEDIUM; cosmetic but cheap.)
      stopDataWatch?.()
      stopStreamingWatch?.()
    })
  }

  return q
}
