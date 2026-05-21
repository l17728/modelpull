import { ref } from 'vue'
import {
  streamChat, listConversations, getConversation,
  type ConversationSummary,
} from '@/api/aiClient'

export interface ToolCard {
  id: string
  tool: string
  input?: Record<string, unknown>
  output?: Record<string, unknown>
  ok?: boolean
}

export interface ChatMessage {
  role: 'user' | 'assistant'
  text: string
  toolCards: ToolCard[]
}

export function useCopilot() {
  const messages = ref<ChatMessage[]>([])
  const streaming = ref(false)
  const conversationId = ref<string | null>(null)
  const conversations = ref<ConversationSummary[]>([])

  async function send(text: string): Promise<void> {
    const trimmed = text.trim()
    if (!trimmed || streaming.value) return
    messages.value.push({ role: 'user', text: trimmed, toolCards: [] })
    const assistant: ChatMessage = { role: 'assistant', text: '', toolCards: [] }
    messages.value.push(assistant)
    streaming.value = true
    try {
      await streamChat({
        message: trimmed,
        conversationId: conversationId.value,
        onEvent: (ev) => {
          if (ev.event === 'assistant.message_delta') {
            assistant.text += String(ev.data.text ?? '')
          } else if (ev.event === 'tool_call') {
            assistant.toolCards.push({
              id: String(ev.data.id ?? ''),
              tool: String(ev.data.tool ?? ''),
              input: ev.data.input as Record<string, unknown> | undefined,
            })
          } else if (ev.event === 'tool_result') {
            const card = assistant.toolCards.find(
              (c) => c.id === String(ev.data.id ?? ''))
            if (card) {
              card.ok = Boolean(ev.data.ok)
              card.output = ev.data.output as Record<string, unknown> | undefined
            }
          } else if (ev.event === 'done') {
            conversationId.value = String(ev.data.conversation_id ?? '') || null
          } else if (ev.event === 'error') {
            assistant.text += `\n[error: ${ev.data.message ?? ev.data.code}]`
          }
        },
      })
    } finally {
      streaming.value = false
    }
  }

  async function refreshConversations(): Promise<void> {
    conversations.value = await listConversations()
  }

  async function loadConversation(id: string): Promise<void> {
    const { conversation, messages: msgs } = await getConversation(id)
    conversationId.value = conversation.id
    messages.value = msgs.map((m) => {
      const content = m.content as {
        text?: string
        tool_calls?: Array<{ id?: string; tool?: string;
                             input?: Record<string, unknown>;
                             output?: Record<string, unknown>; ok?: boolean }>
      }
      return {
        role: m.role === 'user' ? 'user' : 'assistant',
        text: content.text ?? '',
        toolCards: (content.tool_calls ?? []).map((tc) => ({
          id: String(tc.id ?? ''), tool: String(tc.tool ?? ''),
          input: tc.input, output: tc.output, ok: tc.ok,
        })),
      }
    })
  }

  function newConversation(): void {
    conversationId.value = null
    messages.value = []
  }

  return {
    messages, streaming, conversationId, conversations,
    send, refreshConversations, loadConversation, newConversation,
  }
}
