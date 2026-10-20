import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': process.env.EV_API_TARGET || 'http://127.0.0.1:8001',
      '/ws': { target: (process.env.EV_API_TARGET || 'http://127.0.0.1:8001').replace(/^http/, 'ws'), ws: true },
    },
  },
  build: { outDir: 'dist', sourcemap: true },
})
