<script setup lang="ts">
import { computed } from 'vue'
import type { ChunkSeg } from '@/api/types'
import { chunkSegments, segColor } from './segMath'

const props = withDefaults(defineProps<{
  chunks: ChunkSeg[]
  fileSize: number | null
  width?: number
  height?: number
}>(), { width: 280, height: 14 })

const segs = computed(() =>
  chunkSegments(props.chunks, props.fileSize, props.width))
</script>

<template>
  <svg
    :width="width"
    :height="height"
    class="chunk-bar"
  >
    <g
      v-for="s in segs"
      :key="s.chunk_index"
    >
      <rect
        class="seg-bg"
        :x="s.x"
        y="0"
        :width="Math.max(0, s.w - 1)"
        :height="height"
        rx="2"
        fill="var(--el-fill-color)"
      />
      <rect
        class="seg-fill"
        :x="s.x"
        y="0"
        :width="Math.max(0, (s.w - 1) * s.fill)"
        :height="height"
        rx="2"
        :fill="segColor(s.status)"
      >
        <title>
          chunk {{ s.chunk_index }} · {{ s.source_id }} · {{ s.status }}
        </title>
      </rect>
    </g>
  </svg>
</template>

<style scoped>
.chunk-bar { display: block; }
</style>
