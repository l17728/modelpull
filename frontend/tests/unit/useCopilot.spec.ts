import { describe, expect, test, vi } from 'vitest'

const { streamChatMock } = vi.hoisted(() => ({ streamChatMock: vi.fn() }))
vi.mock('@/api/aiClient', () => ({
  streamChat: streamChatMock,
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
})
