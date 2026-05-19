import { describe, expect, test } from 'vitest'
import { mount } from '@vue/test-utils'
import ChunkBar from '@/components/taskdetail/ChunkBar.vue'
import type { ChunkSeg } from '@/api/types'

const chunks: ChunkSeg[] = [
  { chunk_index: 0, byte_start: 0, byte_end: 49, source_id: 'hf',
    status: 'succeeded', bytes_done: 50 },
  { chunk_index: 1, byte_start: 50, byte_end: 99, source_id: 'ms',
    status: 'pending', bytes_done: 0 },
]

describe('ChunkBar', () => {
  test('renders one rect group per chunk', () => {
    const w = mount(ChunkBar, { props: { chunks, fileSize: 100 } })
    expect(w.findAll('rect.seg-bg').length).toBe(2)
  })
  test('empty chunks → placeholder, no rects', () => {
    const w = mount(ChunkBar, { props: { chunks: [], fileSize: null } })
    expect(w.findAll('rect.seg-bg').length).toBe(0)
  })
})
