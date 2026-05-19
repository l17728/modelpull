<script setup lang="ts">
import { computed } from 'vue'
import type { TaskEventItem } from '@/api/types'
import { eventLevel } from './eventLevel'

const props = defineProps<{ event: TaskEventItem }>()

const level = computed(() => eventLevel(props.event.type, props.event.message))
const tagType = computed<'info' | 'warning' | 'danger'>(() =>
  level.value === 'error' ? 'danger'
    : level.value === 'warn' ? 'warning' : 'info')
const ts = computed(() => {
  const d = new Date(props.event.ts)
  return Number.isNaN(d.getTime()) ? props.event.ts : d.toLocaleString()
})
</script>

<template>
  <div
    class="event-row"
    :class="level"
  >
    <span class="ts">{{ ts }}</span>
    <el-tag
      :type="tagType"
      size="small"
      disable-transitions
    >
      {{ level }}
    </el-tag>
    <span class="msg">{{ event.message }}</span>
  </div>
</template>

<style scoped lang="scss">
.event-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
  font-size: 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);

  .ts {
    color: var(--el-text-color-secondary);
    min-width: 168px;
    font-variant-numeric: tabular-nums;
  }
  .msg {
    color: var(--el-text-color-primary);
    word-break: break-word;
  }
  &.error .msg { color: var(--el-color-danger); }
}
</style>
