import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'
import { createPinia } from 'pinia'
import { createApp } from 'vue'
import App from './App.vue'
import { vAdmin, vPerm } from './directives/admin'
import router from './router'
import './styles/index.scss'

createApp(App).use(createPinia()).use(router).use(ElementPlus, { locale: zhCn }).directive('admin', vAdmin).directive('perm', vPerm).mount('#app')
