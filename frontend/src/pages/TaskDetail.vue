<script setup lang="ts">
import { computed, ref, toRef } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'

import DataBoundary from '@/components/DataBoundary.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import AggregateRing from '@/components/taskdetail/AggregateRing.vue'
import SpeedEta from '@/components/taskdetail/SpeedEta.vue'
import SourceBar from '@/components/taskdetail/SourceBar.vue'
import ChunkBar from '@/components/taskdetail/ChunkBar.vue'
import SwimLane from '@/components/taskdetail/SwimLane.vue'
import EventRow from '@/components/taskdetail/EventRow.vue'

import { useTaskDetail } from '@/composables/useTaskDetail'
import { useSubtaskChunks } from '@/composables/useSubtaskChunks'
import { useSourceAllocation } from '@/composables/useSourceAllocation'
import { useParticipatingExecutors } from '@/composables/useParticipatingExecutors'
import { useTaskEvents, fetchOlderEvents } from '@/composables/useTaskEvents'
import { useTaskMutations, canCancel, canDelete } from '@/composables/useTaskMutations'
import { useDownloadRate } from '@/composables/useDownloadRate'
import { formatBytes } from '@/utils/format'
import { TERMINAL_STATUSES } from '@/api/types'
import type { TaskEventItem } from '@/api/types'

const props = defineProps<{ id: string }>()
const { t } = useI18n()
const router = useRouter()
const taskIdRef = toRef(() => props.id)

const { data, isLoading, isError, error } = useTaskDetail(taskIdRef)

const is404 = computed(() =>
  (error.value as { response?: { status?: number } } | null)
    ?.response?.status === 404)

const terminal = computed(() =>
  !!data.value && TERMINAL_STATUSES.has(data.value.status))

const activeTab = ref('files')
const onFiles = computed(() => activeTab.value === 'files')
const onSources = computed(() => activeTab.value === 'sources')
const onExecutors = computed(() => activeTab.value === 'executors')
const onEvents = computed(() => activeTab.value === 'events')

const chunks = useSubtaskChunks(taskIdRef, onFiles, terminal)
const alloc = useSourceAllocation(taskIdRef, onSources, terminal)
const execs = useParticipatingExecutors(taskIdRef, onExecutors, terminal)
const events = useTaskEvents(taskIdRef, onEvents, terminal)

const agg = computed(() => {
  const items = chunks.data.value?.items ?? []
  let bd = 0
  let bt = 0
  let fdone = 0
  for (const s of items) {
    bd += s.bytes_downloaded
    bt += s.file_size ?? 0
    if (s.status === 'succeeded') fdone += 1
  }
  const pct = bt > 0 ? (bd / bt) * 100 : 0
  return {
    percent: pct, bytesDone: bd, bytesTotal: bt > 0 ? bt : null,
    filesDone: fdone, filesTotal: items.length,
  }
})
const bytesDoneRef = computed(() => agg.value.bytesDone)
const bytesTotalRef = computed(() => agg.value.bytesTotal)
const { rate } = useDownloadRate(bytesDoneRef, bytesTotalRef)

const mutations = useTaskMutations()
const showCancel = computed(() =>
  !!data.value && canCancel(data.value.status))
const showDelete = computed(() =>
  !!data.value && canDelete(data.value.status))

async function doCancel() {
  if (!data.value) return
  await ElMessageBox.confirm(t('tasks.detail.cancelConfirm'), '', {
    type: 'warning',
  })
  mutations.cancel.mutate(data.value.id)
  ElMessage.success(t('tasks.detail.cancelled'))
}
async function doDelete() {
  if (!data.value) return
  await ElMessageBox.confirm(t('tasks.detail.deleteConfirm'), '', {
    type: 'warning',
  })
  mutations.remove.mutate(data.value.id)
  ElMessage.success(t('tasks.detail.deleted'))
  router.push('/tasks')
}

const older = ref<TaskEventItem[]>([])
const allEvents = computed<TaskEventItem[]>(() =>
  [...(events.data.value?.items ?? []), ...older.value])
const nextCursor = computed(() => events.data.value?.next_cursor ?? null)
const loadingOlder = ref(false)
async function loadOlder() {
  if (!nextCursor.value) return
  loadingOlder.value = true
  try {
    const page = await fetchOlderEvents(props.id, nextCursor.value)
    older.value = [...older.value, ...page.items]
  } finally {
    loadingOlder.value = false
  }
}

function back() {
  router.push('/tasks')
}
function fmtDate(iso: string | null) {
  return iso ? new Date(iso).toLocaleString() : '—'
}
</script>

