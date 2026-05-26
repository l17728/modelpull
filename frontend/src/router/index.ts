import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

export const routes: RouteRecordRaw[] = [
  {
    path: '/login', name: 'login',
    component: () => import('@/pages/Login.vue'),
    meta: { public: true },
  },
  {
    path: '/', name: 'dashboard',
    component: () => import('@/pages/Dashboard.vue'),
  },
  {
    path: '/tasks', name: 'taskList',
    component: () => import('@/pages/TaskList.vue'),
  },
  {
    path: '/tasks/new', name: 'taskCreate',
    component: () => import('@/pages/TaskCreate.vue'),
  },
  {
    path: '/tasks/:id', name: 'taskDetail',
    component: () => import('@/pages/TaskDetail.vue'),
    props: true,
  },
  {
    path: '/executors', name: 'executors',
    component: () => import('@/pages/Executors.vue'),
  },
  {
    path: '/audit', name: 'audit',
    component: () => import('@/pages/Audit.vue'),
  },
  {
    path: '/quota', name: 'quota',
    component: () => import('@/pages/QuotaPage.vue'),
  },
  {
    path: '/replication', name: 'replication',
    component: () => import('@/pages/Replication.vue'),
  },
  {
    path: '/settings', name: 'settings',
    component: () => import('@/pages/Settings.vue'),
  },
  {
    path: '/device', name: 'device',
    component: () => import('@/pages/Device.vue'),
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach((to) => {
  if (to.meta.public) return true
  const auth = useAuthStore()
  if (!auth.isAuthenticated) return { path: '/login', query: { redirect: to.fullPath } }
  return true
})

export { router }
export default router
