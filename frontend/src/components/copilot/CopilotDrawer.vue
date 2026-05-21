<script setup lang="ts">
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useUiStore } from '@/stores/ui'
import { useCopilot } from '@/composables/useCopilot'
import CopilotConfirmCard from '@/components/copilot/CopilotConfirmCard.vue'

const { t } = useI18n()
const ui = useUiStore()
const copilot = useCopilot()
const input = ref('')

watch(() => ui.copilotOpen, (open) => {
  if (open) void copilot.refreshConversations()
})

async function onSend() {
  const text = input.value
  input.value = ''
  await copilot.send(text)
}
</script>

<template>
  <el-drawer
    v-model="ui.copilotOpen"
    :title="t('copilot.title')"
    direction="rtl"
    size="420px"
    data-test="copilot-drawer"
  >
    <div class="copilot">
      <div class="conv-bar">
        <el-button
          size="small"
          data-test="copilot-new"
          @click="copilot.newConversation()"
        >
          {{ t('copilot.newChat') }}
        </el-button>
        <el-select
          v-if="copilot.conversations.value.length"
          :placeholder="t('copilot.history')"
          size="small"
          data-test="copilot-history"
          @change="copilot.loadConversation($event)"
        >
          <el-option
            v-for="c in copilot.conversations.value"
            :key="c.id"
            :label="c.title || c.id.slice(0, 8)"
            :value="c.id"
          />
        </el-select>
      </div>

      <div
        class="messages"
        data-test="copilot-messages"
      >
        <div
          v-if="!copilot.messages.value.length"
          class="empty"
        >
          {{ t('copilot.empty') }}
        </div>
        <div
          v-for="(m, i) in copilot.messages.value"
          :key="i"
          class="msg"
          :class="m.role"
          :data-test="`copilot-msg-${m.role}`"
        >
          <div class="bubble">
            {{ m.text }}
          </div>
          <div
            v-for="card in m.toolCards"
            :key="card.id"
            class="tool-card"
            data-test="copilot-tool-card"
          >
            <div class="tool-name">
              🛠 {{ card.tool }}
              <el-tag
                v-if="card.ok !== undefined"
                size="small"
                :type="card.ok ? 'success' : 'danger'"
              >
                {{ card.ok ? 'ok' : 'err' }}
              </el-tag>
            </div>
            <pre class="tool-out">{{ JSON.stringify(card.output ?? card.input, null, 2) }}</pre>
          </div>
          <CopilotConfirmCard
            v-if="m.pendingConfirm"
            :pending="m.pendingConfirm"
            @approve="copilot.confirm('approved')"
            @reject="copilot.confirm('rejected')"
            @modify="(mi) => copilot.confirm('modified', mi)"
          />
        </div>
      </div>

      <div class="composer">
        <el-input
          v-model="input"
          type="textarea"
          :rows="2"
          :placeholder="t('copilot.placeholder')"
          :disabled="copilot.streaming.value || copilot.hasPendingConfirm.value"
          data-test="copilot-input"
          @keydown.enter.exact.prevent="onSend"
        />
        <el-button
          type="primary"
          :loading="copilot.streaming.value"
          :disabled="copilot.hasPendingConfirm.value"
          data-test="copilot-send"
          @click="onSend"
        >
          {{ t('copilot.send') }}
        </el-button>
      </div>
    </div>
  </el-drawer>
</template>

<style lang="scss" scoped>
.copilot { display: flex; flex-direction: column; height: 100%; gap: var(--dlw-space-2); }
.conv-bar { display: flex; gap: var(--dlw-space-2); align-items: center; }
.messages { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: var(--dlw-space-2); }
.empty { color: var(--dlw-text-soft); text-align: center; padding: var(--dlw-space-4); }
.msg { display: flex; flex-direction: column; gap: 4px; }
.msg.user { align-items: flex-end; }
.bubble { padding: 8px 12px; border-radius: 8px; background: var(--dlw-surface); max-width: 90%; white-space: pre-wrap; }
.msg.user .bubble { background: var(--el-color-primary-light-8); }
.tool-card { border: 1px solid var(--dlw-border); border-radius: 6px; padding: 6px 8px; font-size: 12px; }
.tool-name { font-weight: 600; display: flex; gap: 6px; align-items: center; }
.tool-out { margin: 4px 0 0; max-height: 160px; overflow: auto; white-space: pre-wrap; }
.composer { display: flex; gap: var(--dlw-space-2); align-items: flex-end; }
</style>
