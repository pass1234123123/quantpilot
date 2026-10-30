import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import '@element-plus/icons-vue'
import './styles.css'
import App from './App.vue'

document.documentElement.classList.add('dark')

const app = createApp(App)
app.use(createPinia())
app.use(ElementPlus)
app.mount('#app')
