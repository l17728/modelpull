<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import AppShell from '@/components/shell/AppShell.vue'
import CommandPalette from '@/components/CommandPalette.vue'

const route = useRoute()
const auth = useAuthStore()
const chrome = computed(
  () => auth.isAuthenticated && route.name !== 'login',
)
</script>

<template>
  <template v-if="chrome">
    <AppShell>
      <RouterView />
    </AppShell>
    <CommandPalette />
  </template>
  <RouterView v-else />
</template>
