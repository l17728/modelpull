import { client } from '@/api/client'
import { parseSseChunk, type SseEvent } from '@/api/sse'
import { useAuthStore } from '@/stores/auth'

export interface ChatEvent {
  event: string
  data: Record<string, unknown>
}

export interface ConversationSummary {
  id: string
  title: string | null
  last_message_at: string
  backend: string
  model_name: string
}

/** Stream POST /api/v1/ai/chat. Calls onEvent for each parsed SSE frame.
 * Resolves when the stream closes (after `done` or `error`). */
export async function streamChat(opts: {
  message: string
  conversationId?: string | null
  onEvent: (ev: ChatEvent) => void
  signal?: AbortSignal
  fetchImpl?: typeof fetch
}): Promise<void> {
  const fetchFn = opts.fetchImpl ?? globalThis.fetch
  const auth = useAuthStore()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    Accept: 'text/event-stream',
  }
  if (auth.accessToken) headers.Authorization = `Bearer ${auth.accessToken}`
  const resp = await fetchFn('/api/v1/ai/chat', {
    method: 'POST', headers, signal: opts.signal,
    body: JSON.stringify({
      message: opts.message,
      conversation_id: opts.conversationId ?? null,
    }),
  })
  if (resp.status === 401) { auth.logout(); return }
  if (!resp.ok || !resp.body) {
    opts.onEvent({ event: 'error',
                   data: { code: 'http', message: `status ${resp.status}` } })
    return
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
    for (const ev of events as SseEvent[]) {
      let data: Record<string, unknown> = {}
      try { data = JSON.parse(ev.data) } catch { data = { raw: ev.data } }
      opts.onEvent({ event: ev.event, data })
    }
  }
}

export async function listConversations(): Promise<ConversationSummary[]> {
  return (await client.get<{ items: ConversationSummary[] }>(
    '/api/v1/ai/conversations')).data.items
}

export async function getConversation(id: string): Promise<{
  conversation: { id: string; title: string | null; backend: string;
                  model_name: string }
  messages: Array<{ id: string; role: string;
                    content: Record<string, unknown>; created_at: string }>
}> {
  return (await client.get(`/api/v1/ai/conversations/${id}`)).data
}
