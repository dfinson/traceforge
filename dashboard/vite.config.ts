import path from 'path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    // Dev proxy so the SPA can hit the read-only API served by
    // `traceforge dashboard` (default 127.0.0.1:7788) without CORS.
    proxy: {
      '/api': 'http://127.0.0.1:7788',
    },
  },
})
