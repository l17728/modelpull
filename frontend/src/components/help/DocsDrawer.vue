<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { marked } from 'marked'
import { useI18n } from 'vue-i18n'
import { useUiStore } from '@/stores/ui'
import { client } from '@/api/client'

interface DocItem {
  slug: string
  title_en: string
  title_zh: string
}

const { t, locale } = useI18n()
const ui = useUiStore()

const items = ref<DocItem[]>([])
const indexLoading = ref(false)
const indexError = ref(false)
const html = ref('')
const docLoading = ref(false)
const docError = ref(false)
const activeSlug = ref<string | null>(null)

async function loadIndex(): Promise<void> {
  if (items.value.length) return
  indexLoading.value = true
  indexError.value = false
  try {
    const res = await client.get<{ items: DocItem[] }>('/api/v1/help/docs')
    items.value = res.data.items
    const first = items.value[0]
    if (!activeSlug.value && first) {
      await loadDoc(first.slug)
    }
  } catch {
    indexError.value = true
  } finally {
    indexLoading.value = false
  }
}

async function loadDoc(slug: string): Promise<void> {
  activeSlug.value = slug
  docLoading.value = true
  docError.value = false
  try {
    const res = await client.get<{ content: string }>(`/api/v1/help/docs/${slug}`)
    html.value = marked.parse(res.data.content) as string
  } catch {
    docError.value = true
  } finally {
    docLoading.value = false
  }
}

const titleFor = (it: DocItem) => locale.value === 'zh-CN' ? it.title_zh : it.title_en

watch(() => ui.docsOpen, (open) => {
  if (open) void loadIndex()
})
</script>

<template>
  <el-drawer
    v-model="ui.docsOpen"
    :title="t('docs.title')"
    direction="rtl"
    size="720px"
    data-test="docs-drawer"
  >
    <div class="docs-layout">
      <aside class="docs-side">
        <div
          v-if="indexLoading"
          class="muted"
        >
          {{ t('docs.loadingIndex') }}
        </div>
        <el-alert
          v-else-if="indexError"
          :title="t('docs.indexError')"
          type="error"
          :closable="false"
          show-icon
        />
        <ul
          v-else
          class="doc-list"
        >
          <li
            v-for="it in items"
            :key="it.slug"
            :class="{ active: it.slug === activeSlug }"
            data-test="docs-side-item"
            @click="loadDoc(it.slug)"
          >
            {{ titleFor(it) }}
          </li>
        </ul>
      </aside>
      <main class="docs-main">
        <div
          v-if="docLoading"
          class="muted"
        >
          {{ t('docs.loadingDoc') }}
        </div>
        <el-alert
          v-else-if="docError"
          :title="t('docs.docError')"
          type="error"
          :closable="false"
          show-icon
        />
        <!-- eslint-disable vue/no-v-html -->
        <div
          v-else
          class="markdown-body"
          v-html="html"
        />
        <!-- eslint-enable vue/no-v-html -->
      </main>
    </div>
  </el-drawer>
</template>

<style lang="scss" scoped>
.docs-layout {
  display: grid;
  grid-template-columns: 240px 1fr;
  gap: 12px;
  height: 100%;
}
.docs-side {
  border-right: 1px solid var(--dlw-border);
  padding-right: 8px;
  overflow-y: auto;
}
.doc-list {
  list-style: none; margin: 0; padding: 0;
  li {
    padding: 8px 10px;
    border-radius: 4px;
    cursor: pointer;
    color: var(--dlw-text);
    font-size: 13px;
    &:hover { background: var(--el-fill-color-light); }
    &.active {
      background: var(--el-color-primary-light-9);
      color: var(--el-color-primary);
      font-weight: 600;
    }
  }
}
.docs-main {
  overflow-y: auto;
  padding-right: 8px;
}
.muted { color: var(--dlw-text-soft); padding: 16px; }
.markdown-body :deep(h1) { font-size: 20px; margin-top: 0; }
.markdown-body :deep(h2) { font-size: 16px; margin-top: 16px; }
.markdown-body :deep(h3) { font-size: 14px; margin-top: 12px; }
.markdown-body :deep(code) {
  background: var(--el-fill-color-light); padding: 1px 4px;
  border-radius: 3px; font-size: 12px;
}
.markdown-body :deep(pre) {
  background: var(--el-fill-color-light); padding: 8px;
  border-radius: 4px; overflow-x: auto; font-size: 12px;
}
.markdown-body :deep(table) {
  border-collapse: collapse; margin: 8px 0; font-size: 13px;
}
.markdown-body :deep(th),
.markdown-body :deep(td) {
  border: 1px solid var(--dlw-border); padding: 4px 8px;
}
</style>
