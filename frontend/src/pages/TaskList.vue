<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'

import StatusBadge from '@/components/StatusBadge.vue'
import EmptyState from '@/components/EmptyState.vue'
import { useTaskList } from '@/composables/useTaskList'
import type { TaskRead } from '@/api/types'

const { t } = useI18n()
const router = useRouter()
const { data, isLoading, isError } = useTaskList()

const items = computed(() => data.value?.items ?? [])

function open(id: string) {
  router.push({ name: 'taskDetail', params: { id } })
}

function shortId(id: string) {
  return id.slice(0, 8)
}

function shortRevision(rev: string) {
  return rev.slice(0, 8)
}

function formatDate(iso: string) {
  return new Date(iso).toLocaleString('zh-CN')
}
</script>

<template>
  <div class="page-container">
    <h2>{{ t('tasks.listHeading') }}</h2>

    <el-table
      v-if="!isLoading && !isError && items.length > 0"
      :data="items"
      stripe
      style="width: 100%"
      @row-click="(row: TaskRead) => open(row.id)"
    >
      <el-table-column
        :label="t('tasks.columns.id')"
        width="120"
      >
        <template #default="{ row }">
          {{ shortId(row.id) }}…
        </template>
      </el-table-column>
      <el-table-column
        prop="repo_id"
        :label="t('tasks.columns.repo')"
        min-width="240"
      />
      <el-table-column
        :label="t('tasks.columns.revision')"
        width="120"
      >
        <template #default="{ row }">
          {{ shortRevision(row.revision) }}…
        </template>
      </el-table-column>
      <el-table-column
        :label="t('tasks.columns.status')"
        width="120"
      >
        <template #default="{ row }">
          <StatusBadge :status="row.status" />
        </template>
      </el-table-column>
      <el-table-column
        :label="t('tasks.columns.createdAt')"
        width="200"
      >
        <template #default="{ row }">
          {{ formatDate(row.created_at) }}
        </template>
      </el-table-column>
      <el-table-column
        :label="t('tasks.columns.actions')"
        width="100"
      >
        <template #default="{ row }">
          <el-button
            link
            type="primary"
            @click.stop="open(row.id)"
          >
            {{ t('tasks.view') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-skeleton
      v-else-if="isLoading"
      :rows="4"
      animated
    />

    <EmptyState
      v-else-if="!isError && items.length === 0"
      :message="t('tasks.empty')"
    />

    <el-alert
      v-else-if="isError"
      type="error"
      :title="t('errors.service_unavailable')"
      :closable="false"
    />
  </div>
</template>
