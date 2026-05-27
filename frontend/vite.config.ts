import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// Shared proxy config — used by both `vite` (dev server) and
// `vite preview` (production-shape static server). Lets a single
// browser origin (5173) reach both the SPA and the controller API
// without exposing the controller's port publicly or wiring CORS.
const apiProxy = {
  '/api': {
    target: process.env.DLW_API_PROXY ?? 'http://localhost:8001',
    changeOrigin: false,
  },
  '/health': {
    target: process.env.DLW_API_PROXY ?? 'http://localhost:8001',
    changeOrigin: false,
  },
  '/metrics': {
    target: process.env.DLW_API_PROXY ?? 'http://localhost:8001',
    changeOrigin: false,
  },
}

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
    proxy: apiProxy,
  },
  preview: {
    // vite preview is what deploy/single-host's docker-compose runs in
    // production-shape. It serves the built dist + proxies /api so the
    // single 5173 port carries both UI and API to the browser.
    port: 5173,
    host: '0.0.0.0',
    proxy: apiProxy,
    // vite 4.6+ rejects unknown Host headers by default. Vite 6
    // accepts `true` to disable the check; vite 5.4.x (what we
    // currently pin) only accepts string[]. Build a list of common
    // POC hosts + whatever the operator passes via
    // DLW_PREVIEW_ALLOWED_HOSTS=catown.cloud,1.2.3.4 (CSV).
    allowedHosts: (() => {
      const extra = (process.env.DLW_PREVIEW_ALLOWED_HOSTS ?? '')
        .split(',').map((s) => s.trim()).filter(Boolean)
      return [
        'localhost', '127.0.0.1', '0.0.0.0',
        // wildcard host accepted by vite — covers any subdomain
        '.catown.cloud', 'catown.cloud',
        ...extra,
      ]
    })(),
  },
})
