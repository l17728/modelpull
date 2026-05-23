<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'

import { client } from '@/api/client'
import { useSessionStore } from '@/stores/session'

const { t } = useI18n()
const route = useRoute()
const session = useSessionStore()

const userCode = ref('')
const status = ref<'idle' | 'submitting' | 'approved' | 'denied'>('idle')
const errorMsg = ref<string | null>(null)

const isServiceToken = computed(() => session.isServiceToken)
const principal = computed(() => session.principal)

onMounted(() => {
  const q = route.query.user_code
  if (typeof q === 'string') userCode.value = q
})

function mapError(err: unknown): string {
  const e = err as {
    response?: { status?: number; data?: { detail?: { code?: string } } }
  }
  const httpStatus = e?.response?.status
  const code = e?.response?.data?.detail?.code
  if (httpStatus === 404 && code === 'DEVICE_CODE_INVALID') return t('device.errorInvalid')
  if (httpStatus === 403 && code === 'SERVICE_CANNOT_APPROVE') return t('device.errorService')
  return t('device.errorGeneric')
}

async function submit(action: 'approve' | 'deny') {
  if (!userCode.value.trim()) {
    ElMessage.error(t('device.codeRequired'))
    return
  }
  status.value = 'submitting'
  errorMsg.value = null
  try {
    await client.post('/api/v1/auth/device/approve', {
      user_code: userCode.value.trim(),
      action,
    })
    status.value = action === 'approve' ? 'approved' : 'denied'
  } catch (err) {
    errorMsg.value = mapError(err)
    status.value = 'idle'
  }
}
</script>

<template>
  <div class="device-page">
    <el-card class="device-card">
      <template #header>
        <h2>{{ t('device.heading') }}</h2>
      </template>

      <p class="intro">
        {{ t('device.intro') }}
      </p>

      <div
        v-if="principal"
        class="principal"
      >
        <strong>{{ t('device.principalLabel') }}:</strong>
        user #{{ principal.userId }} ({{ principal.role }}, tenant {{ principal.tenantId }})
      </div>

      <el-alert
        v-if="isServiceToken"
        type="warning"
        show-icon
        :closable="false"
        class="service-warning"
      >
        {{ t('device.serviceWarning') }}
      </el-alert>

      <el-alert
        v-if="errorMsg"
        type="error"
        show-icon
        :closable="false"
        class="error-alert"
      >
        {{ errorMsg }}
      </el-alert>

      <template v-if="status === 'idle' || status === 'submitting'">
        <el-form
          label-position="top"
          @submit.prevent="submit('approve')"
        >
          <el-form-item :label="t('device.codeLabel')">
            <el-input
              v-model="userCode"
              :placeholder="t('device.codePlaceholder')"
              autocomplete="off"
            />
          </el-form-item>
          <el-form-item>
            <el-button
              type="primary"
              :disabled="isServiceToken || status === 'submitting'"
              @click="submit('approve')"
            >
              {{ t('device.approve') }}
            </el-button>
            <el-button
              :disabled="status === 'submitting'"
              @click="submit('deny')"
            >
              {{ t('device.deny') }}
            </el-button>
          </el-form-item>
        </el-form>
      </template>

      <el-alert
        v-else-if="status === 'approved'"
        type="success"
        show-icon
        :closable="false"
      >
        {{ t('device.successApproved') }}
      </el-alert>

      <el-alert
        v-else-if="status === 'denied'"
        type="info"
        show-icon
        :closable="false"
      >
        {{ t('device.successDenied') }}
      </el-alert>
    </el-card>
  </div>
</template>

<style lang="scss" scoped>
.device-page {
  display: flex;
  justify-content: center;
  padding-top: 64px;

  .device-card {
    width: 560px;

    h2 { margin: 0; font-size: 18px; }
    .intro { margin: 0 0 16px; color: var(--el-text-color-secondary); }
    .principal { margin-bottom: 16px; font-family: var(--el-font-family); }
    .service-warning, .error-alert { margin-bottom: 16px; }
  }
}
</style>
