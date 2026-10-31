import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import '@element-plus/icons-vue'
import './styles.css'
import App from './App.vue'
import { initSecureStore } from './secure-store'

document.documentElement.classList.add('dark')

const app = createApp(App)
app.use(createPinia())
app.use(ElementPlus, { locale: zhCn })
// mount only after the credential cache is warm (desktop: OS credential
// store; browser: localStorage); on failure we still mount - the user can
// re-enter the key in Settings
initSecureStore()
  .catch(() => {})
  .finally(() => app.mount('#app'))
