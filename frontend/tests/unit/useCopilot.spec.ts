import { describe, expect, test, vi } from 'vitest'

const { streamChatMock, confirmToolMock } = vi.hoisted(() => ({
  streamChatMock: vi.fn(), confirmToolMock: vi.fn(),
}))
vi.mock('@/api/aiClient', () => ({
  streamChat: streamChatMock,
  confirmTool: confirmToolMock,
  listConversations: vi.fn(async () => []),
  getConversation: vi.fn(),
}))

import { useCopilot } from '@/composables/useCopilot'

describe('useCopilot (SP4a)', () => {
  test('send assembles user + assistant messages with tool card', async () => {
    streamChatMock.mockImplementationOnce(async (opts: {
      onEvent: (e: { event: string; data: Record<string, unknown> }) => void
    }) => {
      opts.onEvent({ event: 'tool_call',
                     data: { id: 'c1', tool: 'dlw_list_tasks', input: {} } })
      opts.onEvent({ event: 'tool_result',
                     data: { id: 'c1', ok: true, output: { items: [] } } })
      opts.onEvent({ event: 'assistant.message_delta', data: { text: 'You ' } })
      opts.onEvent({ event: 'assistant.message_delta',
                     data: { text: 'have 0 tasks.' } })
      opts.onEvent({ event: 'done', data: { conversation_id: 'conv-1' } })
    })
    const c = useCopilot()
    await c.send('list my tasks')
    expect(c.messages.value).toHaveLength(2)
    expect(c.messages.value[0]).toMatchObject({ role: 'user',
                                                text: 'list my tasks' })
    const a = c.messages.value[1]!
    expect(a.role).toBe('assistant')
    expect(a.text).toBe('You have 0 tasks.')
    expect(a.toolCards).toHaveLength(1)
    expect(a.toolCards[0]).toMatchObject({ tool: 'dlw_list_tasks', ok: true })
    expect(c.conversationId.value).toBe('conv-1')
    expect(c.streaming.value).toBe(false)
  })

  test('send ignores empty / whitespace input', async () => {
    streamChatMock.mockClear()
    const c = useCopilot()
    await c.send('   ')
    expect(streamChatMock).not.toHaveBeenCalled()
    expect(c.messages.value).toHaveLength(0)
  })

  test('pending_confirm sets pendingConfirm + hasPendingConfirm; confirm fires phase 2', async () => {
    streamChatMock.mockClear(); confirmToolMock.mockClear()
    streamChatMock.mockImplementationOnce(async (opts: {
      onEvent: (e: { event: string; data: Record<string, unknown> }) => void
    }) => {
      opts.onEvent({ event: 'tool_call_pending_confirm',
                     data: { id: 'call-9', tool: 'dlw_cancel_task',
                             input: { task_id: 't' }, rationale: 'Cancel t.',
                             estimated_quota_impact: {} } })
      opts.onEvent({ event: 'assistant.message_delta',
                     data: { text: 'Please confirm.' } })
      opts.onEvent({ event: 'done', data: { conversation_id: 'conv-9' } })
    })
    const c = useCopilot()
    await c.send('cancel t')
    const a = c.messages.value[1]!
    expect(a.pendingConfirm?.callId).toBe('call-9')
    expect(c.hasPendingConfirm.value).toBe(true)

    confirmToolMock.mockImplementationOnce(async (opts: {
      onEvent: (e: { event: string; data: Record<string, unknown> }) => void
    }) => {
      opts.onEvent({ event: 'tool_result',
                     data: { id: 'call-9', ok: true,
                             output: { status: 'cancelling' } } })
      opts.onEvent({ event: 'assistant.message_delta', data: { text: 'Done.' } })
      opts.onEvent({ event: 'done', data: { conversation_id: 'conv-9' } })
    })
    await c.confirm('approved')
    expect(confirmToolMock).toHaveBeenCalledTimes(1)
    expect(confirmToolMock.mock.calls[0]?.[0]).toMatchObject({
      conversationId: 'conv-9', callId: 'call-9', decision: 'approved' })
    expect(c.hasPendingConfirm.value).toBe(false)   // card resolved
    const last = c.messages.value.at(-1)!
    expect(last.text).toBe('Done.')
    expect(last.toolCards[0]?.ok).toBe(true)
  })

  test('surfaces quota_exceeded as a budget flag and stops streaming', async () => {
    streamChatMock.mockImplementation(async ({ onEvent }: {
      onEvent: (e: { event: string; data: Record<string, unknown> }) => void
    }) => {
      onEvent({ event: 'quota_exceeded',
                data: { metric: 'ai_tokens', remaining: 0 } })
    })
    const c = useCopilot()
    await c.send('hello')
    const last = c.messages.value.at(-1)!
    expect(last.role).toBe('assistant')
    expect(last.quotaExceeded).toBe(true)
    expect(c.streaming.value).toBe(false)
  })
})
