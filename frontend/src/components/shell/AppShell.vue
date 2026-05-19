<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { useSessionStore } from '@/stores/session'
import { visibleNav } from '@/nav/registry'

const { t } = useI18n()
const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const ui = useUiStore()
const session = useSessionStore()

const items = computed(() => visibleNav(session.role))
const activeRoute = computed(() => String(route.name ?? ''))

function go(name: string) {
  router.push({ name })
}
function logout() {
  auth.logout()
  router.push('/login')
}
function toggleLocale() {
  ui.setLocale(ui.locale === 'zh-CN' ? 'en-US' : 'zh-CN')
}
</script>

<template>
  <el-container class="app-shell">
    <el-aside :width="ui.sidebarCollapsed ? '64px' : '220px'">
      <div class="brand">
        <img
          src="/favicon.svg"
          alt="logo"
          class="logo"
        >
        <span v-show="!ui.sidebarCollapsed">{{ t('app.title') }}</span>
      </div>
      <el-menu
        :default-active="activeRoute"
        :collapse="ui.sidebarCollapsed"
      >
        <el-menu-item
          v-for="i in items"
          :key="i.route"
          :index="i.route"
          @click="go(i.route)"
        >
          <span>{{ t(i.labelKey) }}</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="topbar">
        <el-button
          link
          @click="ui.toggleSidebar()"
        >
          ☰
        </el-button>
        <span class="hint">{{ t('shell.commandHint') }}</span>
        <div class="spacer" />
        <el-tag
          v-if="session.principal"
          size="small"
          type="info"
        >
          {{ t('shell.tenant') }} {{ session.principal.tenantId }} ·
          {{ session.principal.role }}
        </el-tag>
        <el-button
          link
          @click="ui.toggleTheme()"
        >
          {{ ui.theme === 'dark' ? '🌙' : '☀️' }}
        </el-button>
        <el-button
          link
          @click="toggleLocale"
        >
          {{ ui.locale === 'zh-CN' ? 'EN' : '中' }}
        </el-button>
        <el-button
          data-test="logout"
          link
          type="primary"
          @click="logout"
        >
          {{ t('app.logout') }}
        </el-button>
      </el-header>
      <el-main>
        <slot />
      </el-main>
    </el-container>
  </el-container>
</template>

<style lang="scss" scoped>
.app-shell { min-height: 100vh; }
.el-aside {
  background: var(--dlw-surface);
  border-right: 1px solid var(--dlw-border);
  transition: width 0.2s;
}
.brand {
  display: flex; align-items: center; gap: var(--dlw-space-2);
  padding: var(--dlw-space-3); font-weight: 600;
  .logo { width: 28px; height: 28px; }
}
.topbar {
  background: var(--dlw-surface);
  border-bottom: 1px solid var(--dlw-border);
  display: flex; align-items: center; gap: var(--dlw-space-3);
  .hint { color: var(--dlw-text-soft); font-size: 12px; }
  .spacer { flex: 1; }
}
</style>
