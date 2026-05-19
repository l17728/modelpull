<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { client } from '@/api/client'
import { useSessionStore } from '@/stores/session'
import { validateCreate, mapCreateError } from '@/tasks/createValidation'
import type { TaskCreateBody, TaskRead } from '@/api/types'

const { t } = useI18n()
const router = useRouter()
const session = useSessionStore()
const submitting = ref(false)

const form = reactive<TaskCreateBody>({
  repo_id: '', revision: '', storage_id: 1, priority: 1,
  source_strategy: 'auto_balance', trust_non_hf_sha256: false,
  upgrade_from_revision: '',
})

async function submit() {
  const errs = validateCreate(form)
  if (errs.length) {
    ElMessage.error(t(`create.${errs[0]}`))
    return
  }
  submitting.value = true
  try {
    const body: TaskCreateBody = { ...form }
    if (!body.upgrade_from_revision) delete body.upgrade_from_revision
    const r = await client.post<TaskRead>('/api/v1/tasks', body)
    ElMessage.success(t('create.success'))
    router.push({ name: 'taskDetail', params: { id: r.data.id } })
  } catch (e) {
    const status = (e as { response?: { status?: number } })?.response?.status
    ElMessage.error(t(mapCreateError(status)))
  } finally {
    submitting.value = false
  }
}

defineExpose({ submit, form })
</script>

<template>
  <div class="page-container">
    <h2>{{ t('create.heading') }}</h2>

    <el-alert
      v-if="session.isServiceToken"
      type="warning"
      :closable="false"
      :title="t('create.serviceTokenWarn')"
      style="margin-bottom: 16px"
    />

    <el-form
      label-position="top"
      style="max-width: 560px"
    >
      <el-form-item :label="t('create.repo')">
        <el-input
          v-model="form.repo_id"
          placeholder="org/model"
        />
      </el-form-item>
      <el-form-item :label="t('create.revision')">
        <el-input
          v-model="form.revision"
          placeholder="40-hex sha"
        />
      </el-form-item>
      <el-form-item :label="t('create.storageId')">
        <el-input-number
          v-model="form.storage_id"
          :min="1"
        />
      </el-form-item>
      <el-form-item :label="t('create.priority')">
        <el-input-number
          v-model="form.priority"
          :min="0"
          :max="10"
        />
      </el-form-item>
      <el-form-item :label="t('create.strategy')">
        <el-select v-model="form.source_strategy">
          <el-option
            v-for="s in ['auto_balance', 'fastest_only', 'pin_huggingface']"
            :key="s"
            :label="s"
            :value="s"
          />
        </el-select>
      </el-form-item>
      <el-form-item :label="t('create.upgradeFrom')">
        <el-input
          v-model="form.upgrade_from_revision"
          placeholder="(optional) 40-hex sha"
        />
      </el-form-item>
      <el-form-item>
        <el-switch v-model="form.trust_non_hf_sha256" />
        <span style="margin-left: 8px">{{ t('create.trustNonHf') }}</span>
      </el-form-item>
      <el-form-item>
        <el-button
          type="primary"
          :loading="submitting"
          :disabled="session.isServiceToken"
          @click="submit"
        >
          {{ t('create.submit') }}
        </el-button>
      </el-form-item>
    </el-form>
  </div>
</template>
