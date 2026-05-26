<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'

interface Props {
  tool: string
  input?: Record<string, unknown>
  output?: Record<string, unknown>
  ok?: boolean
}
const props = defineProps<Props>()
const { t } = useI18n()
const expanded = ref(false)

const ICON: Record<string, string> = {
  dlw_list_tasks: '📋', dlw_get_task: '🔎', dlw_get_task_events: '📜',
  dlw_quota_current: '📊', hf_api_metadata: '🤗', hf_model_card: '🤗',
  search_modelscope_models: '🔍', web_search: '🌐', fetch_user_content: '🌐',
  dlw_create_task: '➕', dlw_cancel_task: '🛑',
}

const icon = computed(() => ICON[props.tool] ?? '🛠')

const inputSummary = computed(() => {
  if (!props.input) return ''
  const parts: string[] = []
  for (const [k, v] of Object.entries(props.input)) {
    if (v === null || v === undefined) continue
    const s = typeof v === 'string' ? `"${v.length > 60 ? v.slice(0, 57) + '…' : v}"`
      : typeof v === 'object' ? JSON.stringify(v).slice(0, 60)
      : String(v)
    parts.push(`${k}=${s}`)
  }
  return parts.join(', ')
})

const status = computed(() => {
  if (props.output === undefined) return { kind: 'pending', label: t('copilot.toolCard.running') }
  if (props.ok === false) return { kind: 'err', label: t('copilot.toolCard.failed') }
  return { kind: 'ok', label: t('copilot.toolCard.success') }
})

const resultSummary = computed(() => {
  if (!props.output) return ''
  if (props.ok === false) {
    return String(props.output.error ?? props.output.message ?? 'error')
  }
  // Heuristic summaries for common shapes
  const o = props.output as Record<string, unknown>
  if (Array.isArray(o.items)) {
    return t('copilot.toolCard.itemsCount', { n: (o.items as unknown[]).length })
  }
  if (Array.isArray(o.results)) {
    return t('copilot.toolCard.resultsCount', { n: (o.results as unknown[]).length })
  }
  if (typeof o.total === 'number') {
    return t('copilot.toolCard.totalCount', { n: o.total })
  }
  if (o.repo_id) return `repo_id=${o.repo_id}`
  if (o.tenant_id !== undefined) {
    return `tenant_id=${o.tenant_id}`
  }
  // Generic: count keys
  return t('copilot.toolCard.fieldsCount', { n: Object.keys(o).length })
})
</script>

<template>
  <div
    class="tool-card"
    :class="`s-${status.kind}`"
    data-test="copilot-tool-card"
  >
    <div
      class="header"
      @click="expanded = !expanded"
    >
      <span class="ic">{{ icon }}</span>
      <code class="name">{{ tool }}</code>
      <span class="args">({{ inputSummary }})</span>
      <el-tag
        size="small"
        :type="status.kind === 'ok' ? 'success'
          : status.kind === 'err' ? 'danger' : 'info'"
      >
        {{ status.label }}
      </el-tag>
      <span class="caret">{{ expanded ? '▾' : '▸' }}</span>
    </div>
    <div
      v-if="output && !expanded"
      class="summary"
    >
      → {{ resultSummary }}
    </div>
    <div
      v-if="expanded"
      class="body"
    >
      <details
        open
        class="block"
      >
        <summary>{{ t('copilot.toolCard.input') }}</summary>
        <pre class="raw">{{ JSON.stringify(input ?? {}, null, 2) }}</pre>
      </details>
      <details
        v-if="output"
        open
        class="block"
      >
        <summary>{{ t('copilot.toolCard.output') }}</summary>
        <pre class="raw">{{ JSON.stringify(output, null, 2) }}</pre>
      </details>
    </div>
  </div>
</template>

<style lang="scss" scoped>
.tool-card {
  border: 1px solid var(--dlw-border);
  border-radius: 6px;
  padding: 6px 8px;
  font-size: 12px;
  background: var(--dlw-surface);
}
.tool-card.s-ok { border-left: 3px solid var(--el-color-success); }
.tool-card.s-err { border-left: 3px solid var(--el-color-danger); }
.tool-card.s-pending { border-left: 3px solid var(--el-color-info); }
.header {
  display: flex; align-items: center; gap: 6px;
  cursor: pointer; user-select: none;
}
.ic { width: 18px; text-align: center; }
.name { font-family: var(--dlw-font-mono, monospace); color: var(--el-color-primary); }
.args { color: var(--dlw-text-soft); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.caret { color: var(--dlw-text-soft); }
.summary { padding-left: 24px; color: var(--dlw-text-soft); font-size: 11px; margin-top: 2px; }
.body { margin-top: 4px; padding-left: 24px; }
.block { margin-top: 4px; }
summary { cursor: pointer; color: var(--dlw-text-soft); font-size: 11px; }
.raw { margin: 4px 0; max-height: 200px; overflow: auto; white-space: pre-wrap;
       background: var(--el-fill-color-light); padding: 4px 6px; border-radius: 4px;
       font-size: 11px; }
</style>
