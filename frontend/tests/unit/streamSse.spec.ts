import { describe, expect, test } from 'vitest'
import { streamSse, type SseEvent } from '@/api/sse'

describe('streamSse', () => {
  test('parses events from a ReadableStream and stops on abort', async () => {
    const ac = new AbortController()
    const got: SseEvent[] = []
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(new TextEncoder().encode('data: a\n\n'))
        c.enqueue(new TextEncoder().encode('data: b\n\n'))
        c.close()
      },
    })
    const fetchImpl: typeof fetch = async () => new Response(
      stream, { status: 200 })
    await streamSse({
      url: '/x', token: 't', signal: ac.signal,
      onEvent: (e) => {
        got.push(e)
        if (got.length === 2) ac.abort()
      },
      onUnauthorized: () => {},
      fetchImpl,
    })
    expect(got.map((e) => e.data)).toEqual(['a', 'b'])
  })

  test('401 → calls onUnauthorized and rejects without retry', async () => {
    const ac = new AbortController()
    let unauth = 0
    const fetchImpl: typeof fetch = async () => new Response(
      '', { status: 401 })
    await expect(streamSse({
      url: '/x', token: 't', signal: ac.signal,
      onEvent: () => {},
      onUnauthorized: () => { unauth++ },
      fetchImpl,
    })).rejects.toThrow(/SSE 401/)
    expect(unauth).toBe(1)
  })

  test('404 → resolves without retry (permanent client error)', async () => {
    const ac = new AbortController()
    const fetchImpl: typeof fetch = async () => new Response(
      'not found', { status: 404 })
    await streamSse({
      url: '/x', token: 't', signal: ac.signal,
      onEvent: () => {},
      onUnauthorized: () => {},
      fetchImpl,
    })
  })
})
