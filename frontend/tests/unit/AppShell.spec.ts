import { beforeEach, describe, expect, test, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { createI18n } from 'vue-i18n'
import AppShell from '@/components/shell/AppShell.vue'
import zh from '@/locale/zh-CN.json'
import { useAuthStore } from '@/stores/auth'

const i18n = createI18n({ legacy: false, locale: 'zh-CN', messages: { 'zh-CN': zh } })
const push = vi.fn()
vi.mock('vue-router', async (importOriginal) => ({
  // SP4a: AppShell now transitively imports @/api/client → @/router, which
  // calls createRouter at module load — so keep the real router factories.
  ...(await importOriginal<typeof import('vue-router')>()),
  useRouter: () => ({ push }),
  useRoute: () => ({ name: 'taskList' }),
  RouterView: { template: '<div class="rv" />' },
}))

function mountShell() {
  return mount(AppShell, { global: { plugins: [ElementPlus, i18n] } })
}

describe('AppShell', () => {
  beforeEach(() => { setActivePinia(createPinia()); push.mockClear() })

  test('authenticated → renders nav items', () => {
    useAuthStore().login('h.' + btoa(JSON.stringify(
      { sub: '1', tid: 1, role: 'tenant_admin', pids: [] })) + '.s')
    const w = mountShell()
    expect(w.text()).toContain(zh.nav.tasks)
    expect(w.text()).toContain(zh.nav.dashboard)
  })

  test('logout calls auth.logout + redirects', async () => {
    const auth = useAuthStore()
    auth.login('h.' + btoa(JSON.stringify(
      { sub: '1', tid: 1, role: 'tenant_admin', pids: [] })) + '.s')
    const w = mountShell()
    await w.find('[data-test=logout]').trigger('click')
    expect(auth.isAuthenticated).toBe(false)
    expect(push).toHaveBeenCalledWith('/login')
  })
})
