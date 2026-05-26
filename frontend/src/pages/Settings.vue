<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { useSessionStore } from '@/stores/session'
import { useUiStore } from '@/stores/ui'
import { useSystemHealth } from '@/composables/useSystemHealth'
import { client } from '@/api/client'
import HealthPill from '@/components/infra/HealthPill.vue'
import { useQuota } from '@/composables/useQuota'
const { t } = useI18n()
const route = useRoute()
const session = useSessionStore()
const ui = useUiStore()
const { data: health } = useSystemHealth()
const quotaQuery = useQuota()
const quota = quotaQuery.data

// v2.1 SP1 — SLA tier. system_admin can change; others read-only.
const slaUpdating = ref(false)
const slaIsAdmin = () => session.principal?.role === 'system_admin'
async function changeSlaTier(newTier: string): Promise<void> {
  if (!session.principal?.tenantId) return
  slaUpdating.value = true
  try {
    await client.put(
      `/api/v1/tenants/${session.principal.tenantId}/sla`,
      { sla_tier: newTier })
    ElMessage.success(t('settings.slaUpdated'))
    await quotaQuery.refetch()
  } catch (err: unknown) {
    const status = (err as { response?: { status?: number } })?.response?.status
    if (status === 403) ElMessage.error(t('settings.slaAdminOnly'))
    else if (status === 422) ElMessage.error(t('settings.slaInvalid'))
    else ElMessage.error(t('errors.network'))
  } finally {
    slaUpdating.value = false
  }
}

// ── Change password ──────────────────────────────────────────────────────────
const pwFormRef = ref<FormInstance>()
const pwLoading = ref(false)
const pwForm = reactive({ old: '', new: '', confirm: '' })
const pwRules: FormRules = {
  old: [{ required: true, message: () => t('settings.oldPassword'), trigger: 'submit' }],
  new: [
    { required: true, message: () => t('settings.newPassword'), trigger: 'submit' },
    { min: 8, message: () => t('settings.passwordTooShort'), trigger: 'submit' },
  ],
  confirm: [
    { required: true, message: () => t('settings.confirmPassword'), trigger: 'submit' },
    {
      validator: (_rule, value, cb) => {
        if (value !== pwForm.new) cb(new Error(t('settings.passwordMismatch')))
        else cb()
      },
      trigger: 'submit',
    },
  ],
}

async function changePassword() {
  if (!pwFormRef.value) return
  const valid = await pwFormRef.value.validate().catch(() => false)
  if (!valid) return
  pwLoading.value = true
  try {
    await client.post('/api/v1/auth/local/password', {
      old_password: pwForm.old,
      new_password: pwForm.new,
    })
    ElMessage.success(t('settings.passwordChanged'))
    pwForm.old = ''
    pwForm.new = ''
    pwForm.confirm = ''
  } catch (err: unknown) {
    const status = (err as { response?: { status?: number } })?.response?.status
    if (status === 401) ElMessage.error(t('settings.wrongOldPassword'))
    else if (status === 404) ElMessage.warning(t('settings.localAuthOnly'))
    else ElMessage.error(t('errors.network'))
  } finally {
    pwLoading.value = false
  }
}

// ── User management (admin only) ─────────────────────────────────────────────
interface LocalUser {
  user_id: number
  username: string
  tenant_id: number
  role: string
  must_change_password: boolean
  created_at: string | null
}

const users = ref<LocalUser[]>([])
const usersLoading = ref(false)

async function loadUsers() {
  usersLoading.value = true
  try {
    const res = await client.get<LocalUser[]>('/api/v1/auth/local/users')
    users.value = res.data
  } catch {
    /* not admin or not available */
  } finally {
    usersLoading.value = false
  }
}

// Create user dialog
const createDialogVisible = ref(false)
const createFormRef = ref<FormInstance>()
const createLoading = ref(false)
const createForm = reactive({
  username: '',
  password: '',
  tenant_id: 1,
  role: 'tenant_operator',
})
const createRules: FormRules = {
  username: [{ required: true, min: 1, max: 64, trigger: 'submit' }],
  password: [{ required: true, min: 8, message: () => t('settings.passwordTooShort'), trigger: 'submit' }],
  tenant_id: [{ required: true, trigger: 'submit' }],
  role: [{ required: true, trigger: 'submit' }],
}

