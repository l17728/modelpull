import { describe, expect, test } from 'vitest'
import { chunkSegments } from '@/components/taskdetail/segMath'
import type { ChunkSeg } from '@/api/types'

const seg = (i: number, s: number, e: number, st: string,
             done: number): ChunkSeg => ({
  chunk_index: i, byte_start: s, byte_end: e, source_id: 'hf',
  status: st, bytes_done: done,
})

describe('chunkSegments', () => {
  test('empty → []', () => {
    expect(chunkSegments([], 100, 200)).toEqual([])
  })
  test('two equal chunks → x/width proportional, fill ratio', () => {
    const out = chunkSegments(
      [seg(0, 0, 49, 'succeeded', 50), seg(1, 50, 99, 'pending', 25)],
      100, 200)
    expect(out).toHaveLength(2)
    expect(out[0]?.x).toBeCloseTo(0, 5)
    expect(out[0]?.w).toBeCloseTo(100, 5)
    expect(out[0]?.fill).toBeCloseTo(1, 5)
    expect(out[1]?.x).toBeCloseTo(100, 5)
    expect(out[1]?.fill).toBeCloseTo(0.5, 5)
  })
  test('fileSize null → falls back to span sum', () => {
    const out = chunkSegments([seg(0, 0, 99, 'pending', 0)], null, 200)
    expect(out[0]?.w).toBeCloseTo(200, 5)
  })
})
