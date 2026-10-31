<script setup lang="ts">
import { ref } from 'vue'
import { api, type AuthUser } from '../api'

// R2 remediation P2-05/T6: standalone login gate. The app must not show
// any business view before a personal identity exists (JWT via login or
// a personal qpk_ key configured in Settings).
const emit = defineEmits<{ (e: 'success', user: AuthUser): void }>()

const username = ref('')
const password = ref('')
const busy = ref(false)
const errorText = ref('')

async function submit() {
  if (!username.value.trim() || !password.value) return
  busy.value = true
  errorText.value = ''
  try {
    const user = await api.login(username.value.trim(), password.value)
    password.value = ''
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

      <div class="login__note">
        <p>· 共享服务密钥仅用于 Agent / EA 机器通道，不能作为用户身份登录。</p>
        <p>· 登录 Token 保存于系统凭据管理器（桌面端），不写入浏览器存储。</p>
        <p>· 角色与账户授权由管理员分配；会话过期后需重新登录。</p>
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
.login__btn { width: 100%; letter-spacing: 6px; }

.login__note { display: flex; flex-direction: column; gap: 6px; margin-top: 4px; }
.login__note p {
  margin: 0;
  font-size: 10.5px;
  line-height: 1.7;
  color: var(--qp-text-dim);
}
</style>
