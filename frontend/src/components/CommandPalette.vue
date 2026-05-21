<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { useSessionStore } from '@/stores/session'
import { useUiStore } from '@/stores/ui'
import { buildCommands, type Command } from '@/components/palette'

const { t } = useI18n()
const router = useRouter()
const session = useSessionStore()
const ui = useUiStore()
const open = ref(false)
const q = ref('')

const all = computed(() => buildCommands(session.role, t))
const filtered = computed(() =>
  all.value.filter((c) => c.label.toLowerCase().includes(q.value.toLowerCase())))

function onKey(e: KeyboardEvent) {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    open.value = !open.value
    q.value = ''
  } else if (e.key === 'Escape') {
    open.value = false
  }
}
onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => window.removeEventListener('keydown', onKey))

async function run(c: Command) {
  open.value = false
  if (c.kind === 'nav' && c.routeName) {
    router.push({ name: c.routeName })
  } else if (c.action === 'createTask') {
    router.push({ name: 'taskCreate' })
  } else if (c.action === 'openTaskById') {
    const r = await ElMessageBox.prompt(t('palette.openTaskPrompt'), '', {
      inputPattern: /\S+/,
    }).catch(() => null)
    if (r?.value) router.push({ name: 'taskDetail', params: { id: r.value.trim() } })
  } else if (c.action === 'openCopilot') {
    ui.toggleCopilot()
  }
}
</script>

<template>
  <el-dialog
    v-model="open"
    :show-close="false"
    top="12vh"
    width="520px"
  >
    <el-input
      v-model="q"
      :placeholder="t('palette.placeholder')"
      autofocus
    />
    <el-scrollbar max-height="320px">
      <div
        v-for="c in filtered"
        :key="c.id"
        class="cmd"
        @click="run(c)"
      >
        {{ c.label }}
        <small>{{ c.kind === 'nav' ? t('palette.navGroup') : t('palette.actionGroup') }}</small>
      </div>
    </el-scrollbar>
  </el-dialog>
</template>

<style lang="scss" scoped>
.cmd {
  display: flex; justify-content: space-between; align-items: center;
  padding: var(--dlw-space-2) var(--dlw-space-3); cursor: pointer;
  border-radius: var(--dlw-radius);
  small { color: var(--dlw-text-soft); }
  &:hover { background: var(--dlw-bg); }
}
</style>
