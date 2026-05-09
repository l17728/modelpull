<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps<{ status: string }>()
const { t, te } = useI18n()

type ElTagType = 'info' | 'warning' | 'primary' | 'success' | 'danger'

const TYPE_MAP: Readonly<Record<string, ElTagType>> = {
  pending: 'info',
  queued: 'info',
  cancelled: 'info',
  scheduling: 'warning',
  downloading: 'primary',
  in_progress: 'primary',
  assigned: 'primary',
  succeeded: 'success',
  failed: 'danger',
}

const tagType = computed<ElTagType>(() => TYPE_MAP[props.status] ?? 'info')

const label = computed(() => {
  const key = `status.${props.status}`
  return te(key) ? t(key) : props.status
})
</script>

<template>
  <el-tag
    :type="tagType"
    disable-transitions
  >
    {{ label }}
  </el-tag>
</template>
