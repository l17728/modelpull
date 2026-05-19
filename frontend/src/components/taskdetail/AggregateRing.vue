<script setup lang="ts">
import { computed } from 'vue'
import { formatBytes } from '@/utils/format'
import { ringDash } from './ringMath'

const props = defineProps<{
  percent: number
  filesDone: number
  filesTotal: number
  bytesDone: number
  bytesTotal: number | null
}>()

const R = 52
const C = 2 * Math.PI * R
const dash = computed(() => ringDash(props.percent, C))
const pctLabel = computed(() => `${Math.round(props.percent)}%`)
</script>

<template>
  <div class="agg-ring">
    <svg
      width="140"
      height="140"
      viewBox="0 0 140 140"
    >
      <circle
        cx="70"
        cy="70"
        :r="R"
        fill="none"
        stroke="var(--el-border-color)"
        stroke-width="12"
      />
      <circle
        cx="70"
        cy="70"
        :r="R"
        fill="none"
        stroke="var(--el-color-primary)"
        stroke-width="12"
        stroke-linecap="round"
        :stroke-dasharray="dash"
        transform="rotate(-90 70 70)"
      />
      <text
        x="70"
        y="76"
        text-anchor="middle"
        class="pct"
      >{{ pctLabel }}</text>
    </svg>
    <div class="meta">
      <div>{{ formatBytes(bytesDone) }} / {{ formatBytes(bytesTotal) }}</div>
      <div>{{ filesDone }} / {{ filesTotal }}</div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.agg-ring {
  display: flex;
  align-items: center;
  gap: 16px;

  .pct {
    font-size: 22px;
    font-weight: 600;
    fill: var(--el-text-color-primary);
  }
  .meta {
    font-size: 13px;
    color: var(--el-text-color-regular);
    line-height: 1.6;
  }
}
</style>
