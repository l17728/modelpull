<script setup lang="ts">
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import type { PendingConfirm } from '@/composables/useCopilot'

const props = defineProps<{ pending: PendingConfirm }>()
const emit = defineEmits<{
  approve: []
  reject: []
  modify: [modifiedInput: Record<string, unknown>]
}>()

const { t } = useI18n()
const modifying = ref(false)
const draft = ref(JSON.stringify(props.pending.input, null, 2))
const parseError = ref('')

function submitModify() {
  try {
    const parsed = JSON.parse(draft.value)
    parseError.value = ''
    emit('modify', parsed as Record<string, unknown>)
  } catch (e) {
    parseError.value = String(e)
  }
}
</script>

<template>
  <div
    class="confirm-card"
    data-test="copilot-confirm-card"
  >
    <div class="head">
      🛠 {{ t('copilot.confirm.title', { tool: pending.tool }) }}
    </div>
    <div
      v-if="pending.rationale"
      class="rationale"
    >
      {{ pending.rationale }}
    </div>
    <pre
      v-if="!modifying"
      class="input"
    >{{ JSON.stringify(pending.input, null, 2) }}</pre>
    <template v-else>
      <el-input
        v-model="draft"
        type="textarea"
        :rows="4"
        data-test="copilot-confirm-modify-input"
      />
      <div
        v-if="parseError"
        class="parse-err"
      >
        {{ t('copilot.confirm.modifyHint') }}: {{ parseError }}
      </div>
    </template>
    <div class="actions">
      <template v-if="!modifying">
        <el-button
          type="primary"
          data-test="copilot-confirm-approve"
          @click="emit('approve')"
        >
          {{ t('copilot.confirm.approve') }}
        </el-button>
        <el-button
          data-test="copilot-confirm-modify"
          @click="modifying = true"
        >
          {{ t('copilot.confirm.modify') }}
        </el-button>
        <el-button
          type="danger"
          plain
          data-test="copilot-confirm-reject"
          @click="emit('reject')"
        >
          {{ t('copilot.confirm.reject') }}
        </el-button>
      </template>
      <template v-else>
        <el-button
          type="primary"
          data-test="copilot-confirm-modify-submit"
          @click="submitModify"
        >
          {{ t('copilot.confirm.approve') }}
        </el-button>
        <el-button @click="modifying = false">
          {{ t('copilot.confirm.reject') }}
        </el-button>
      </template>
    </div>
  </div>
</template>

<style lang="scss" scoped>
.confirm-card {
  border: 1px solid var(--el-color-warning); border-radius: 6px;
  padding: 8px 10px; margin-top: 4px; font-size: 13px;
}
.head { font-weight: 600; }
.rationale { color: var(--dlw-text-soft); margin: 4px 0; }
.input { max-height: 160px; overflow: auto; white-space: pre-wrap; margin: 4px 0; }
.parse-err { color: var(--el-color-danger); font-size: 12px; }
.actions { display: flex; gap: 8px; margin-top: 6px; }
</style>
