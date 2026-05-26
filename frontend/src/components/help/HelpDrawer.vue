<script setup lang="ts">
import { ref, watch } from 'vue'
import { marked } from 'marked'
import { useI18n } from 'vue-i18n'
import { useUiStore } from '@/stores/ui'
import { client } from '@/api/client'

const { t } = useI18n()
const ui = useUiStore()

const html = ref('')
const loading = ref(false)
const loadError = ref(false)

async function loadManual() {
  // Always re-fetch on open: MANUAL.md is small (~17KB), the controller
  // re-reads it from disk every request, and in dev the file changes
  // often. The old "cache forever in this session" guard meant restarting
  // the controller with updated docs left users staring at stale content
  // until they hard-refreshed the browser. Network cache (ETag / 304)
  // can re-add a true cache later if the file ever grows huge.
  loading.value = true
  loadError.value = false
  try {
    const res = await client.get<{ content: string }>('/api/v1/help/manual')
    html.value = marked.parse(res.data.content) as string
  } catch {
    loadError.value = true
  } finally {
    loading.value = false
  }
}

watch(() => ui.helpOpen, (open) => {
  if (open) void loadManual()
})

function retry() {
  html.value = ''
  void loadManual()
}
</script>

<template>
  <el-drawer
    v-model="ui.helpOpen"
    :title="t('help.title')"
    direction="rtl"
    size="780px"
    data-test="help-drawer"
    class="help-drawer"
  >
    <div
      v-if="loading"
      class="help-loading"
    >
      {{ t('help.loading') }}
    </div>

    <el-result
      v-else-if="loadError"
      icon="error"
      :title="t('help.error')"
    >
      <template #extra>
        <el-button
          type="primary"
          @click="retry"
        >
          重试
        </el-button>
      </template>
    </el-result>

    <!-- eslint-disable vue/no-v-html -->
    <div
      v-else
      class="help-body markdown-body"
      v-html="html"
    />
    <!-- eslint-enable vue/no-v-html -->
  </el-drawer>
</template>

<style lang="scss">
.help-drawer .el-drawer__body {
  overflow-y: auto;
  padding: 0 24px 24px;
}

.help-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 48px 0;
  color: var(--el-text-color-secondary);
}

.markdown-body {
  font-size: 14px;
  line-height: 1.75;
  color: var(--el-text-color-primary);

  h1 {
    font-size: 1.6em;
    margin: 0 0 16px;
    border-bottom: 1px solid var(--el-border-color);
    padding-bottom: 8px;
  }
  h2 {
    font-size: 1.3em;
    margin: 28px 0 10px;
    border-bottom: 1px solid var(--el-border-color-lighter);
    padding-bottom: 6px;
  }
  h3 { font-size: 1.1em; margin: 20px 0 8px; }
  h4 { font-size: 1em; margin: 16px 0 6px; }

  p { margin: 0 0 12px; }
  ul, ol { padding-left: 24px; margin: 0 0 12px; }
  li { margin-bottom: 4px; }

  table {
    border-collapse: collapse;
    width: 100%;
    margin: 12px 0;
    font-size: 13px;

    th,
    td {
      border: 1px solid var(--el-border-color);
      padding: 6px 12px;
      text-align: left;
    }
    th { background: var(--el-fill-color-light); font-weight: 600; }
    tr:nth-child(even) td { background: var(--el-fill-color-extra-light); }
  }

  code {
    font-family: 'Courier New', monospace;
    font-size: 0.88em;
    background: var(--el-fill-color);
    border: 1px solid var(--el-border-color-lighter);
    border-radius: 3px;
    padding: 1px 5px;
  }
  pre {
    background: var(--el-fill-color-darker, #1e1e1e);
    border: 1px solid var(--el-border-color);
    border-radius: 6px;
    padding: 14px 16px;
    overflow-x: auto;
    margin: 12px 0;

    code {
      background: none;
      border: none;
      padding: 0;
      font-size: 0.86em;
      color: var(--el-text-color-primary);
    }
  }

  blockquote {
    margin: 12px 0;
    padding: 8px 16px;
    border-left: 4px solid var(--el-color-primary-light-5);
    background: var(--el-color-primary-light-9);
    color: var(--el-text-color-secondary);
    border-radius: 0 4px 4px 0;

    p { margin: 0; }
  }

  hr { border: none; border-top: 1px solid var(--el-border-color); margin: 24px 0; }

  a { color: var(--el-color-primary); text-decoration: none; }
  a:hover { text-decoration: underline; }

  strong { font-weight: 600; }
  em { font-style: italic; }
}
</style>