function openCreateDialog() {
  createForm.username = ''
  createForm.password = ''
  createForm.tenant_id = 1
  createForm.role = 'tenant_operator'
  createDialogVisible.value = true
}

async function submitCreateUser() {
  if (!createFormRef.value) return
  const valid = await createFormRef.value.validate().catch(() => false)
  if (!valid) return
  createLoading.value = true
  try {
    await client.post('/api/v1/auth/local/users', {
      username: createForm.username.trim(),
      password: createForm.password,
      tenant_id: createForm.tenant_id,
      role: createForm.role,
    })
    ElMessage.success(t('settings.userCreated'))
    createDialogVisible.value = false
    await loadUsers()
  } catch (err: unknown) {
    const status = (err as { response?: { status?: number } })?.response?.status
    if (status === 409) ElMessage.error(`${t('settings.usernameLabel')}: already exists`)
    else ElMessage.error(t('errors.network'))
  } finally {
    createLoading.value = false
  }
}

// Reset password dialog
const resetDialogVisible = ref(false)
const resetTarget = ref<LocalUser | null>(null)
const resetFormRef = ref<FormInstance>()
const resetLoading = ref(false)
const resetForm = reactive({ new_password: '' })
const resetRules: FormRules = {
  new_password: [
    { required: true, min: 8, message: () => t('settings.passwordTooShort'), trigger: 'submit' },
  ],
}

function openResetDialog(user: LocalUser) {
  resetTarget.value = user
  resetForm.new_password = ''
  resetDialogVisible.value = true
}

async function submitResetPassword() {
  if (!resetFormRef.value || !resetTarget.value) return
  const valid = await resetFormRef.value.validate().catch(() => false)
  if (!valid) return
  resetLoading.value = true
  try {
    await client.post(`/api/v1/auth/local/users/${resetTarget.value.user_id}/reset`, {
      new_password: resetForm.new_password,
    })
    ElMessage.success(t('settings.passwordReset'))
    resetDialogVisible.value = false
    await loadUsers()
  } catch {
    ElMessage.error(t('errors.network'))
  } finally {
    resetLoading.value = false
  }
}

const isAdmin = () => session.principal?.role === 'system_admin'

