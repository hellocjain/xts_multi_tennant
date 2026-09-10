import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8500',
        changeOrigin: true,
      },
      '/admin/api': {
        target: 'http://localhost:8500',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:8500',
        ws: true,
      }
    }
  }
})
