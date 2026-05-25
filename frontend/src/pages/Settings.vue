<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { useSessionStore } from '@/stores/session'
import { useUiStore } from '@/stores/ui'
import { useSystemHealth } from '@/composables/useSystemHealth'
import HealthPill from '@/components/infra/HealthPill.vue'
import HelpDrawer from '@/components/help/HelpDrawer.vue'

const { t } = useI18n()
const session = useSessionStore()
const ui = useUiStore()
const { data: health } = useSystemHealth()
</script>

<template>
  <div class="page-container">
    <h2>{{ t('settings.heading') }}</h2>

    <el-card class="card">
      <h3>{{ t('settings.profile') }}</h3>
      <el-descriptions
        :column="1"
        size="small"
        border
      >
        <el-descriptions-item :label="t('settings.principal.user')">
          {{ session.principal?.userId ?? '—' }}
        </el-descriptions-item>
        <el-descriptions-item :label="t('settings.principal.tenant')">
          {{ session.principal?.tenantId ?? '—' }}
        </el-descriptions-item>
        <el-descriptions-item :label="t('settings.principal.role')">
          {{ session.principal?.role ?? '—' }}
        </el-descriptions-item>
        <el-descriptions-item :label="t('settings.principal.projects')">
          {{ (session.principal?.projectIds ?? []).join(', ') || '—' }}
        </el-descriptions-item>
        <el-descriptions-item :label="t('settings.principal.serviceToken')">
          {{ session.isServiceToken ? 'yes' : 'no' }}
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-card class="card">
      <h3>{{ t('settings.preferences') }}</h3>
      <div class="row">
        <span class="lbl">{{ t('settings.theme') }}</span>
        <el-switch
          :model-value="ui.theme === 'dark'"
          :active-text="t('settings.themeDark')"
          :inactive-text="t('settings.themeLight')"
          @change="ui.toggleTheme()"
        />
      </div>
      <div class="row">
        <span class="lbl">{{ t('settings.localeLabel') }}</span>
        <el-radio-group
          :model-value="ui.locale"
          @update:model-value="(v: string | number | boolean | undefined) =>
            ui.setLocale(String(v) as 'en-US' | 'zh-CN')"
        >
          <el-radio value="en-US">
            English
          </el-radio>
          <el-radio value="zh-CN">
            中文
          </el-radio>
        </el-radio-group>
      </div>
    </el-card>

    <el-card class="card">
      <h3>{{ t('settings.system') }}</h3>
      <div class="row">
        <span class="lbl">{{ t('settings.controllerState') }}</span>
        <HealthPill :state="health?.controller_state ?? 'unknown'" />
      </div>
    </el-card>

    <el-card class="card">
      <h3>{{ t('settings.help') }}</h3>
      <div class="row help-row">
        <div class="help-desc">
          {{ t('settings.helpDesc') }}
        </div>
        <el-button
          type="primary"
          data-test="help-open-btn"
          @click="ui.toggleHelp()"
        >
          📖 {{ t('settings.helpBtn') }}
        </el-button>
      </div>
    </el-card>
  </div>

  <HelpDrawer />
</template>

<style scoped lang="scss">
.card { margin-top: 16px; }
.row {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-top: 8px;
  .lbl { min-width: 140px; color: var(--el-text-color-regular); }
}
.help-row {
  align-items: flex-start;
  .help-desc { flex: 1; color: var(--el-text-color-secondary); font-size: 13px; padding-top: 6px; }
}
</style>
