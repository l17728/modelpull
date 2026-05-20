import { onScopeDispose, ref, toValue, watch, type MaybeRefOrGetter, type Ref, type WatchStopHandle } from 'vue'
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
  /** UI-SP5: opt in to SSE. When set with applyEvent, the composable opens a
   * stream after the first useQuery success and writes events into the cache
   * via setQueryData. Polling stays disabled while streaming is healthy and
   * resumes automatically if the stream gives up. */
  streamUrl?: string | Ref<string>
  applyEvent?: (prev: T | undefined, ev: SseEvent) => T
}

/**
 * Single realtime seam. Today: adaptive polling on vue-query, with an
 * additive opt-in SSE swap (UI-SP5). UI-SP4 (AI-Copilot) and future
 * transports plug in here without view changes.
 *
 * vue-query v5 does NOT accept a getter for `queryKey` — it must be a
 * QueryKey (array). Reactivity comes from putting refs *inside* the array.
 */
export function useLiveResource<T>(
  key: QueryKey,
  fetcher: () => Promise<T>,
  opts: LiveOptions<T>,
) {
  const streaming = shouldStream({
    streamUrl: opts.streamUrl,
    // `shouldStream` only checks truthiness; cast widens T → unknown safely.
    applyEvent: opts.applyEvent as
      ((prev: unknown, ev: SseEvent) => unknown) | undefined,
    enabled: opts.enabled,
  })

  // Pre-review fix (IMPORTANT 1): use a ref for clarity; the callback re-reads
  // the closure variable per refetchInterval-eval, but matching Vue idioms
  // makes intent obvious and future-proofs anyone wanting to watch() this.
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
      if (streaming && !pollingFallback.value) {
        return false
      }
      return computeInterval({
        base: opts.baseIntervalMs, terminal, hidden, errored,
      })
    },
  })

  if (streaming && opts.applyEvent) {
    const qc = useQueryClient()
    const ac = new AbortController()
    const auth = useAuthStore()
    const apply = opts.applyEvent
    // Pre-review BLOCKER fix: Vue 3.5's `watch({ immediate: true })` invokes
    // the handler SYNCHRONOUSLY inside the watch() call — before `stopWatch`
    // is assigned the returned stop handle. If vue-query serves a cached
    // snapshot synchronously (e.g. HMR / route revisit / future initialData),
    // calling `stopWatch()` from inside the handler would hit TDZ
    // (ReferenceError). Fix: `let stopWatch` + optional-call + a `started`
    // flag so the kick-off runs exactly once even if the watcher fires
    // synchronously on registration.
    let stopWatch: WatchStopHandle | undefined
    let started = false
    stopWatch = watch(
      () => q.data.value,
      (snapshot) => {
        if (snapshot === undefined || started) return
        started = true
        stopWatch?.()  // safe — undefined on synchronous immediate fire
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
      },
      { immediate: true },
    )
    onScopeDispose(() => { ac.abort() })
  }

  return q
}
