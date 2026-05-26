import { ref } from 'vue'
import { defineStore } from 'pinia'
import { setI18nLocale, type LocaleCode } from '@/i18n'

type Theme = 'light' | 'dark'

export const useUiStore = defineStore('ui', () => {
  const theme = ref<Theme>(
    (localStorage.getItem('dlw_theme') as Theme) ??
      (window.matchMedia?.('(prefers-color-scheme: dark)').matches
        ? 'dark' : 'light'),
  )
  const sidebarCollapsed = ref(localStorage.getItem('dlw_sidebar') === 'true')
  const copilotOpen = ref(false)
  const helpOpen = ref(false)
  const docsOpen = ref(false)
  const locale = ref<LocaleCode>(
    (localStorage.getItem('dlw_locale') as LocaleCode) ?? 'zh-CN',
  )

  function applyTheme(): void {
    document.documentElement.classList.toggle('dark', theme.value === 'dark')
  }
  function toggleTheme(): void {
    theme.value = theme.value === 'dark' ? 'light' : 'dark'
    localStorage.setItem('dlw_theme', theme.value)
    applyTheme()
  }
  function toggleSidebar(): void {
    sidebarCollapsed.value = !sidebarCollapsed.value
    localStorage.setItem('dlw_sidebar', String(sidebarCollapsed.value))
  }
  function toggleCopilot(): void {
    copilotOpen.value = !copilotOpen.value
  }
  function toggleHelp(): void {
    helpOpen.value = !helpOpen.value
  }
  function toggleDocs(): void {
    docsOpen.value = !docsOpen.value
  }
  function setLocale(l: LocaleCode): void {
    locale.value = l
    localStorage.setItem('dlw_locale', l)
    setI18nLocale(l)
  }
  function hydrate(): void {
    applyTheme()
    setI18nLocale(locale.value)
  }

  return { theme, sidebarCollapsed, copilotOpen, helpOpen, docsOpen, locale,
    toggleTheme, toggleSidebar, toggleCopilot, toggleHelp, toggleDocs,
    setLocale, hydrate }
})
