<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const { t } = useI18n()
const router = useRouter()
const authStore = useAuthStore()

function logout() {
  authStore.logout()
  router.push('/login')
}
</script>

<template>
  <el-container class="app-layout">
    <el-header class="app-header">
      <div class="brand">
        <img src="/favicon.svg" alt="logo" class="logo" />
        <span class="title">{{ t('app.title') }}</span>
      </div>
      <el-button
        v-if="authStore.isAuthenticated"
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
</template>

<style lang="scss" scoped>
.app-layout {
  min-height: 100vh;
}
.app-header {
  background: white;
  border-bottom: 1px solid #ebeef5;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;

  .brand {
    display: flex;
    align-items: center;
    gap: 8px;

    .logo {
      width: 28px;
      height: 28px;
    }
    .title {
      font-size: 16px;
      font-weight: 600;
    }
  }
}
</style>
