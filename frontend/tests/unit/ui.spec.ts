import { beforeEach, describe, expect, test, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

// vi.mock is hoisted — its factory must not reference an outer const
// (TDZ). Use vi.hoisted so the mock fn is created in the hoisted scope.
const { setLocaleMock } = vi.hoisted(() => ({ setLocaleMock: vi.fn() }))
vi.mock('@/i18n', () => ({ setI18nLocale: setLocaleMock }))

import { useUiStore } from '@/stores/ui'

describe('ui store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    document.documentElement.classList.remove('dark')
    setLocaleMock.mockClear()
  })

  test('toggleTheme flips + persists + sets html.dark', () => {
    const ui = useUiStore()
    expect(ui.theme).toBe('light')
    ui.toggleTheme()
    expect(ui.theme).toBe('dark')
    expect(document.documentElement.classList.contains('dark')).toBe(true)
    expect(localStorage.getItem('dlw_theme')).toBe('dark')
  })

  test('setLocale persists + calls i18n setter', () => {
    const ui = useUiStore()
    ui.setLocale('en-US')
    expect(ui.locale).toBe('en-US')
    expect(localStorage.getItem('dlw_locale')).toBe('en-US')
    expect(setLocaleMock).toHaveBeenCalledWith('en-US')
  })

  test('toggleSidebar persists', () => {
    const ui = useUiStore()
    const before = ui.sidebarCollapsed
    ui.toggleSidebar()
    expect(ui.sidebarCollapsed).toBe(!before)
    expect(localStorage.getItem('dlw_sidebar')).toBe(String(!before))
  })
})
