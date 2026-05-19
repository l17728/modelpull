import { createI18n } from 'vue-i18n'
import zhCN from './locale/zh-CN.json'
import enUS from './locale/en-US.json'

export type LocaleCode = 'zh-CN' | 'en-US'

export const i18n = createI18n({
  legacy: false,
  locale: (localStorage.getItem('dlw_locale') as LocaleCode) ?? 'zh-CN',
  fallbackLocale: 'zh-CN',
  messages: { 'zh-CN': zhCN, 'en-US': enUS },
})

export function setI18nLocale(l: LocaleCode): void {
  i18n.global.locale.value = l
}
