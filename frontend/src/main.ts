import 'element-plus/dist/index.css'
import './styles/main.scss'

import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { VueQueryPlugin } from '@tanstack/vue-query'
import ElementPlus from 'element-plus'

import App from './App.vue'
import router from './router'
import { i18n } from './i18n'
import { useUiStore } from './stores/ui'

const app = createApp(App)
const pinia = createPinia()
app.use(pinia)
app.use(router)
app.use(i18n)
app.use(ElementPlus)
app.use(VueQueryPlugin, {
  queryClientConfig: {
    defaultOptions: {
      queries: { retry: 3, refetchOnWindowFocus: true, staleTime: 5_000 },
    },
  },
})
useUiStore().hydrate()
// Pre-review fix B-I2: mount AFTER the initial route resolves so App.vue's
// route.name is defined on first render — otherwise an authenticated user
// briefly sees the bare (login) layout flash.
router.isReady().then(() => app.mount('#app'))
