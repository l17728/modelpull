<script setup lang="ts">
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()
const open = ref(true)

interface ToolItem {
  name: string
  icon: string
  category: 'read' | 'write' | 'external'
}

const TOOLS: ToolItem[] = [
  { name: 'dlw_list_tasks', icon: '📋', category: 'read' },
  { name: 'dlw_get_task', icon: '🔎', category: 'read' },
  { name: 'dlw_get_task_events', icon: '📜', category: 'read' },
  { name: 'dlw_quota_current', icon: '📊', category: 'read' },
  { name: 'hf_api_metadata', icon: '🤗', category: 'external' },
  { name: 'hf_model_card', icon: '🤗', category: 'external' },
  { name: 'search_huggingface_models', icon: '🤗', category: 'external' },
  { name: 'search_modelscope_models', icon: '🔍', category: 'external' },
  { name: 'web_search', icon: '🌐', category: 'external' },
  { name: 'fetch_user_content', icon: '🌐', category: 'external' },
  { name: 'dlw_create_task', icon: '➕', category: 'write' },
  { name: 'dlw_cancel_task', icon: '🛑', category: 'write' },
]
</script>

<template>
  <div
    class="tools-help"
    data-test="copilot-tools-help"
  >
    <div
      class="header"
      @click="open = !open"
    >
      <span class="caret">{{ open ? '▾' : '▸' }}</span>
      <span class="title">🛠 {{ t('copilot.tools.title') }}</span>
      <span class="hint">{{ t('copilot.tools.hint') }}</span>
    </div>
    <div
      v-if="open"
      class="body"
    >
      <p class="intro">
        {{ t('copilot.tools.intro') }}
      </p>
      <div
        v-for="tool in TOOLS"
        :key="tool.name"
        class="tool"
      >
        <div class="tool-row">
          <span class="ic">{{ tool.icon }}</span>
          <code class="name">{{ tool.name }}</code>
          <el-tag
            size="small"
            :type="tool.category === 'write' ? 'warning'
              : tool.category === 'external' ? 'info' : 'success'"
          >
            {{ t(`copilot.tools.cat.${tool.category}`) }}
          </el-tag>
        </div>
        <div class="desc">
          {{ t(`copilot.tools.descs.${tool.name}`) }}
        </div>
        <div class="example">
          <span class="ex-label">{{ t('copilot.tools.example') }}：</span>
          <span class="ex-text">{{ t(`copilot.tools.examples.${tool.name}`) }}</span>
        </div>
      </div>
      <p class="footnote">
        {{ t('copilot.tools.footnote') }}
      </p>
    </div>
  </div>
</template>

<style lang="scss" scoped>
.tools-help {
  border: 1px solid var(--dlw-border);
  border-radius: 8px;
  background: var(--dlw-surface);
  margin-bottom: var(--dlw-space-2);
  font-size: 13px;
}
.header {
  display: flex; align-items: center; gap: 6px;
  padding: 8px 10px; cursor: pointer;
  user-select: none;
}
.header:hover { background: var(--el-fill-color-light); }
.caret { width: 12px; color: var(--dlw-text-soft); }
.title { font-weight: 600; }
.hint { margin-left: auto; color: var(--dlw-text-soft); font-size: 12px; }
.body { padding: 4px 12px 10px; }
.intro { margin: 4px 0 8px; color: var(--dlw-text-soft); font-size: 12px; }
.tool { padding: 6px 0; border-top: 1px dashed var(--dlw-border); }
.tool:first-of-type { border-top: none; }
.tool-row { display: flex; align-items: center; gap: 6px; }
.ic { width: 18px; text-align: center; }
.name { font-family: var(--dlw-font-mono, monospace); font-size: 12px; color: var(--el-color-primary); }
.desc { padding-left: 24px; color: var(--dlw-text); font-size: 12px; margin: 2px 0; }
.example { padding-left: 24px; font-size: 12px; color: var(--dlw-text-soft); }
.ex-label { font-weight: 600; }
.ex-text { font-style: italic; }
.footnote { margin: 8px 0 0; color: var(--dlw-text-soft); font-size: 11px; }
</style>
