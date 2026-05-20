import { isRef, type Ref } from 'vue'

export interface SseEvent {
  event: string
  data: string
  id?: string
}

export interface StreamSseOptions {
  url: string
  token: string | null
  onEvent: (ev: SseEvent) => void
  onUnauthorized: () => void
  signal: AbortSignal
  /** Test seam: override the global fetch (defaults to globalThis.fetch). */
  fetchImpl?: typeof fetch
}

export interface StreamGateInput {
  streamUrl?: string | Ref<string> | undefined
  applyEvent?: ((prev: unknown, ev: SseEvent) => unknown) | undefined
  enabled?: boolean | Ref<boolean> | undefined
}

/** Pure gate: does this LiveResource configuration want streaming? */
export function shouldStream(o: StreamGateInput): boolean {
  if (!o.streamUrl || !o.applyEvent) return false
  if (o.enabled === false) return false
  if (isRef(o.enabled) && o.enabled.value === false) return false
  return true
}

const BACKOFF_MS = [1_000, 2_000, 4_000, 8_000, 16_000, 30_000] as const
const GIVEUP_AFTER_CONSECUTIVE_FAILURES = 3

/**
 * Fetch-based SSE client. Sends `Authorization: Bearer <token>` (browser
 * EventSource cannot set headers). On 401 → calls onUnauthorized and rejects.
 * On 403/404 → resolves (permanent client error; consumer falls back to
 * polling). On any other disconnect/error: exponential backoff with ±20%
 * jitter, capped at 30 s. Successful chunk receipt resets the backoff
 * counter. Aborts when the AbortSignal fires.
 *
 * Resolves when consecutive failures exceed GIVEUP_AFTER_CONSECUTIVE_FAILURES
 * (signal for the consumer to fall back to polling). Rejects only on 401.
 */
export async function streamSse(opts: StreamSseOptions): Promise<void> {
  const fetchFn = opts.fetchImpl ?? globalThis.fetch
  let consecutiveFailures = 0
  let backoffIdx = 0
  while (!opts.signal.aborted) {
    let connected = false
    try {
      const headers: Record<string, string> = {
        Accept: 'text/event-stream',
      }
      if (opts.token) headers.Authorization = `Bearer ${opts.token}`
      const resp = await fetchFn(opts.url, {
        method: 'GET', headers, signal: opts.signal,
      })
      if (resp.status === 401) {
        opts.onUnauthorized()
        throw new Error('SSE 401')
      }
      // Pre-review IMPORTANT fix: permanent client errors (task gone /
      // forbidden) should fail-fast — burning 7+ s of backoff on a 404 is
      // pure latency before the consumer's poll-fallback kicks in.
      if (resp.status === 403 || resp.status === 404) {
        return
      }
      if (!resp.ok || !resp.body) {
        throw new Error(`SSE upstream status ${resp.status}`)
      }
      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        const { events, remainder } = parseSseChunk(buf)
        buf = remainder
        for (const ev of events) {
          opts.onEvent(ev)
          if (!connected) {
            connected = true
            consecutiveFailures = 0
            backoffIdx = 0
          }
        }
      }
    } catch (err) {
      if (opts.signal.aborted) return
      if ((err as Error).message === 'SSE 401') throw err
      consecutiveFailures += 1
      if (consecutiveFailures >= GIVEUP_AFTER_CONSECUTIVE_FAILURES) return
    }
    if (opts.signal.aborted) return
    const base = BACKOFF_MS[Math.min(backoffIdx, BACKOFF_MS.length - 1)]
      ?? 30_000
    const jitter = base * (0.8 + Math.random() * 0.4)
    backoffIdx += 1
    await new Promise<void>((resolve) => {
      if (opts.signal.aborted) { resolve(); return }
      const t = setTimeout(resolve, jitter)
      opts.signal.addEventListener('abort',
        () => { clearTimeout(t); resolve() }, { once: true })
    })
  }
}

/**
 * Pure SSE wire-format parser. The caller accumulates raw text chunks from a
 * ReadableStream and feeds them in; this function returns any complete events
 * plus the trailing partial-block remainder to prepend to the next chunk.
 */
export function parseSseChunk(
  buf: string,
): { events: SseEvent[]; remainder: string } {
  const events: SseEvent[] = []
  const parts = buf.split(/\r?\n\r?\n/)
  const remainder = parts.pop() ?? ''
  for (const block of parts) {
    let event = 'message'
    let data = ''
    let id: string | undefined
    // Pre-review IMPORTANT fix: track presence of any `data:` field, not just
    // truthy strings, so legitimate payloads like "0" / "false" / "" aren't
    // silently dropped (cf. SSE spec — empty data is a valid event).
    let hasData = false
    for (const line of block.split(/\r?\n/)) {
      if (line === '' || line.startsWith(':')) continue
      const i = line.indexOf(':')
      const field = i === -1 ? line : line.slice(0, i)
      const value = i === -1 ? '' : line.slice(i + 1).replace(/^ /, '')
      if (field === 'event') event = value
      else if (field === 'data') {
        hasData = true
        data += (data ? '\n' : '') + value
      }
      else if (field === 'id') id = value
    }
    if (hasData) {
      events.push(id !== undefined ? { event, data, id } : { event, data })
    }
  }
  return { events, remainder }
}
