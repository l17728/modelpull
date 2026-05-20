<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { formatDateTime } from '@/utils/format'
import type { AuditEntry } from '@/api/types'

const props = defineProps<{ entry: AuditEntry }>()
const { t } = useI18n()

type ElTagType = 'success' | 'warning' | 'danger' | 'info'
const tagType = computed<ElTagType>(() => {
  if (props.entry.outcome === 'success') return 'success'
  if (props.entry.outcome === 'denied') return 'danger'
  if (props.entry.outcome === 'error') return 'warning'
  return 'info'
})
const actorLabel = computed(() =>
  props.entry.actor_user_id === null
    ? t('audit.systemActor')
    : String(props.entry.actor_user_id))
const shortId = computed(() => {
  const r = props.entry.resource_id
  return r === null ? '—' : (r.length > 16 ? `${r.slice(0, 16)}…` : r)
})
</script>

<template>
  <div class="audit-row">
    <span class="ts">{{ formatDateTime(entry.occurred_at) }}</span>
    <el-tag
      :type="tagType"
      size="small"
      disable-transitions
    >
      {{ entry.outcome }}
    </el-tag>
    <span class="actor">{{ actorLabel }}</span>
    <span class="action">{{ entry.action }}</span>
    <span class="rtype">{{ entry.resource_type }}</span>
    <span
      class="rid"
      :title="entry.resource_id ?? ''"
    >{{ shortId }}</span>
  </div>
</template>

<style scoped lang="scss">
.audit-row {
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
  .actor { color: var(--el-text-color-regular); min-width: 60px; }
  .action { color: var(--el-text-color-primary); font-weight: 500; }
  .rtype { color: var(--el-text-color-regular); }
  .rid {
    color: var(--el-text-color-secondary);
    font-family: var(--el-font-family-monospace, monospace);
  }
}
</style>
