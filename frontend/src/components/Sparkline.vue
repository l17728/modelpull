<script setup lang="ts">
import { computed } from 'vue'
const props = withDefaults(
  defineProps<{ data: number[]; width?: number; height?: number }>(),
  { width: 240, height: 48 },
)
const points = computed(() => {
  const d = props.data
  const max = Math.max(1, ...d)
  const step = d.length > 1 ? props.width / (d.length - 1) : props.width
  return d.map((v, i) =>
    `${(i * step).toFixed(1)},${(props.height - (v / max) * props.height).toFixed(1)}`).join(' ')
})
</script>

<template>
  <svg
    :width="width"
    :height="height"
    class="sparkline"
  >
    <polyline
      :points="points"
      fill="none"
      stroke="var(--el-color-primary)"
      stroke-width="2"
    />
  </svg>
</template>
