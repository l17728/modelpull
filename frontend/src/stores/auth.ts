import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

const STORAGE_KEY = 'dlw_token'

export const useAuthStore = defineStore('auth', () => {
  const accessToken = ref<string | null>(localStorage.getItem(STORAGE_KEY))

  function login(token: string) {
    localStorage.setItem(STORAGE_KEY, token)
    accessToken.value = token
  }

  function logout() {
    localStorage.removeItem(STORAGE_KEY)
    accessToken.value = null
  }

  const isAuthenticated = computed(() => accessToken.value !== null)

  return { accessToken, isAuthenticated, login, logout }
})
