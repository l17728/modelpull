<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import DataBoundary from '@/components/DataBoundary.vue'
import AuditRow from '@/components/infra/AuditRow.vue'
import { useAuditLog, fetchOlderAudit } from '@/composables/useAuditLog'
import type { AuditEntry } from '@/api/types'

const { t } = useI18n()
const action = ref('')
const actor = ref<number | null>(null)
const from = ref<string | null>(null)
const to = ref<string | null>(null)

const { data, isLoading, isError } = useAuditLog({ action, actor, from, to })
const older = ref<AuditEntry[]>([])
const all = computed<AuditEntry[]>(() =>
  [...(data.value?.items ?? []), ...older.value])
const nextCursor = computed(() => data.value?.next_cursor ?? null)
const loadingOlder = ref(false)

async function loadOlder() {
  if (!nextCursor.value) return
  loadingOlder.value = true
  try {
    const page = await fetchOlderAudit({
      action: action.value, actor: actor.value,
      from: from.value, to: to.value,
    }, nextCursor.value)
    older.value = [...older.value, ...page.items]
  } finally {
    loadingOlder.value = false
  }
}

function reset() {
  action.value = ''
  actor.value = null
  from.value = null
  to.value = null
  older.value = []
}
</script>

<template>
  <div class="page-container">
    <h2>{{ t('audit.heading') }}</h2>
    <div class="bar">
      <el-input
        v-model="action"
        :placeholder="t('audit.filterAction')"
        size="small"
        style="width: 200px"
        clearable
      />
      <el-input-number
        v-model="actor"
        :placeholder="t('audit.filterActor')"
        size="small"
        :min="1"
        :step="1"
        :precision="0"
        controls-position="right"
        style="width: 180px"
      />
      <el-date-picker
        v-model="from"
        type="datetime"
        :placeholder="t('audit.filterFrom')"
        size="small"
        value-format="YYYY-MM-DDTHH:mm:ss[Z]"
      />
      <el-date-picker
        v-model="to"
        type="datetime"
        :placeholder="t('audit.filterTo')"
        size="small"
        value-format="YYYY-MM-DDTHH:mm:ss[Z]"
      />
      <el-button
        size="small"
        @click="reset"
      >
        {{ t('audit.reset') }}
      </el-button>
    </div>

    <DataBoundary
      :loading="isLoading"
      :error="isError"
      :is-empty="all.length === 0"
      :empty-message="t('audit.empty')"
      style="margin-top: 16px"
    >
      <AuditRow
        v-for="(e, i) in all"
        :key="`${e.id}-${i}`"
        :entry="e"
      />
      <div
        v-if="nextCursor"
        class="load-older"
      >
        <el-button
          :loading="loadingOlder"
          @click="loadOlder"
        >
          {{ t('audit.loadOlder') }}
        </el-button>
      </div>
    </DataBoundary>
  </div>
</template>

<style scoped lang="scss">
.bar {
  margin-top: 16px;
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  align-items: center;
}
.load-older {
  text-align: center;
  margin-top: 12px;
}
</style>
