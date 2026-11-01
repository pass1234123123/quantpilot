<script setup lang="ts">
import { ref } from 'vue'
import { api, type AuthUser } from '../api'

// R3 P1-02: dual-mode gate - username/password (JWT) OR the personal
// qpk_ API key (own credential slot, X-API-Key lane). A shared service
// key is a machine lane and is rejected before any request is sent.
type Mode = 'password' | 'apikey'

const emit = defineEmits<{
  (e: 'success', user: AuthUser): void
  (e: 'reconfigure'): void
}>()

const mode = ref<Mode>('password')
const username = ref('')
const password = ref('')
const apiKey = ref('')
const busy = ref(false)
const errorText = ref('')

async function submit() {
  if (busy.value) return
  busy.value = true
  errorText.value = ''
  try {
    let user: AuthUser
    if (mode.value === 'password') {
      if (!username.value.trim() || !password.value) return
      user = await api.login(username.value.trim(), password.value)
      password.value = ''
    } else {
      if (!apiKey.value.trim()) return
      user = await api.loginWithApiKey(apiKey.value)
      apiKey.value = ''
    }
    emit('success', user)
  } catch (e) {
    errorText.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="login">
    <div class="login__card">
      <div class="login__brand">
        <div class="login__logo">Q</div>
        <div>
          <div class="login__name">QUANTPILOT AI</div>
          <div class="login__sub">多用户安全登录 · 个人身份访问业务 API</div>
        </div>
      </div>

      <el-radio-group v-model="mode" size="large" class="login__modes">
        <el-radio-button value="password">用户名密码</el-radio-button>
        <el-radio-button value="apikey">个人 API Key</el-radio-button>
      </el-radio-group>

      <template v-if="mode === 'password'">
        <el-input
          v-model="username"
          placeholder="用户名"
          size="large"
          autocomplete="username"
          @keyup.enter="submit"
        />
        <el-input
          v-model="password"
          type="password"
          show-password
          placeholder="密码"
          size="large"
          autocomplete="current-password"
          @keyup.enter="submit"
        />
      </template>
      <el-input
        v-else
        v-model="apiKey"
        type="password"
        show-password
        placeholder="qpk_…（个人密钥，AI/账户授权管理中创建）"
        size="large"
        @keyup.enter="submit"
      />

      <el-button type="primary" size="large" class="login__btn" :loading="busy" @click="submit">
        登 录
      </el-button>

      <el-alert
        v-if="errorText"
        :title="`登录失败：${errorText}`"
        type="error"
        show-icon
        :closable="false"
      />

      <el-link
        type="primary"
        :underline="false"
        class="login__reconf"
        @click="emit('reconfigure')"
      >
        修改 API 连接地址
      </el-link>

      <div class="login__note">
        <p>· 个人 API Key 以 qpk_ 开头；共享服务密钥仅用于 Agent / EA 机器通道，不能登录。</p>
        <p>· 登录 Token 与个人 API Key 分开保存于系统凭据管理器（桌面端），不写入浏览器存储。</p>
        <p>· 角色与账户授权由管理员分配；密钥撤销或会话过期后需重新登录。</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login {
  height: 100vh;
  display: grid;
  place-items: center;
  background:
    radial-gradient(900px 500px at 70% -10%, rgba(0, 224, 240, 0.08), transparent),
    radial-gradient(700px 400px at 10% 110%, rgba(0, 122, 138, 0.1), transparent),
    #0b1420;
}

.login__card {
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

.login__brand {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 6px;
}

.login__logo {
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

.login__name { font-weight: 700; letter-spacing: 1.5px; font-size: 15px; }
.login__sub { font-size: 10.5px; color: var(--qp-text-dim); letter-spacing: 0.6px; margin-top: 3px; }
.login__modes { display: flex; width: 100%; }
.login__modes :deep(.el-radio-button) { flex: 1; }
.login__modes :deep(.el-radio-button__inner) { width: 100%; }
.login__btn { width: 100%; letter-spacing: 6px; }
.login__reconf { width: 100%; justify-content: center; font-size: 11.5px; }

.login__note { display: flex; flex-direction: column; gap: 6px; margin-top: 4px; }
.login__note p {
  margin: 0;
  font-size: 10.5px;
  line-height: 1.7;
  color: var(--qp-text-dim);
}
</style>
