<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import DataBoundary from '@/components/DataBoundary.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { useTaskList } from '@/composables/useTaskList'
import { useTaskMutations, canCancel, canDelete } from '@/composables/useTaskMutations'
import { filterTasks } from '@/tasks/filter'
import type { TaskRead } from '@/api/types'

const { t } = useI18n()
const router = useRouter()
const { data, isLoading, isError } = useTaskList()
const { cancel, remove } = useTaskMutations()

const status = ref('')
const q = ref('')
const STATUSES = ['pending', 'scheduling', 'downloading', 'succeeded',
  'failed', 'cancelled']

const rows = computed(() =>
  filterTasks(data.value?.items ?? [], { status: status.value, q: q.value }))

function open(id: string) {
  router.push({ name: 'taskDetail', params: { id } })
}
async function doCancel(row: TaskRead) {
  const ok = await ElMessageBox.confirm(t('tasks.cancelConfirm')).catch(() => null)
  if (ok) {
    await cancel.mutateAsync(row.id)
    ElMessage.success(t('tasks.cancelled'))
  }
}
async function doDelete(row: TaskRead) {
  const ok = await ElMessageBox.confirm(t('tasks.deleteConfirm')).catch(() => null)
  if (ok) {
    await remove.mutateAsync(row.id)
    ElMessage.success(t('tasks.deleted'))
  }
}
function fmt(iso: string) {
  return new Date(iso).toLocaleString()
}
</script>

<template>
  <div class="page-container">
    <div class="bar">
      <h2>{{ t('tasks.listHeading') }}</h2>
      <el-button
        type="primary"
        @click="router.push({ name: 'taskCreate' })"
      >
        {{ t('tasks.create') }}
      </el-button>
    </div>

    <div class="filters">
      <el-select
        v-model="status"
        clearable
        :placeholder="t('tasks.filterStatus')"
        style="width: 160px"
      >
        <el-option
          v-for="s in STATUSES"
          :key="s"
          :label="t(`status.${s}`)"
          :value="s"
        />
      </el-select>
      <el-input
        v-model="q"
        :placeholder="t('tasks.search')"
        style="width: 240px"
        clearable
      />
    </div>

    <DataBoundary
      :loading="isLoading"
      :error="isError"
      :is-empty="rows.length === 0"
      :empty-message="t('tasks.empty')"
    >
      <template #empty-action>
        <el-button
          type="primary"
          @click="router.push({ name: 'taskCreate' })"
        >
          {{ t('tasks.create') }}
        </el-button>
      </template>
      <el-table
        :data="rows"
        stripe
        @row-click="(r: TaskRead) => open(r.id)"
      >
        <el-table-column
          :label="t('tasks.columns.id')"
          width="120"
        >
          <template #default="{ row }">
            {{ row.id.slice(0, 8) }}…
          </template>
        </el-table-column>
        <el-table-column
          prop="repo_id"
          :label="t('tasks.columns.repo')"
          min-width="220"
        />
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
          width="190"
        >
          <template #default="{ row }">
            {{ fmt(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column
          :label="t('tasks.columns.actions')"
          width="200"
        >
          <template #default="{ row }">
            <el-button
              link
              type="primary"
              @click.stop="open(row.id)"
            >
              {{ t('tasks.view') }}
            </el-button>
            <el-button
              v-if="canCancel(row.status)"
              link
              type="warning"
              @click.stop="doCancel(row)"
            >
              {{ t('tasks.cancel') }}
            </el-button>
            <el-button
              v-if="canDelete(row.status)"
              link
              type="danger"
              @click.stop="doDelete(row)"
            >
              {{ t('tasks.delete') }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </DataBoundary>
  </div>
</template>

<style lang="scss" scoped>
.bar { display: flex; justify-content: space-between; align-items: center; }
.filters { display: flex; gap: var(--dlw-space-3); margin: var(--dlw-space-3) 0; }
</style>
