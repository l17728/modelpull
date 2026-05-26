<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { useSessionStore } from '@/stores/session'
import { visibleNav } from '@/nav/registry'
import CopilotDrawer from '@/components/copilot/CopilotDrawer.vue'
import DocsDrawer from '@/components/help/DocsDrawer.vue'
import HelpDrawer from '@/components/help/HelpDrawer.vue'

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
      <div class="aside-inner">
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
          class="main-menu"
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
        <div class="aside-footer">
          <el-menu :collapse="ui.sidebarCollapsed">
            <el-menu-item
              index="docs"
              data-test="nav-docs"
              @click="ui.toggleDocs()"
            >
              <span>📚 {{ t('nav.docs') }}</span>
            </el-menu-item>
            <el-menu-item
              index="help"
              data-test="nav-help"
              @click="ui.toggleHelp()"
            >
              <span>📖 {{ t('nav.help') }}</span>
            </el-menu-item>
          </el-menu>
        </div>
      </div>
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
        <el-button
          link
          data-test="copilot-toggle"
          @click="ui.toggleCopilot()"
        >
          🤖 {{ t('copilot.title') }}
        </el-button>
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
    <CopilotDrawer />
    <DocsDrawer />
    <HelpDrawer />
  </el-container>
</template>

<style lang="scss" scoped>
.app-shell { min-height: 100vh; }
.el-aside {
  background: var(--dlw-surface);
  border-right: 1px solid var(--dlw-border);
  transition: width 0.2s;
  /* Pin sidebar to viewport so the Help footer is always visible,
     even when main content scrolls past the fold. */
  position: sticky;
  top: 0;
  height: 100vh;
}
.aside-inner {
  display: flex; flex-direction: column;
  height: 100%;
  overflow-y: auto;
}
.main-menu { flex: 1; border-right: 0; }
.aside-footer {
  border-top: 1px solid var(--dlw-border);
  flex-shrink: 0;
  .el-menu { border-right: 0; }
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
