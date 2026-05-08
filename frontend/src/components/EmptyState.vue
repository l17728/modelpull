<script setup lang="ts">
defineProps<{
  message: string
  description?: string
}>()
</script>

<template>
  <div class="empty-state">
    <div class="icon">
      📭
    </div>
    <div class="message">
      {{ message }}
    </div>
    <div
      v-if="description"
      class="description"
    >
      {{ description }}
    </div>
    <div class="action">
      <slot />
    </div>
  </div>
</template>

<style lang="scss" scoped>
.empty-state {
  text-align: center;
  padding: 64px 24px;
  color: #909399;

  .icon {
    font-size: 48px;
    margin-bottom: 16px;
  }
  .message {
    font-size: 16px;
    color: #606266;
    margin-bottom: 8px;
  }
  .description {
    font-size: 13px;
  }
  .action {
    margin-top: 16px;
  }
}
</style>
