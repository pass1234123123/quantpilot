<script setup lang="ts">
import { ref } from 'vue'
import { getApiBase, setApiBase, testApiBase, validateApiBase } from '../api'

// R3 P1-01: FIRST-RUN connection setup, shown BEFORE the login gate.
// The desktop shell (Tauri production) has no Vite proxy: without an
// API base the login request hits the webview origin and never reaches
// the cloud API - the first-install deadlock. This page asks ONLY for
// the API address: no shared service key may be collected here (machine
// lane, never a user identity). The base is validated, connectivity-
// tested against the public /healthz, and persisted only on success.
const emit = defineEmits<{ (e: 'configured'): void }>()

const baseUrl = ref(getApiBase())
const busy = ref(false)
const errorText = ref('')

async function save() {
  if (busy.value) return
  const raw = baseUrl.value.trim()
  const check = validateApiBase(raw)
  if (!check.ok) {
    errorText.value = check.error ?? 'API 地址无效'
    return
  }
  busy.value = true
  errorText.value = ''
  try {
    await testApiBase(raw)
    setApiBase(raw) // persist only after the probe passes
    emit('configured')
  } catch (e) {
    errorText.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="setup">
    <div class="setup__card">
      <div class="setup__brand">
        <div class="setup__logo">Q</div>
        <div>
          <div class="setup__name">QUANTPILOT AI</div>
          <div class="setup__sub">首次连接配置 · 指向云端 API</div>
        </div>
      </div>

      <el-input
        v-model="baseUrl"
        placeholder="http://127.0.0.1:8000"
        size="large"
        clearable
        @keyup.enter="save"
      />
      <el-button type="primary" size="large" class="setup__btn" :loading="busy" @click="save">
        测试连接并保存
      </el-button>

      <el-alert
        v-if="errorText"
        :title="`连接测试失败：${errorText}`"
        type="error"
        show-icon
        :closable="false"
      />

      <div class="setup__note">
        <p>· 填写云端 API 地址（协议 + 主机 + 端口），例如 http://127.0.0.1:8000。</p>
        <p>· 此页面只配置连接地址；用户身份请使用用户名密码或个人 API Key（qpk_）在登录页登录。</p>
        <p>· 共享服务密钥仅用于 Agent / EA 机器通道，不能在此填写，也不能登录。</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.setup {
  height: 100vh;
  display: grid;
  place-items: center;
  background:
    radial-gradient(900px 500px at 70% -10%, rgba(0, 224, 240, 0.08), transparent),
    radial-gradient(700px 400px at 10% 110%, rgba(0, 122, 138, 0.1), transparent),
    #0b1420;
}

.setup__card {
  width: 360px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 34px 32px;
  border: 1px solid var(--qp-border);
  border-radius: 14px;
  background: rgba(13, 24, 38, 0.92);
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.45);
}

.setup__brand {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 6px;
}

.setup__logo {
  width: 42px;
  height: 42px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  font-weight: 800;
  font-size: 20px;
  color: #031015;
  background: linear-gradient(135deg, #00e0f0, #007a8a);
}

.setup__name { font-weight: 700; letter-spacing: 1.5px; font-size: 15px; }
.setup__sub { font-size: 10.5px; color: var(--qp-text-dim); letter-spacing: 0.6px; margin-top: 3px; }
.setup__btn { width: 100%; letter-spacing: 2px; }

.setup__note { display: flex; flex-direction: column; gap: 6px; margin-top: 4px; }
.setup__note p {
  margin: 0;
  font-size: 10.5px;
  line-height: 1.7;
  color: var(--qp-text-dim);
}
</style>
