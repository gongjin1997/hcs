import { fileURLToPath, URL } from 'node:url'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'
import { defineConfig } from 'vite'
import pkg from './package.json'

const proxy = { '/api': { target: process.env.VITE_API_TARGET || 'http://localhost:8000', changeOrigin: true } }

/**
 * 大依赖单独分包：业务代码变更时不影响其缓存。
 * 注意：Element Plus 不要手工归入同一个 chunk —— 把 element-plus/* 全部强制并到一个 chunk 会连带打进
 * 全量组件安装器（es/index → defaults → component），按需引入失效（实测 ~950KB）；交给 Rollup 自动拆分即可。
 */
function chunkOf(id: string): string | undefined {
  if (!id.includes('node_modules')) return undefined
  if (/node_modules[\\/](echarts|zrender|tslib)[\\/]/.test(id)) return 'echarts'
  if (/node_modules[\\/](vue|@vue|vue-router|pinia|axios|dayjs)[\\/]/.test(id)) return 'vendor'
  return undefined
}

export default defineConfig({
  plugins: [
    vue(),
    // Element Plus 组件按需引入（模板里用到的组件 / v-loading 指令自动 import，JS 体积约减半）；
    // 样式仍在 main.ts 整体引入一次：保证 styles/index.scss 的覆盖样式始终排在组件样式之后（按需样式随页面异步加载会打乱层叠顺序）
    Components({ resolvers: [ElementPlusResolver({ importStyle: false })], dirs: [], dts: false }),
  ],
  define: { __APP_VERSION__: JSON.stringify(pkg.version) },
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: { port: 5173, proxy },
  preview: { port: 4173, proxy },
  build: {
    chunkSizeWarningLimit: 1200,
    rollupOptions: { output: { manualChunks: chunkOf } },
  },
})
