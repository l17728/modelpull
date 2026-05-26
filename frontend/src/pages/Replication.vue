<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import DataBoundary from '@/components/DataBoundary.vue'
import { client } from '@/api/client'
import { useReplicationJobs } from '@/composables/useReplicationJobs'
import type { ReplicationJob, ReplicationJobStatus } from '@/api/types'
import { REPLICATION_TERMINAL } from '@/api/types'

const { t } = useI18n()

const statusFilter = ref<string>('')
const { data, isLoading, isError, refetch } = useReplicationJobs(
  statusFilter.value || undefined,
)

const items = computed<ReplicationJob[]>(() => data.value?.items ?? [])

const createDialogOpen = ref(false)
const createForm = ref({ source_object_id: 0, target_storage_id: 0 })
const creating = ref(false)

async function openCreate() {
  createForm.value = { source_object_id: 0, target_storage_id: 0 }
  createDialogOpen.value = true
}

async function submitCreate() {
  if (!createForm.value.source_object_id || !createForm.value.target_storage_id) {
    ElMessage.warning(t('replication.bothIdsRequired'))
    return
  }
  creating.value = true
  try {
    await client.post('/api/v1/replication', createForm.value)
    ElMessage.success(t('replication.created'))
    createDialogOpen.value = false
    refetch()
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err)
    ElMessage.error(`${t('replication.createFailed')}: ${msg}`)
  } finally {
    creating.value = false
  }
}

async function cancelJob(job: ReplicationJob) {
  if (REPLICATION_TERMINAL.has(job.status)) return
  try {
    await ElMessageBox.confirm(
      t('replication.cancelConfirmBody', { id: job.id }),
      t('replication.cancelConfirmTitle'),
      { confirmButtonText: t('common.confirm'),
        cancelButtonText: t('common.cancel'),
        type: 'warning' })
  } catch {
    return
  }
  try {
    await client.post(`/api/v1/replication/${job.id}/cancel`)
    ElMessage.success(t('replication.cancelled'))
    refetch()
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err)
    ElMessage.error(`${t('replication.cancelFailed')}: ${msg}`)
  }
}

function statusTagType(s: ReplicationJobStatus) {
  switch (s) {
    case 'succeeded': return 'success'
    case 'running':   return 'primary'
    case 'failed':    return 'danger'
    case 'cancelled': return 'info'
    case 'skipped_existing': return 'warning'
    default: return 'info'
  }
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`
}
</script>

<template>
  <div class="page-container">
    <div class="header-row">
      <h2>{{ t('replication.heading') }}</h2>
      <div class="actions">
        <el-select
          v-model="statusFilter"
          :placeholder="t('replication.allStatuses')"
          clearable
          style="width: 180px"
          @change="refetch()"
        >
          <el-option
            label="pending"
            value="pending"
          />
          <el-option
            label="running"
            value="running"
          />
          <el-option
            label="succeeded"
            value="succeeded"
          />
          <el-option
            label="failed"
            value="failed"
          />
          <el-option
            label="cancelled"
            value="cancelled"
          />
          <el-option
            label="skipped_existing"
            value="skipped_existing"
          />
        </el-select>
        <el-button
          type="primary"
          data-test="replication-create-button"
          @click="openCreate"
        >
          ＋ {{ t('replication.createButton') }}
        </el-button>
      </div>
    </div>

    <DataBoundary
      :loading="isLoading"
      :error="isError"
      :is-empty="items.length === 0"
      :empty-message="t('replication.empty')"
      style="margin-top: 16px"
    >
      <el-table
        :data="items"
        stripe
        size="small"
        data-test="replication-table"
      >
        <el-table-column
          prop="id"
          :label="t('replication.col.id')"
          width="80"
        />
        <el-table-column
          prop="source_object_id"
          :label="t('replication.col.source')"
          width="120"
        />
        <el-table-column
          prop="target_storage_id"
          :label="t('replication.col.target')"
          width="120"
        />
        <el-table-column
          :label="t('replication.col.status')"
          width="160"
        >
          <template #default="{ row }">
            <el-tag
              :type="statusTagType(row.status)"
              size="small"
            >
              {{ row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column
          :label="t('replication.col.bytes')"
          width="140"
        >
          <template #default="{ row }">
            {{ formatBytes(row.bytes_transferred) }}
          </template>
        </el-table-column>
        <el-table-column
          prop="retry_count"
          :label="t('replication.col.retry')"
          width="80"
        />
        <el-table-column
          prop="created_at"
          :label="t('replication.col.created')"
          min-width="180"
        />
        <el-table-column
          prop="error_message"
          :label="t('replication.col.error')"
          min-width="200"
          show-overflow-tooltip
        />
        <el-table-column
          :label="t('common.actions')"
          width="120"
          fixed="right"
        >
          <template #default="{ row }">
            <el-button
              v-if="!REPLICATION_TERMINAL.has(row.status)"
              size="small"
              type="warning"
              data-test="replication-cancel"
              @click="cancelJob(row)"
            >
              {{ t('common.cancel') }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </DataBoundary>

    <el-dialog
      v-model="createDialogOpen"
      :title="t('replication.createTitle')"
      width="420"
    >
      <el-form label-position="top">
        <el-form-item :label="t('replication.sourceObjectId')">
          <el-input-number
            v-model="createForm.source_object_id"
            :min="1"
            data-test="replication-create-source"
          />
        </el-form-item>
        <el-form-item :label="t('replication.targetStorageId')">
          <el-input-number
            v-model="createForm.target_storage_id"
            :min="1"
            data-test="replication-create-target"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createDialogOpen = false">
          {{ t('common.cancel') }}
        </el-button>
        <el-button
          type="primary"
          :loading="creating"
          data-test="replication-create-submit"
          @click="submitCreate"
        >
          {{ t('common.confirm') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.header-row {
  display: flex; align-items: center; gap: 16px;
  h2 { flex: 1; margin: 0; }
  .actions { display: flex; gap: 12px; }
}
</style>
