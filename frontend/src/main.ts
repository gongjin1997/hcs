import { provideGlobalConfig } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
// Element Plus 组件按需引入（见 vite.config.ts）；样式整体引入一次，且必须在 styles/index.scss 之前
import 'element-plus/dist/index.css'
import { createPinia } from 'pinia'
import { createApp } from 'vue'
import App from './App.vue'
import { vAdmin, vPerm } from './directives/admin'
import router from './router'
import './styles/index.scss'

const app = createApp(App)
// 全局中文：组件内通过注入获取；ElMessageBox 等函数式调用使用全局配置（第三个参数 true）
provideGlobalConfig({ locale: zhCn }, app, true)
app.use(createPinia()).use(router).directive('admin', vAdmin).directive('perm', vPerm).mount('#app')
