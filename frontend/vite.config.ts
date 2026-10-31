import fs from 'node:fs'
import path from 'node:path'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// Dev proxy injects the service key server-side (from repo root .env or
// process env) so the key never ships in the client bundle.
function repoEnv(name: string): string {
  try {
    const env = fs.readFileSync(path.resolve(process.cwd(), '../.env'), 'utf8')
    const line = env.split(/\r?\n/).find((l) => l.startsWith(`${name}=`))
    return line ? line.slice(name.length + 1).trim() : ''
  } catch {
    return ''
  }
}

const apiKey = process.env.QP_API_KEY || repoEnv('QP_API_KEY')

// https://vite.dev/config/
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
        headers: apiKey ? { 'X-API-Key': apiKey } : {},
      },
      '/healthz': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
