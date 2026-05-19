<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { formatTimeAgo, formatBytes } from '@/utils/format'
import type { ExecutorRead } from '@/api/types'

const props = defineProps<{ executor: ExecutorRead }>()
const { t } = useI18n()

type ElTagType = 'success' | 'warning' | 'danger' | 'info'
const tagType = computed<ElTagType>(() => {
  switch (props.executor.status) {
    case 'healthy': return 'success'
    case 'degraded': return 'warning'
    case 'suspect': return 'warning'
    case 'faulty': return 'danger'
    default: return 'info'
  }
})

const diskPct = computed(() => {
  const free = props.executor.disk_free_gb
  const total = props.executor.disk_total_gb
  if (free === null || total === null || total <= 0) return null
  return Math.max(0, Math.min(100, Math.round((1 - free / total) * 100)))
})

function nicLabel(): string {
  const n = props.executor.nic_speed_gbps
  return n === null ? '—' : `${n} ${t('executors.gbps')}`
}
function diskLabel(): string {
  const free = props.executor.disk_free_gb
  const total = props.executor.disk_total_gb
  if (free === null || total === null) return '—'
  return `${formatBytes(free * 1024 ** 3)} / ${formatBytes(total * 1024 ** 3)}`
}
</script>

<template>
  <div class="exec-row">
    <span class="eid">{{ executor.id }}</span>
    <el-tag
      :type="tagType"
      size="small"
      disable-transitions
    >
      {{ executor.status }}
    </el-tag>
    <span class="m">{{ t('executors.health') }}: {{ executor.health_score }}</span>
    <span class="m">{{ t('executors.lastHeartbeat') }}:
      {{ formatTimeAgo(executor.last_heartbeat_at) }}</span>
    <span class="m">NIC: {{ nicLabel() }}</span>
    <span class="m">{{ t('executors.disk') }}: {{ diskLabel() }}
      <span
        v-if="diskPct !== null"
        class="pct"
      >({{ diskPct }}%)</span>
    </span>
  </div>
</template>

<style scoped lang="scss">
.exec-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  font-size: 13px;

  .eid { font-weight: 600; min-width: 160px; }
  .m { color: var(--el-text-color-regular); }
  .pct { color: var(--el-text-color-secondary); margin-left: 4px; }
}
</style>
