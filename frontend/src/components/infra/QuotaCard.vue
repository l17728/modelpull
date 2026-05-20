<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { formatBytes } from '@/utils/format'

type Fmt = 'bytes' | 'gb' | 'count'
const props = defineProps<{
  label: string
  used: number
  quota: number
  format: Fmt
}>()
const { t } = useI18n()

const pct = computed(() => {
  if (props.quota <= 0) return 0
  return Math.min(100, Math.round((props.used / props.quota) * 100))
})
const showWarn = computed(() => pct.value >= 85 && pct.value < 100)
const showOver = computed(() => pct.value >= 100)
function fmt(n: number): string {
  if (props.format === 'bytes') return formatBytes(n)
  if (props.format === 'gb') return formatBytes(n * 1024 ** 3)
  return String(n)
}
</script>

<template>
  <div class="quota-card">
    <div class="head">
      <span class="lbl">{{ label }}</span>
      <span
        v-if="showOver"
        class="chip over"
      >{{ t('quotaPage.threshold.over') }}</span>
      <span
        v-else-if="showWarn"
        class="chip warn"
      >{{ t('quotaPage.threshold.warn') }}</span>
    </div>
    <div class="val">
      <span class="used">{{ fmt(used) }}</span>
      <span class="sep">/</span>
      <span class="quota">{{ fmt(quota) }}</span>
      <span class="pct">{{ pct }}%</span>
    </div>
    <el-progress
      :percentage="pct"
      :status="showOver ? 'exception' : (showWarn ? 'warning' : undefined)"
      :show-text="false"
    />
  </div>
</template>

<style scoped lang="scss">
.quota-card {
  padding: 16px;
  border-radius: 6px;
  background: var(--el-fill-color-lighter);

  .head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    .lbl { font-size: 13px; color: var(--el-text-color-regular); }
    .chip {
      font-size: 11px;
      padding: 2px 8px;
      border-radius: 10px;
      &.warn { background: var(--el-color-warning-light-8);
               color: var(--el-color-warning); }
      &.over { background: var(--el-color-danger-light-8);
               color: var(--el-color-danger); }
    }
  }
  .val {
    margin: 8px 0;
    display: flex;
    align-items: baseline;
    gap: 6px;
    .used { font-size: 20px; font-weight: 600;
            color: var(--el-text-color-primary); }
    .sep { color: var(--el-text-color-secondary); }
    .quota { color: var(--el-text-color-regular); }
    .pct {
      margin-left: auto;
      font-size: 13px;
      color: var(--el-text-color-regular);
    }
  }
}
</style>