onMounted(() => {
  if (route.query.mustChangePassword) {
    ElMessage.warning(t('login.mustChangePassword'))
  }
  if (isAdmin()) void loadUsers()
})
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
      <div class="row">
        <span class="lbl">{{ t('settings.slaTier') }}</span>
        <template v-if="slaIsAdmin()">
          <el-select
            :model-value="quota?.sla_tier ?? 'standard'"
            size="small"
            :loading="slaUpdating"
            data-test="sla-tier-select"
            @update:model-value="(v: string | number | boolean | undefined) =>
              changeSlaTier(String(v))"
          >
            <el-option
              value="critical"
              :label="t('settings.slaCritical')"
            />
            <el-option
              value="standard"
              :label="t('settings.slaStandard')"
            />
            <el-option
              value="bulk"
              :label="t('settings.slaBulk')"
            />
          </el-select>
          <span class="sub">{{ t('settings.slaAdminHint') }}</span>
        </template>
        <el-tag
          v-else
          size="small"
          :type="quota?.sla_tier === 'critical' ? 'danger'
            : quota?.sla_tier === 'bulk' ? 'info' : 'success'"
          data-test="sla-tier-readonly"
        >
          {{ quota?.sla_tier ?? 'standard' }}
        </el-tag>
      </div>
    </el-card>

    <el-card class="card">
      <h3>{{ t('settings.changePassword') }} <span class="sub">{{ t('settings.localAuthOnly') }}</span></h3>
      <el-form
        ref="pwFormRef"
        :model="pwForm"
        :rules="pwRules"
        label-position="top"
        class="pw-form"
      >
        <el-form-item
          :label="t('settings.oldPassword')"
          prop="old"
        >
          <el-input
            v-model="pwForm.old"
            type="password"
            show-password
            autocomplete="current-password"
          />
        </el-form-item>
        <el-form-item
          :label="t('settings.newPassword')"
          prop="new"
        >
          <el-input
            v-model="pwForm.new"
            type="password"
            show-password
            autocomplete="new-password"
          />
        </el-form-item>
        <el-form-item
          :label="t('settings.confirmPassword')"
          prop="confirm"
        >
          <el-input
            v-model="pwForm.confirm"
            type="password"
            show-password
            autocomplete="new-password"
          />
        </el-form-item>
        <el-form-item>
          <el-button
            type="primary"
            :loading="pwLoading"
            @click="changePassword"
          >
            {{ t('settings.changePasswordBtn') }}
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card
      v-if="isAdmin()"
      class="card"
    >
      <div class="card-header">
        <h3>{{ t('settings.userManagement') }}</h3>
        <el-button
          type="primary"
          size="small"
          @click="openCreateDialog"
        >
          {{ t('settings.createUser') }}
        </el-button>
      </div>
      <el-table
        v-loading="usersLoading"
        :data="users"
        size="small"
        class="user-table"
      >
        <el-table-column
          prop="user_id"
          label="ID"
          width="80"
        />
        <el-table-column
          prop="username"
          :label="t('settings.usernameLabel')"
        />
        <el-table-column
          prop="tenant_id"
          :label="t('settings.tenantIdLabel')"
          width="100"
        />
        <el-table-column
          prop="role"
          :label="t('settings.roleLabel')"
        />
        <el-table-column
          prop="must_change_password"
          label="Must change"
          width="110"
        >
          <template #default="{ row }">
            {{ row.must_change_password ? '✓' : '' }}
          </template>
        </el-table-column>
        <el-table-column
          label=""
          width="120"
        >
          <template #default="{ row }">
            <el-button
              link
              type="warning"
              size="small"
              @click="openResetDialog(row)"
            >
              {{ t('settings.resetPassword') }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>

  <!-- Create user dialog -->
  <el-dialog
    v-model="createDialogVisible"
    :title="t('settings.createUser')"
    width="420px"
  >
    <el-form
      ref="createFormRef"
      :model="createForm"
      :rules="createRules"
      label-position="top"
    >
      <el-form-item
        :label="t('settings.usernameLabel')"
        prop="username"
      >
        <el-input
          v-model="createForm.username"
          autocomplete="off"
        />
      </el-form-item>
      <el-form-item
        :label="t('settings.initialPasswordLabel')"
        prop="password"
      >
        <el-input
          v-model="createForm.password"
          type="password"
          show-password
          autocomplete="new-password"
        />
      </el-form-item>
      <el-form-item
        :label="t('settings.tenantIdLabel')"
        prop="tenant_id"
      >
        <el-input-number
          v-model="createForm.tenant_id"
          :min="1"
        />
      </el-form-item>
      <el-form-item
        :label="t('settings.roleLabel')"
        prop="role"
      >
        <el-select v-model="createForm.role">
          <el-option
            value="system_admin"
            label="system_admin"
          />
          <el-option
            value="tenant_admin"
            label="tenant_admin"
          />
          <el-option
            value="tenant_operator"
            label="tenant_operator"
          />
          <el-option
            value="tenant_viewer"
            label="tenant_viewer"
          />
        </el-select>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="createDialogVisible = false">
        Cancel
      </el-button>
      <el-button
        type="primary"
        :loading="createLoading"
        @click="submitCreateUser"
      >
        {{ t('settings.createUser') }}
      </el-button>
    </template>
  </el-dialog>

  <!-- Reset password dialog -->
  <el-dialog
    v-model="resetDialogVisible"
    :title="resetTarget ? t('settings.newPasswordFor', { username: resetTarget.username }) : ''"
    width="360px"
  >
    <el-form
      ref="resetFormRef"
      :model="resetForm"
      :rules="resetRules"
      label-position="top"
    >
      <el-form-item
        :label="t('settings.newPassword')"
        prop="new_password"
      >
        <el-input
          v-model="resetForm.new_password"
          type="password"
          show-password
          autocomplete="new-password"
        />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="resetDialogVisible = false">
        Cancel
      </el-button>
      <el-button
        type="primary"
        :loading="resetLoading"
        @click="submitResetPassword"
      >
        {{ t('settings.resetPassword') }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped lang="scss">
.card { margin-top: 16px; }
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
  h3 { margin: 0; }
}
.row {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-top: 8px;
  .lbl { min-width: 140px; color: var(--el-text-color-regular); }
}
.pw-form { max-width: 360px; }
.sub { font-size: 12px; font-weight: 400; color: var(--el-text-color-secondary); margin-left: 8px; }
.user-table { margin-top: 8px; }
</style>
