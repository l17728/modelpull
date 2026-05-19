<script setup lang="ts">
import { computed } from 'vue'
import type { SourceUsed } from '@/api/types'

const props = defineProps<{ sources: SourceUsed[] }>()

const PALETTE = [
  'var(--el-color-primary)', 'var(--el-color-success)',
  'var(--el-color-warning)', 'var(--el-color-danger)',
  'var(--el-color-info)',
]
const segs = computed(() =>
  props.sources.map((s, i) => ({
    ...s,
    color: PALETTE[i % PALETTE.length] ?? 'var(--el-color-primary)',
  })))
</script>

<template>
  <div class="source-bar">
    <div class="bar">
      <div
        v-for="s in segs"
        :key="s.source_id"
        class="seg"
        :style="{ width: `${s.percent}%`, background: s.color }"
        :title="`${s.source_id} ${s.percent}%`"
      />
    </div>
    <ul class="legend">
      <li
        v-for="s in segs"
        :key="s.source_id"
      >
        <span
          class="dot"
          :style="{ background: s.color }"
        />
        {{ s.source_id }} · {{ s.percent }}%
      </li>
    </ul>
  </div>
</template>

<style scoped lang="scss">
.source-bar {
  .bar {
    display: flex;
    height: 16px;
    border-radius: 4px;
    overflow: hidden;
    background: var(--el-fill-color);

    .seg { height: 100%; }
  }
  .legend {
    list-style: none;
    padding: 0;
    margin: 8px 0 0;
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    font-size: 12px;
    color: var(--el-text-color-regular);

    .dot {
      display: inline-block;
      width: 8px;
      height: 8px;
      border-radius: 50%;
      margin-right: 4px;
    }
  }
}
</style>
