import { computed, ref } from 'vue'
import {
  streamChat, confirmTool, listConversations, getConversation,
  type ConversationSummary,
} from '@/api/aiClient'

export interface ToolCard {
  id: string
  tool: string
  input?: Record<string, unknown>
  output?: Record<string, unknown>
  ok?: boolean
}

export interface PendingConfirm {
  callId: string
  tool: string
  input: Record<string, unknown>
  rationale: string
  estimatedImpact: Record<string, unknown>
}

export interface ChatMessage {
  role: 'user' | 'assistant'
  text: string
  toolCards: ToolCard[]
  pendingConfirm?: PendingConfirm
  quotaExceeded?: boolean
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
          } else if (ev.event === 'tool_call_pending_confirm') {
            assistant.pendingConfirm = {
              callId: String(ev.data.id ?? ''),
              tool: String(ev.data.tool ?? ''),
              input: (ev.data.input as Record<string, unknown>) ?? {},
              rationale: String(ev.data.rationale ?? ''),
              estimatedImpact:
                (ev.data.estimated_quota_impact as Record<string, unknown>) ?? {},
            }
          } else if (ev.event === 'quota_exceeded') {
            assistant.quotaExceeded = true
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

  // UI-layer reinforcement of invariant 17: while a write proposal awaits
  // confirmation, the composer is locked so the user can't start a new turn.
  const hasPendingConfirm = computed(
    () => messages.value.some((m) => m.pendingConfirm !== undefined))

  async function confirm(
    decision: 'approved' | 'rejected' | 'modified',
    modifiedInput?: Record<string, unknown>,
  ): Promise<void> {
    const msg = messages.value.find((m) => m.pendingConfirm !== undefined)
    if (!msg || !msg.pendingConfirm || !conversationId.value) return
    const callId = msg.pendingConfirm.callId
    msg.pendingConfirm = undefined        // resolve the card
    const result: ChatMessage = { role: 'assistant', text: '', toolCards: [] }
    messages.value.push(result)
    streaming.value = true
    try {
      await confirmTool({
        conversationId: conversationId.value, callId, decision, modifiedInput,
        onEvent: (ev) => {
          if (ev.event === 'assistant.message_delta') {
            result.text += String(ev.data.text ?? '')
          } else if (ev.event === 'tool_result') {
            result.toolCards.push({
              id: String(ev.data.id ?? ''), tool: callId,
              ok: Boolean(ev.data.ok),
              output: ev.data.output as Record<string, unknown> | undefined,
            })
          } else if (ev.event === 'error') {
            result.text += `\n[error: ${ev.data.message ?? ev.data.code}]`
          }
        },
      })
    } finally {
      streaming.value = false
    }
  }

  return {
    messages, streaming, conversationId, conversations, hasPendingConfirm,
    send, confirm, refreshConversations, loadConversation, newConversation,
  }
}
