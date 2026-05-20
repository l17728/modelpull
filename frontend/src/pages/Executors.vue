<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import DataBoundary from '@/components/DataBoundary.vue'
import ExecutorRow from '@/components/infra/ExecutorRow.vue'
import { useExecutors } from '@/composables/useExecutors'
import type { ExecutorRead } from '@/api/types'

const { t } = useI18n()
const statusFilter = ref<string | null>(null)
const { data, isLoading, isError } = useExecutors(statusFilter)

const grouped = computed<Array<{ host: string; items: ExecutorRead[] }>>(() => {
  const items = data.value?.items ?? []
  const by: Record<string, ExecutorRead[]> = {}
  for (const e of items) {
    const k = e.host_id ?? '—'
    const arr = by[k] ?? (by[k] = [])
    arr.push(e)
  }
  return Object.keys(by).sort().map((h) => ({
    host: h, items: by[h] ?? [],
  }))
})
</script>

<template>
  <div class="page-container">
    <h2>{{ t('executors.heading') }}</h2>

    <div class="bar">
      <el-select
        v-model="statusFilter"
        :placeholder="t('executors.filterStatus')"
        clearable
        size="small"
        style="width: 180px"
      >
        <el-option
          v-for="s in ['joining','healthy','degraded','suspect','faulty']"
          :key="s"
          :value="s"
          :label="t(`executors.${s}`)"
        />
      </el-select>
    </div>

    <DataBoundary
      :loading="isLoading"
      :error="isError"
      :is-empty="grouped.length === 0"
      :empty-message="t('executors.empty')"
      style="margin-top: 16px"
    >
      <div
        v-for="g in grouped"
        :key="g.host"
        class="host-group"
      >
        <div class="host-hdr">
          <span class="hk">{{ t('executors.host') }}</span>
          <span class="hv">{{ g.host }}</span>
          <span class="hn">({{ g.items.length }})</span>
        </div>
        <ExecutorRow
          v-for="e in g.items"
          :key="e.id"
          :executor="e"
        />
      </div>
    </DataBoundary>
  </div>
</template>

<style scoped lang="scss">
.bar { margin-top: 16px; }
.host-group {
  margin-top: 16px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  overflow: hidden;
}
.host-hdr {
  padding: 8px 12px;
  background: var(--el-fill-color-lighter);
  display: flex;
  gap: 8px;
  align-items: center;
  font-size: 13px;
  .hk { color: var(--el-text-color-secondary); }
  .hv { font-weight: 600; }
  .hn { color: var(--el-text-color-secondary); }
}
</style>
