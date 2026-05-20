import { describe, expect, test } from 'vitest'
import { parseSseChunk } from '@/api/sse'

describe('parseSseChunk', () => {
  test('empty → no events, empty remainder', () => {
    expect(parseSseChunk('')).toEqual({ events: [], remainder: '' })
  })
  test('single event', () => {
    const { events, remainder } = parseSseChunk('data: hello\n\n')
    expect(remainder).toBe('')
    expect(events).toHaveLength(1)
    expect(events[0]?.event).toBe('message')
    expect(events[0]?.data).toBe('hello')
  })
  test('two events in one buffer', () => {
    const { events, remainder } = parseSseChunk(
      'data: a\n\ndata: b\n\n')
    expect(remainder).toBe('')
    expect(events.map((e) => e.data)).toEqual(['a', 'b'])
  })
  test('event split across two buffers → remainder carries', () => {
    const a = parseSseChunk('data: hel')
    expect(a.events).toEqual([])
    expect(a.remainder).toBe('data: hel')
    const b = parseSseChunk(a.remainder + 'lo\n\n')
    expect(b.events).toHaveLength(1)
    expect(b.events[0]?.data).toBe('hello')
  })
  test('comment lines ignored', () => {
    const { events } = parseSseChunk(
      ':keepalive\ndata: payload\n\n')
    expect(events).toHaveLength(1)
    expect(events[0]?.data).toBe('payload')
  })
  test('custom event field', () => {
    const { events } = parseSseChunk(
      'event: progress\ndata: 42\n\n')
    expect(events[0]?.event).toBe('progress')
    expect(events[0]?.data).toBe('42')
  })
  test('multi-line data field joined with newline', () => {
    const { events } = parseSseChunk(
      'data: line1\ndata: line2\n\n')
    expect(events[0]?.data).toBe('line1\nline2')
  })
  test('CRLF line endings also accepted', () => {
    const { events } = parseSseChunk(
      'data: x\r\n\r\n')
    expect(events[0]?.data).toBe('x')
  })
})
