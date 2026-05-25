<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import axios from 'axios'

import { useAuthStore } from '@/stores/auth'
import { oidcLoginUrl } from '@/pages/oidc'

function safeRedirect(raw: unknown): string {
  if (typeof raw !== 'string') return '/'
  if (!raw.startsWith('/') || raw.startsWith('//')) return '/'
  if (raw === '/login' || raw.startsWith('/login?') || raw.startsWith('/login/')) return '/'
  return raw
}

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

const formRef = ref<FormInstance>()
const loading = ref(false)
const form = reactive({ username: '', password: '' })
const rules: FormRules = {
  username: [{ required: true, message: () => t('login.usernameRequired'), trigger: 'submit' }],
  password: [{ required: true, message: () => t('login.passwordRequired'), trigger: 'submit' }],
}

onMounted(() => {
  if (route.query.reason === 'invalid_token') {
    ElMessage.error(t('errors.invalid_token'))
  }
  if (authStore.isAuthenticated) {
    router.replace(safeRedirect(route.query.redirect))
  }
})

async function onSubmit() {
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    const base = import.meta.env.VITE_API_BASE ?? ''
    const res = await axios.post(`${base}/api/v1/auth/local/login`, {
      username: form.username.trim(),
      password: form.password,
    })
    authStore.login(res.data.access_token)
    if (res.data.must_change_password) {
      ElMessage.warning(t('login.mustChangePassword'))
      router.replace('/settings?mustChangePassword=true')
    } else {
      router.replace(safeRedirect(route.query.redirect))
    }
  } catch (err: unknown) {
    const status = (err as { response?: { status?: number } })?.response?.status
    if (status === 401) {
      ElMessage.error(t('login.invalidCredentials'))
    } else {
      ElMessage.error(t('errors.network'))
    }
  } finally {
    loading.value = false
  }
}

function loginOidc() {
  window.location.assign(oidcLoginUrl(import.meta.env.VITE_API_BASE))
}

defineExpose({ form, onSubmit })
</script>

<template>
  <div class="login-page">
    <el-card class="login-card">
      <template #header>
        <h2>{{ t('login.heading') }}</h2>
      </template>
      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        @submit.prevent="onSubmit"
      >
        <el-form-item
          :label="t('login.usernameLabel')"
          prop="username"
        >
          <el-input
            v-model="form.username"
            :placeholder="t('login.usernamePlaceholder')"
            autocomplete="username"
          />
        </el-form-item>
        <el-form-item
          :label="t('login.passwordLabel')"
          prop="password"
        >
          <el-input
            v-model="form.password"
            type="password"
            show-password
            :placeholder="t('login.passwordPlaceholder')"
            autocomplete="current-password"
          />
        </el-form-item>
        <el-form-item>
          <el-button
            type="primary"
            native-type="submit"
            :loading="loading"
            @click="onSubmit"
          >
            {{ t('login.submit') }}
          </el-button>
        </el-form-item>
        <el-form-item>
          <el-button
            link
            @click="loginOidc"
          >
            {{ t('login.oidc') }}
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<style lang="scss" scoped>
.login-page {
  display: flex;
  justify-content: center;
  padding-top: 96px;

  .login-card {
    width: 420px;

    h2 {
      margin: 0;
      font-size: 18px;
    }
  }
}
</style>
