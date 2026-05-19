import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5173,
    // A browser SPA can only talk plain HTTP through the dev proxy, so the
    // default targets a plain-HTTP controller. In the shipped deployment
    // the executor-facing controller is HTTPS+mTLS (e.g. :8000) and a
    // separate browser-friendly plain-HTTP instance is run (e.g. :8001).
    // Override with DLW_API_PROXY when your controller is elsewhere.
    proxy: {
      '/api': {
        target: process.env.DLW_API_PROXY ?? 'http://localhost:8001',
        changeOrigin: false,
      },
      '/health': {
        target: process.env.DLW_API_PROXY ?? 'http://localhost:8001',
        changeOrigin: false,
      },
    },
  },
})
