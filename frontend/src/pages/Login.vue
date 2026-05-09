<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'

import { useAuthStore } from '@/stores/auth'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

const formRef = ref<FormInstance>()
const form = reactive({ token: '' })
const rules: FormRules = {
  token: [{ required: true, message: () => t('login.tokenRequired'), trigger: 'submit' }],
}

onMounted(() => {
  if (route.query.reason === 'invalid_token') {
    ElMessage.error(t('errors.invalid_token'))
  }
  if (authStore.isAuthenticated) {
    router.replace('/')
  }
})

async function onSubmit() {
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return
  authStore.login(form.token.trim())
  router.push('/')
}
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
          :label="t('login.tokenLabel')"
          prop="token"
        >
          <el-input
            v-model="form.token"
            type="password"
            show-password
            :placeholder="t('login.tokenPlaceholder')"
            autocomplete="off"
          />
        </el-form-item>
        <el-form-item>
          <el-button
            type="primary"
            native-type="submit"
            @click="onSubmit"
          >
            {{ t('login.submit') }}
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
