<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import EmptyState from '@/components/EmptyState.vue'

withDefaults(defineProps<{
  loading?: boolean
  error?: boolean
  isEmpty?: boolean
  forbidden?: boolean
  emptyMessage?: string
}>(), { loading: false, error: false, isEmpty: false, forbidden: false,
       emptyMessage: '' })

const { t } = useI18n()
</script>

<template>
  <el-skeleton
    v-if="loading"
    :rows="5"
    animated
  />
  <EmptyState
    v-else-if="forbidden"
    :message="t('errors.forbidden')"
  />
  <el-alert
    v-else-if="error"
    type="error"
    :title="t('errors.service_unavailable')"
    :closable="false"
  />
  <EmptyState
    v-else-if="isEmpty"
    :message="emptyMessage ?? ''"
  >
    <slot name="empty-action" />
  </EmptyState>
  <slot v-else />
</template>
