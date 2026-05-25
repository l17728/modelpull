import { beforeEach, describe, expect, test, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { createI18n } from 'vue-i18n'
import en from '@/locale/en-US.json'
import Login from '@/pages/Login.vue'

const replace = vi.fn()
const push = vi.fn()
let mockQuery: Record<string, unknown> = {}
vi.mock('vue-router', async (importOriginal) => ({
  ...(await importOriginal<typeof import('vue-router')>()),
  useRouter: () => ({ push, replace }),
  useRoute: () => ({ query: mockQuery }),
}))

vi.mock('axios', async (importOriginal) => {
  const actual = await importOriginal<typeof import('axios')>()
  return {
    ...actual,
    default: {
      ...actual.default,
      post: vi.fn().mockResolvedValue({
        data: { access_token: 'fake-token', must_change_password: false },
      }),
    },
  }
})

const i18n = createI18n({ legacy: false, locale: 'en-US', messages: { 'en-US': en } })
function mountLogin() {
  return mount(Login, { global: { plugins: [ElementPlus, i18n] } })
}

type FormVM = { form: { username: string; password: string }; onSubmit: () => Promise<void> }

describe('Login redirect-after-login (FU6-UI)', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    replace.mockClear(); push.mockClear()
    mockQuery = {}
  })

  test('honors same-origin /device?user_code=... redirect', async () => {
    mockQuery = { redirect: '/device?user_code=ABCD-1234' }
    const w = mountLogin()
    ;(w.vm as unknown as FormVM).form.username = 'user'
    ;(w.vm as unknown as FormVM).form.password = 'pass1234'
    await (w.vm as unknown as FormVM).onSubmit()
    expect(replace).toHaveBeenCalledWith('/device?user_code=ABCD-1234')
  })

  test('rejects external https:// redirect, falls back to /', async () => {
    mockQuery = { redirect: 'https://attacker.example/' }
    const w = mountLogin()
    ;(w.vm as unknown as FormVM).form.username = 'user'
    ;(w.vm as unknown as FormVM).form.password = 'pass1234'
    await (w.vm as unknown as FormVM).onSubmit()
    expect(replace).toHaveBeenCalledWith('/')
  })

  test('rejects protocol-relative // redirect, falls back to /', async () => {
    mockQuery = { redirect: '//attacker.example/' }
    const w = mountLogin()
    ;(w.vm as unknown as FormVM).form.username = 'user'
    ;(w.vm as unknown as FormVM).form.password = 'pass1234'
    await (w.vm as unknown as FormVM).onSubmit()
    expect(replace).toHaveBeenCalledWith('/')
  })

  test('rejects /login redirect (loop defense), falls back to /', async () => {
    mockQuery = { redirect: '/login?redirect=/login' }
    const w = mountLogin()
    ;(w.vm as unknown as FormVM).form.username = 'user'
    ;(w.vm as unknown as FormVM).form.password = 'pass1234'
    await (w.vm as unknown as FormVM).onSubmit()
    expect(replace).toHaveBeenCalledWith('/')
  })

  test('rejects array-typed redirect (vue-router types it string|string[]|null)', async () => {
    mockQuery = { redirect: ['/a', '/b'] }
    const w = mountLogin()
    ;(w.vm as unknown as FormVM).form.username = 'user'
    ;(w.vm as unknown as FormVM).form.password = 'pass1234'
    await (w.vm as unknown as FormVM).onSubmit()
    expect(replace).toHaveBeenCalledWith('/')
  })

  test('no redirect query → defaults to /', async () => {
    mockQuery = {}
    const w = mountLogin()
    ;(w.vm as unknown as FormVM).form.username = 'user'
    ;(w.vm as unknown as FormVM).form.password = 'pass1234'
    await (w.vm as unknown as FormVM).onSubmit()
    expect(replace).toHaveBeenCalledWith('/')
  })
})
