<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ state: string }>()
type ElTagType = 'success' | 'warning' | 'info' | 'danger'
const tagType = computed<ElTagType>(() => {
  if (props.state === 'active') return 'success'
  if (props.state === 'recovering') return 'warning'
  if (props.state === 'standby') return 'info'
  return 'danger'
})
</script>

<template>
  <el-tag
    :type="tagType"
    disable-transitions
    size="small"
  >
    {{ state }}
  </el-tag>
</template>
