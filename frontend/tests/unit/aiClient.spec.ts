import { describe, expect, test, vi, beforeEach } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { streamChat, type ChatEvent } from '@/api/aiClient'

function sseStream(frames: string[]): ReadableStream<Uint8Array> {
  const enc = new TextEncoder()
  return new ReadableStream({
    start(controller) {
      for (const f of frames) controller.enqueue(enc.encode(f))
      controller.close()
    },
  })
}

describe('aiClient.streamChat (SP4a)', () => {
  beforeEach(() => { setActivePinia(createPinia()) })

  test('parses event/data frames and invokes onEvent in order', async () => {
    const body = sseStream([
      ':open\n\n',
      'event: assistant.thinking\ndata: {"text":"hm"}\n\n',
      'event: tool_call\ndata: {"id":"c1","tool":"dlw_list_tasks"}\n\n',
      'event: tool_result\ndata: {"id":"c1","ok":true,"output":{"items":[]}}\n\n',
      'event: assistant.message_delta\ndata: {"text":"done"}\n\n',
      'event: done\ndata: {"conversation_id":"abc"}\n\n',
    ])
    const respond = async () => new Response(body, {
      status: 200, headers: { 'content-type': 'text/event-stream' },
    })
    const fetchImpl = vi.fn(respond) as unknown as typeof fetch
    const got: ChatEvent[] = []
    await streamChat({ message: 'list my tasks', onEvent: (e) => got.push(e),
                       fetchImpl })
    expect(got.map((e) => e.event)).toEqual([
      'assistant.thinking', 'tool_call', 'tool_result',
      'assistant.message_delta', 'done'])
    expect(got.at(-1)?.data.conversation_id).toBe('abc')
  })

  test('non-200 yields an error event', async () => {
    const respond = async () => new Response('nope', { status: 500 })
    const fetchImpl = vi.fn(respond) as unknown as typeof fetch
    const got: ChatEvent[] = []
    await streamChat({ message: 'x', onEvent: (e) => got.push(e), fetchImpl })
    expect(got[0]?.event).toBe('error')
  })
})
