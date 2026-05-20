export interface SseEvent {
  event: string
  data: string
  id?: string
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
