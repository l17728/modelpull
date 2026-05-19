import { useQuery, type QueryKey } from '@tanstack/vue-query'

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
}

/**
 * Single realtime seam. Today: adaptive polling on vue-query. UI-SP5 swaps
 * the internals to SSE/WS — consumers (views) never change.
 *
 * vue-query v5.59 does NOT accept a getter for `queryKey` — it must be a
 * QueryKey (array). Reactivity comes from putting refs *inside* the array
 * (v5 unwraps them), exactly like the scaffold's proven useTaskDetail.
 */
export function useLiveResource<T>(
  key: QueryKey,
  fetcher: () => Promise<T>,
  opts: LiveOptions<T>,
) {
  return useQuery<T>({
    queryKey: key,
    queryFn: fetcher,
    staleTime: opts.staleTime ?? 0,
    refetchInterval: (query) => {
      const data = query.state.data as T | undefined
      const errored = query.state.status === 'error'
      const terminal = data !== undefined && !!opts.isTerminal?.(data)
      const hidden =
        typeof document !== 'undefined' && document.visibilityState === 'hidden'
      return computeInterval({
        base: opts.baseIntervalMs, terminal, hidden, errored,
      })
    },
  })
}
