<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { formatBytes } from '@/utils/format'
import type { ParticipatingExecutor } from '@/api/types'

const props = defineProps<{ executor: ParticipatingExecutor }>()
const { t } = useI18n()

type ElTagType = 'success' | 'warning' | 'danger' | 'info'
const tagType = computed<ElTagType>(() => {
  switch (props.executor.executor_status) {
    case 'healthy': return 'success'
    case 'degraded': return 'warning'
    case 'suspect': return 'warning'
    case 'faulty': return 'danger'
    default: return 'info'
  }
})
const statusLabel = computed(() =>
  props.executor.executor_status ?? t('tasks.detail.unknown'))
</script>

<template>
  <div class="swimlane">
    <span class="eid">{{ executor.executor_id }}</span>
    <el-tag
      :type="tagType"
      size="small"
      disable-transitions
    >
      {{ statusLabel }}
    </el-tag>
    <span class="m">
      {{ t('tasks.detail.active') }}: {{ executor.active_subtasks }} /
      {{ executor.assigned_subtasks }}
    </span>
    <span
      v-if="executor.health_score !== null"
      class="m"
    >
      {{ t('tasks.detail.health') }}: {{ executor.health_score }}
    </span>
    <span class="m">{{ formatBytes(executor.bytes_downloaded) }}</span>
  </div>
</template>

<style scoped lang="scss">
.swimlane {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  font-size: 13px;

  .eid {
    font-weight: 600;
    min-width: 140px;
  }
  .m { color: var(--el-text-color-regular); }
}
</style>
