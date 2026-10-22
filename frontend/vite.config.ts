import { fileURLToPath, URL } from 'node:url'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

const proxy = { '/api': { target: process.env.VITE_API_TARGET || 'http://localhost:8000', changeOrigin: true } }

export default defineConfig({
  plugins: [vue()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: { port: 5173, proxy },
  preview: { port: 4173, proxy },
  build: {
    chunkSizeWarningLimit: 1500,
    rollupOptions: {
      output: {
        // 大依赖单独分包，业务代码变更时不影响其缓存
        manualChunks: { 'element-plus': ['element-plus'], echarts: ['echarts'], vendor: ['vue', 'vue-router', 'pinia', 'axios', 'dayjs'] },
      },
    },
  },
})
