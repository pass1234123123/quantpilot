import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// R2 remediation P1-01: the dev proxy NO LONGER injects the shared
// service key. The service key is a machine lane (agent/EA) only; dev
// sessions must authenticate like production - username/password login
// (Bearer JWT) or a personal qpk_ API key. Without credentials the API
// answers 401 and the login gate opens.
export default defineConfig({
  plugins: [vue()],
  server: {
    host: true, // bind all interfaces (IPv4 + IPv6); Node 17+ default resolves localhost to ::1 only
    port: 5173,
    proxy: {
      // dev proxy -> cloud API (services/api), avoids CORS
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/healthz': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
