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

/** Shared SSE-over-POST to /api/v1/ai/chat. Calls onEvent per parsed frame;
 * resolves when the stream closes (after `done`/`error`). */
async function _streamPost(
  body: Record<string, unknown>,
  onEvent: (ev: ChatEvent) => void,
  signal?: AbortSignal,
  fetchImpl?: typeof fetch,
): Promise<void> {
  const fetchFn = fetchImpl ?? globalThis.fetch
  const auth = useAuthStore()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    Accept: 'text/event-stream',
  }
  if (auth.accessToken) headers.Authorization = `Bearer ${auth.accessToken}`
  const resp = await fetchFn('/api/v1/ai/chat', {
    method: 'POST', headers, signal, body: JSON.stringify(body),
  })
  if (resp.status === 401) { auth.logout(); return }
  if (!resp.ok || !resp.body) {
    onEvent({ event: 'error',
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
      onEvent({ event: ev.event, data })
    }
  }
}

/** Phase 1: send a user message. */
export async function streamChat(opts: {
  message: string
  conversationId?: string | null
  onEvent: (ev: ChatEvent) => void
  signal?: AbortSignal
  fetchImpl?: typeof fetch
}): Promise<void> {
  return _streamPost(
    { message: opts.message, conversation_id: opts.conversationId ?? null },
    opts.onEvent, opts.signal, opts.fetchImpl)
}

/** Phase 2: resolve a pending write-tool call (approve/reject/modify). */
export async function confirmTool(opts: {
  conversationId: string
  callId: string
  decision: 'approved' | 'rejected' | 'modified'
  modifiedInput?: Record<string, unknown> | null
  onEvent: (ev: ChatEvent) => void
  signal?: AbortSignal
  fetchImpl?: typeof fetch
}): Promise<void> {
  return _streamPost({
    conversation_id: opts.conversationId,
    tool_confirmation: {
      call_id: opts.callId, decision: opts.decision,
      modified_input: opts.modifiedInput ?? null,
    },
  }, opts.onEvent, opts.signal, opts.fetchImpl)
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
