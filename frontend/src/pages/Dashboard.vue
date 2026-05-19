<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import DataBoundary from '@/components/DataBoundary.vue'
import Sparkline from '@/components/Sparkline.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { useTaskList } from '@/composables/useTaskList'
import { useQuota } from '@/composables/useQuota'
import { aggregateKpis, bucket24h } from '@/dashboard/aggregate'

const { t } = useI18n()
const router = useRouter()
const { data, isLoading, isError } = useTaskList()
const { data: quota } = useQuota()

const tasks = computed(() => data.value?.items ?? [])
const kpi = computed(() => aggregateKpis(tasks.value))
const trend = computed(() => bucket24h(tasks.value))
const recent = computed(() => tasks.value.slice(0, 8))

function open(id: string) {
  router.push({ name: 'taskDetail', params: { id } })
}
</script>

<template>
  <div class="page-container">
    <h2>{{ t('dashboard.heading') }}</h2>
    <DataBoundary
      :loading="isLoading"
      :error="isError"
    >
      <div class="kpis">
        <el-card>{{ t('dashboard.inProgress') }}<b>{{ kpi.inProgress }}</b></el-card>
        <el-card>{{ t('dashboard.completed') }}<b>{{ kpi.completed }}</b></el-card>
        <el-card>{{ t('dashboard.failed') }}<b>{{ kpi.failed }}</b></el-card>
        <el-card>{{ t('dashboard.total') }}<b>{{ kpi.total }}</b></el-card>
      </div>

      <el-card style="margin-top: 16px">
        <template #header>
          {{ t('dashboard.trend') }}
        </template>
        <Sparkline :data="trend" />
      </el-card>

      <el-card
        v-if="quota"
        style="margin-top: 16px"
      >
        <template #header>
          {{ t('dashboard.quota') }}
        </template>
        <p>
          {{ t('dashboard.quotaBytes') }}: {{ quota.bytes_used_month }} /
          {{ quota.bytes_quota_month }}
        </p>
        <p>
          {{ t('dashboard.quotaConcurrent') }}: {{ quota.concurrent_tasks }} /
          {{ quota.concurrent_quota }}
        </p>
      </el-card>

      <el-card style="margin-top: 16px">
        <template #header>
          {{ t('dashboard.recent') }}
        </template>
        <el-table :data="recent">
          <el-table-column
            prop="repo_id"
            :label="t('tasks.columns.repo')"
          />
          <el-table-column
            :label="t('tasks.columns.status')"
            width="120"
          >
            <template #default="{ row }">
              <StatusBadge :status="row.status" />
            </template>
          </el-table-column>
          <el-table-column width="80">
            <template #default="{ row }">
              <el-button
                link
                type="primary"
                @click="open(row.id)"
              >
                {{ t('tasks.view') }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </DataBoundary>
  </div>
</template>

<style lang="scss" scoped>
.kpis {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--dlw-space-3);
  b { display: block; font-size: 28px; margin-top: 4px; }
}
</style>
