<script setup lang="ts">
import { computed, toRef } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'

import StatusBadge from '@/components/StatusBadge.vue'
import EmptyState from '@/components/EmptyState.vue'
import { useTaskDetail } from '@/composables/useTaskDetail'
import { TERMINAL_STATUSES } from '@/api/types'

const props = defineProps<{ id: string }>()

const { t } = useI18n()
const router = useRouter()
const taskIdRef = toRef(() => props.id)
const { data, isLoading, isError, error } = useTaskDetail(taskIdRef)

const isPolling = computed(() => {
  if (!data.value) return true
  return !TERMINAL_STATUSES.has(data.value.status)
})

const is404 = computed(() => {
  return (error.value as { response?: { status?: number } } | null)?.response?.status === 404
})

function formatDate(iso: string | null) {
  return iso ? new Date(iso).toLocaleString('zh-CN') : '—'
}

function formatBytes(n: number | null) {
  if (n === null || n === undefined) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let v = n
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

function shortSha(s: string | null) {
  return s ? `${s.slice(0, 12)}…` : '—'
}

function back() {
  router.push('/')
}
</script>

<template>
  <div class="page-container">
    <el-page-header @back="back">
      <template #content>
        {{ t('tasks.detailHeading') }}
      </template>
    </el-page-header>

    <el-skeleton
      v-if="isLoading"
      :rows="6"
      animated
      style="margin-top: 24px"
    />

    <EmptyState
      v-else-if="is404"
      :message="t('tasks.notFound')"
    >
      <el-button @click="back">
        {{ t('tasks.back') }}
      </el-button>
    </EmptyState>

    <el-alert
      v-else-if="isError"
      type="error"
      :title="t('errors.service_unavailable')"
      :closable="false"
      style="margin-top: 24px"
    />

    <template v-else-if="data">
      <el-card
        class="summary"
        style="margin-top: 24px"
      >
        <el-descriptions
          :column="2"
          border
        >
          <el-descriptions-item label="ID">
            {{ data.id }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('tasks.columns.repo')">
            {{ data.repo_id }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('tasks.columns.revision')">
            {{ data.revision }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('tasks.columns.status')">
            <StatusBadge :status="data.status" />
          </el-descriptions-item>
          <el-descriptions-item :label="t('tasks.columns.createdAt')">
            {{ formatDate(data.created_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="completed_at">
            {{ formatDate(data.completed_at) }}
          </el-descriptions-item>
          <el-descriptions-item
            v-if="data.error_message"
            label="error"
            :span="2"
          >
            <pre class="error">{{ data.error_message }}</pre>
          </el-descriptions-item>
        </el-descriptions>

        <div
          class="polling-indicator"
          :class="{ active: isPolling }"
        >
          <span class="dot" />
          {{ isPolling ? t('tasks.polling') : t('tasks.completed') }}
        </div>
      </el-card>

      <h3 style="margin-top: 24px">
        {{ t('tasks.subtasksHeading') }}
      </h3>
      <el-table
        :data="data.subtasks"
        stripe
        style="width: 100%"
      >
        <el-table-column
          :label="t('tasks.subtaskColumns.filename')"
          prop="filename"
          min-width="240"
        />
        <el-table-column
          :label="t('tasks.subtaskColumns.size')"
          width="140"
        >
          <template #default="{ row }">
            {{ formatBytes(row.file_size) }}
          </template>
        </el-table-column>
        <el-table-column
          :label="t('tasks.subtaskColumns.sha256')"
          width="180"
        >
          <template #default="{ row }">
            {{ shortSha(row.expected_sha256) }}
          </template>
        </el-table-column>
        <el-table-column
          :label="t('tasks.subtaskColumns.status')"
          width="140"
        >
          <template #default="{ row }">
            <StatusBadge :status="row.status" />
          </template>
        </el-table-column>
      </el-table>
    </template>
  </div>
</template>

<style lang="scss" scoped>
.summary {
  .polling-indicator {
    margin-top: 12px;
    color: #909399;
    font-size: 12px;
    display: flex;
    align-items: center;
    gap: 6px;

    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #c0c4cc;
    }
    &.active .dot {
      background: var(--el-color-primary);
      animation: pulse 1.4s ease-in-out infinite;
    }
  }

  .error {
    margin: 0;
    white-space: pre-wrap;
    color: var(--el-color-danger);
    font-size: 12px;
  }
}

@keyframes pulse {
  0%, 100% { opacity: 0.4; }
  50%      { opacity: 1; }
}
</style>
