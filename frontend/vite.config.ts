import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  server: {
    host: true, // bind all interfaces (IPv4 + IPv6); Node 17+ default resolves localhost to ::1 only
    port: 5173,
    proxy: {
      // dev proxy -> cloud API (services/api), avoids CORS
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/healthz': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
