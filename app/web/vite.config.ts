import http from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const rootDir = path.dirname(fileURLToPath(import.meta.url))
const proxyAgent = new http.Agent({ keepAlive: true, maxSockets: 16 })

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(rootDir, './src'),
    },
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    watch: {
      usePolling: true,
      interval: 300,
    },
    proxy: {
      '/api': { target: 'http://127.0.0.1:18080', agent: proxyAgent },
      '/health': { target: 'http://127.0.0.1:18080', agent: proxyAgent },
    },
  },
  build: {
    rollupOptions: {
      input: path.resolve(rootDir, 'dashboard.html'),
    },
  },
})
