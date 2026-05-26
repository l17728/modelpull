<script setup lang="ts">
import { computed } from 'vue'
import { marked } from 'marked'
import { useI18n } from 'vue-i18n'

interface ToolCardLike {
  tool: string
  ok?: boolean
}

interface Props {
  role: 'user' | 'assistant'
  text: string
  toolCards: ToolCardLike[]
}
const props = defineProps<Props>()
const { t } = useI18n()

// Render assistant text as Markdown. User text stays plain (no MD).
// Defense-in-depth: strip <script>/<iframe> tags before marked parses,
// since marked v14 preserves raw HTML by default. The model rarely emits
// HTML; this is a belt-and-braces guard, not the primary defense.
const html = computed(() => {
  if (props.role !== 'assistant' || !props.text) return ''
  const cleaned = props.text.replace(
    /<\/?(?:script|iframe|object|embed|style)\b[^>]*>/gi, '')
  return marked.parse(cleaned, { async: false, breaks: true }) as string
})

// Group tools into source categories so we can show one badge per distinct
// data source instead of one badge per tool call.
const sources = computed(() => {
  if (props.role !== 'assistant') return []
  const cards = props.toolCards.filter((c) => c.ok !== false)
  if (cards.length === 0) {
    return [{ key: 'model', icon: '💭', label: t('copilot.source.model') }]
  }
  const seen = new Set<string>()
  const out: Array<{ key: string; icon: string; label: string }> = []
  for (const c of cards) {
    let key = 'internal', icon = '📊'
    if (c.tool === 'web_search') { key = 'web'; icon = '🌐' }
    else if (c.tool === 'search_modelscope_models') { key = 'modelscope'; icon = '🔍' }
    else if (c.tool === 'fetch_user_content') { key = 'fetch'; icon = '🌐' }
    else if (c.tool === 'hf_api_metadata' || c.tool === 'hf_model_card') {
      key = 'hf'; icon = '🤗'
    }
    if (seen.has(key)) continue
    seen.add(key)
    out.push({ key, icon, label: t(`copilot.source.${key}`) })
  }
  return out
})

const hasFailedTool = computed(
  () => props.toolCards.some((c) => c.ok === false))
</script>

<template>
  <article
    class="bubble"
    :class="role"
    :data-test="`copilot-bubble-${role}`"
  >
    <!-- assistant: rendered markdown; user: plain text -->
    <!-- eslint-disable vue/no-v-html -->
    <div
      v-if="role === 'assistant'"
      class="md"
      v-html="html"
    />
    <!-- eslint-enable vue/no-v-html -->
    <div
      v-else
      class="plain"
    >
      {{ text }}
    </div>

    <footer
      v-if="role === 'assistant' && text"
      class="src"
    >
      <span
        v-for="s in sources"
        :key="s.key"
        class="src-badge"
        :class="`src-${s.key}`"
      >
        {{ s.icon }} {{ s.label }}
      </span>
      <span
        v-if="hasFailedTool"
        class="src-badge src-error"
      >
        ⚠ {{ t('copilot.source.toolFailed') }}
      </span>
    </footer>
  </article>
</template>

<style lang="scss" scoped>
.bubble {
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--dlw-surface);
  max-width: 92%;
  border: 1px solid var(--dlw-border);
}
.bubble.user {
  background: var(--el-color-primary-light-8);
  align-self: flex-end;
  max-width: 88%;
}
.plain { white-space: pre-wrap; }

/* Markdown body — keep margins compact inside the chat bubble */
.md :deep(p)        { margin: 0 0 6px; line-height: 1.5; }
.md :deep(p:last-child) { margin-bottom: 0; }
.md :deep(ul),
.md :deep(ol)       { margin: 4px 0 6px 20px; padding: 0; }
.md :deep(li)       { margin: 2px 0; }
.md :deep(code)     {
  background: var(--el-fill-color-light); padding: 1px 4px;
  border-radius: 3px; font-size: 12px;
  font-family: var(--dlw-font-mono, ui-monospace, monospace);
}
.md :deep(pre) {
  background: var(--el-fill-color-light); padding: 8px 10px;
  border-radius: 4px; overflow-x: auto;
  font-size: 12px; margin: 6px 0;
}
.md :deep(pre code) { background: transparent; padding: 0; }
.md :deep(blockquote) {
  border-left: 3px solid var(--dlw-border);
  padding-left: 10px; margin: 6px 0; color: var(--dlw-text-soft);
}
.md :deep(h1), .md :deep(h2), .md :deep(h3) {
  font-size: 14px; margin: 8px 0 4px; font-weight: 600;
}
.md :deep(table) { border-collapse: collapse; margin: 6px 0; font-size: 12px; }
.md :deep(th), .md :deep(td) {
  border: 1px solid var(--dlw-border); padding: 3px 6px;
}
.md :deep(a) { color: var(--el-color-primary); text-decoration: underline; }

/* Source attribution footer */
.src {
  margin-top: 8px; padding-top: 6px;
  border-top: 1px dashed var(--dlw-border);
  display: flex; flex-wrap: wrap; gap: 6px;
  font-size: 11px;
}
.src-badge {
  padding: 1px 6px; border-radius: 10px;
  background: var(--el-fill-color-light);
}
.src-model     { color: var(--el-color-warning); }
.src-web       { color: var(--el-color-primary); }
.src-modelscope{ color: var(--el-color-primary); }
.src-fetch     { color: var(--el-color-primary); }
.src-hf        { color: var(--el-color-success); }
.src-internal  { color: var(--el-color-success); }
.src-error     { color: var(--el-color-danger); }
</style>
