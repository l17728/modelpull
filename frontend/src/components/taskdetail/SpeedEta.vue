<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { formatRate, formatDuration } from '@/utils/format'

defineProps<{
  currentBps: number
  avgBps: number
  etaSeconds: number | null
}>()
const { t } = useI18n()
</script>

<template>
  <div class="speed-eta">
    <div>
      <span class="lbl">{{ t('tasks.detail.speedNow') }}</span>
      <span class="val">{{ formatRate(currentBps) }}</span>
    </div>
    <div>
      <span class="lbl">{{ t('tasks.detail.speedAvg') }}</span>
      <span class="val">{{ formatRate(avgBps) }}</span>
    </div>
    <div>
      <span class="lbl">{{ t('tasks.detail.eta') }}</span>
      <span class="val">{{ formatDuration(etaSeconds) }}</span>
    </div>
  </div>
</template>

<style scoped lang="scss">
.speed-eta {
  display: flex;
  gap: 24px;

  .lbl {
    display: block;
    font-size: 12px;
    color: var(--el-text-color-secondary);
  }
  .val {
    font-size: 16px;
    font-weight: 600;
    color: var(--el-text-color-primary);
  }
}
</style>