<template>
  <div class="page-container">
    <el-page-header @back="back">
      <template #content>
        {{ t('tasks.detailHeading') }}
      </template>
    </el-page-header>

    <DataBoundary
      :loading="isLoading"
      :error="isError && !is404"
      :is-empty="is404 || (!isLoading && !data)"
      :empty-message="t('tasks.notFound')"
      style="margin-top: 24px"
    >
      <template #empty-action>
        <el-button @click="back">
          {{ t('tasks.back') }}
        </el-button>
      </template>

      <template v-if="data">
        <el-card class="hdr">
          <div class="hdr-grid">
            <div class="info">
              <el-descriptions
                :column="1"
                size="small"
              >
                <el-descriptions-item label="ID">
                  {{ data.id }}
                </el-descriptions-item>
                <el-descriptions-item :label="t('tasks.columns.repo')">
                  {{ data.repo_id }}
                </el-descriptions-item>
                <el-descriptions-item
                  :label="t('tasks.columns.revision')"
                >
                  {{ data.revision }}
                </el-descriptions-item>
                <el-descriptions-item :label="t('tasks.columns.status')">
                  <StatusBadge :status="data.status" />
                </el-descriptions-item>
                <el-descriptions-item
                  :label="t('tasks.columns.createdAt')"
                >
                  {{ fmtDate(data.created_at) }}
                </el-descriptions-item>
              </el-descriptions>
              <div class="actions">
                <el-button
                  v-if="showCancel"
                  type="warning"
                  @click="doCancel"
                >
                  {{ t('tasks.detail.cancel') }}
                </el-button>
                <el-button
                  v-if="showDelete"
                  type="danger"
                  @click="doDelete"
                >
                  {{ t('tasks.detail.delete') }}
                </el-button>
              </div>
            </div>
            <div class="prog">
              <AggregateRing
                :percent="agg.percent"
                :files-done="agg.filesDone"
                :files-total="agg.filesTotal"
                :bytes-done="agg.bytesDone"
                :bytes-total="agg.bytesTotal"
              />
              <SpeedEta
                :current-bps="rate.currentBps"
                :avg-bps="rate.avgBps"
                :eta-seconds="rate.etaSeconds"
              />
            </div>
          </div>
        </el-card>

        <el-tabs
          v-model="activeTab"
          class="tabs"
        >
          <el-tab-pane
            :label="t('tasks.detail.tabFiles')"
            name="files"
          >
            <DataBoundary
              :loading="chunks.isLoading.value"
              :error="chunks.isError.value"
              :is-empty="(chunks.data.value?.items.length ?? 0) === 0"
              :empty-message="t('tasks.detail.noChunks')"
            >
              <el-table
                :data="chunks.data.value?.items ?? []"
                stripe
                style="width: 100%"
                max-height="520"
              >
                <el-table-column
                  :label="t('tasks.detail.colFile')"
                  prop="filename"
                  min-width="220"
                />
                <el-table-column
                  :label="t('tasks.detail.colSize')"
                  width="120"
                >
                  <template #default="{ row }">
                    {{ formatBytes(row.file_size) }}
                  </template>
                </el-table-column>
                <el-table-column
                  :label="t('tasks.detail.colStatus')"
                  width="130"
                >
                  <template #default="{ row }">
                    <StatusBadge :status="row.status" />
                  </template>
                </el-table-column>
                <el-table-column
                  :label="t('tasks.detail.colChunks')"
                  width="110"
                >
                  <template #default="{ row }">
                    {{ row.chunks_completed }} /
                    {{ row.chunks_total ?? '—' }}
                  </template>
                </el-table-column>
                <el-table-column
                  :label="t('tasks.detail.colProgress')"
                  min-width="300"
                >
                  <template #default="{ row }">
                    <ChunkBar
                      :chunks="row.chunks"
                      :file-size="row.file_size"
                    />
                  </template>
                </el-table-column>
              </el-table>
            </DataBoundary>
          </el-tab-pane>

          <el-tab-pane
            :label="t('tasks.detail.tabSources')"
            name="sources"
          >
            <DataBoundary
              :loading="alloc.isLoading.value"
              :error="alloc.isError.value"
              :is-empty="(alloc.data.value?.sources_used.length ?? 0) === 0"
              :empty-message="t('tasks.detail.noSources')"
            >
              <SourceBar
                :sources="alloc.data.value?.sources_used ?? []"
              />
              <div
                v-for="r in alloc.data.value?.chunk_level_routing ?? []"
                :key="r.filename"
                class="routing"
              >
                <div class="rf">
                  {{ r.filename }}
                </div>
                <ChunkBar
                  :chunks="r.chunks"
                  :file-size="null"
                />
              </div>
            </DataBoundary>
          </el-tab-pane>

          <el-tab-pane
            :label="t('tasks.detail.tabExecutors')"
            name="executors"
          >
            <DataBoundary
              :loading="execs.isLoading.value"
              :error="execs.isError.value"
              :is-empty="(execs.data.value?.items.length ?? 0) === 0"
              :empty-message="t('tasks.detail.noExecutors')"
            >
              <SwimLane
                v-for="e in execs.data.value?.items ?? []"
                :key="e.executor_id"
                :executor="e"
              />
            </DataBoundary>
          </el-tab-pane>

          <el-tab-pane
            :label="t('tasks.detail.tabEvents')"
            name="events"
          >
            <DataBoundary
              :loading="events.isLoading.value"
              :error="events.isError.value"
              :is-empty="allEvents.length === 0"
              :empty-message="t('tasks.detail.noEvents')"
            >
              <EventRow
                v-for="(ev, i) in allEvents"
                :key="`${ev.ts}-${i}`"
                :event="ev"
              />
              <div
                v-if="nextCursor"
                class="load-older"
              >
                <el-button
                  :loading="loadingOlder"
                  @click="loadOlder"
                >
                  {{ t('tasks.detail.loadOlder') }}
                </el-button>
              </div>
            </DataBoundary>
          </el-tab-pane>
        </el-tabs>
      </template>
    </DataBoundary>
  </div>
</template>

<style scoped lang="scss">
.hdr {
  margin-top: 24px;

  .hdr-grid {
    display: flex;
    justify-content: space-between;
    gap: 32px;
    flex-wrap: wrap;
  }
  .actions {
    margin-top: 12px;
    display: flex;
    gap: 8px;
  }
  .prog {
    display: flex;
    flex-direction: column;
    gap: 16px;
    align-items: flex-start;
  }
}
.tabs { margin-top: 24px; }
.routing {
  margin-top: 12px;

  .rf {
    font-size: 12px;
    color: var(--el-text-color-regular);
    margin-bottom: 4px;
  }
}
.load-older {
  text-align: center;
  margin-top: 12px;
}
</style>
