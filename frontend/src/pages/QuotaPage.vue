<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import DataBoundary from '@/components/DataBoundary.vue'
import QuotaCard from '@/components/infra/QuotaCard.vue'
import { useQuota } from '@/composables/useQuota'

const { t } = useI18n()
const { data, isLoading, isError } = useQuota()
</script>

<template>
  <div class="page-container">
    <h2>{{ t('quotaPage.heading') }}</h2>
    <DataBoundary
      :loading="isLoading"
      :error="isError"
      :is-empty="!data"
      :empty-message="t('errors.service_unavailable')"
      style="margin-top: 16px"
    >
      <template v-if="data">
        <div class="grid">
          <QuotaCard
            :label="t('quotaPage.byteUsage')"
            :used="data.bytes_used_month"
            :quota="data.bytes_quota_month"
            format="bytes"
          />
          <QuotaCard
            :label="t('quotaPage.storageUsage')"
            :used="data.storage_gb_used"
            :quota="data.storage_gb_quota"
            format="gb"
          />
          <QuotaCard
            :label="t('quotaPage.concurrentUsage')"
            :used="data.concurrent_tasks"
            :quota="data.concurrent_quota"
            format="count"
          />
        </div>
      </template>
    </DataBoundary>
  </div>
</template>

<style scoped lang="scss">
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 16px;
  margin-top: 16px;
}
</style>
