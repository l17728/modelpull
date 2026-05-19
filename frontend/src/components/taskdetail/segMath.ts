import type { ChunkSeg } from '@/api/types'

export interface Seg {
  x: number
  w: number
  fill: number
  status: string
  source_id: string
  chunk_index: number
}

/** Lay out chunk byte-ranges into [0,totalWidth] px, with fill ratio. */
export function chunkSegments(
  chunks: ChunkSeg[], fileSize: number | null, totalWidth: number,
): Seg[] {
  if (chunks.length === 0) return []
  const spanSum = chunks.reduce(
    (a, c) => a + (c.byte_end - c.byte_start + 1), 0)
  const total = fileSize && fileSize > 0 ? fileSize : spanSum
  if (total <= 0) return []
  const out: Seg[] = []
  for (const c of chunks) {
    const span = c.byte_end - c.byte_start + 1
    const x = (c.byte_start / total) * totalWidth
    const w = (span / total) * totalWidth
    const fill = span > 0 ? Math.min(1, Math.max(0, c.bytes_done / span)) : 0
    out.push({
      x, w, fill, status: c.status, source_id: c.source_id,
      chunk_index: c.chunk_index,
    })
  }
  return out
}

/** Element-Plus status-token color for a chunk status. */
export function segColor(status: string): string {
  if (status === 'succeeded' || status === 'done') {
    return 'var(--el-color-success)'
  }
  if (status === 'failed') return 'var(--el-color-danger)'
  if (status === 'pending') return 'var(--el-color-info)'
  return 'var(--el-color-primary)'
}
