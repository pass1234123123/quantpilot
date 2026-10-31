<script setup lang="ts">
import { computed, ref } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'
import { api } from '../../api'

const store = useDashboardStore()

// 真实环境信息：来自 nodes / terminals / snapshot 上报
const node = computed(() => store.nodes[0] ?? null)
const terminal = computed(() =>
  store.terminals.find((t) => t.node_id === node.value?.node_id) ?? store.terminals[0] ?? null,
)
const account = computed(() => store.selectedAccount)
const snap = computed(() => store.snapshot)

// 桌面连接（Tauri 生产模式无 Vite 代理，需直连 API 并自带密钥）
const connBase = ref(localStorage.getItem('qp.apiBase') ?? '')
const connKey = ref(localStorage.getItem('qp.apiKey') ?? '')
const connTesting = ref(false)
const connResult = ref<'' | 'ok' | 'fail'>('')
const connError = ref('')

function saveConn() {
  localStorage.setItem('qp.apiBase', connBase.value.trim())
  localStorage.setItem('qp.apiKey', connKey.value.trim())
  connResult.value = ''
}

function clearConn() {
  localStorage.removeItem('qp.apiBase')
  localStorage.removeItem('qp.apiKey')
  connBase.value = ''
  connKey.value = ''
  connResult.value = ''
}

async function testConn() {
  connTesting.value = true
  connResult.value = ''
  connError.value = ''
  try {
    await api.nodes()
    connResult.value = 'ok'
  } catch (e) {
    connResult.value = 'fail'
    connError.value = e instanceof Error ? e.message : String(e)
  } finally {
    connTesting.value = false
  }
}

const timeText = (iso: string | null) =>
  iso ? new Date(iso).toLocaleString('zh-CN', { hour12: false }) : '—'
</script>

<template>
  <div class="settings">
    <div class="qp-panel">
      <div class="qp-panel__title">
        系统信息
        <span class="qp-live-badge">真实数据</span>
      </div>
      <div class="settings__rows">
        <div class="settings__row"><span>系统模式</span><b class="qp-up">OBSERVE_ONLY（只读观察）</b></div>
        <div class="settings__row"><span>Agent 版本</span><b>{{ node?.agent_version ?? '—' }}</b></div>
        <div class="settings__row"><span>EA 版本</span><b>{{ snap?.ea_version ?? '—' }}</b></div>
        <div class="settings__row"><span>终端 build</span><b>{{ snap?.terminal_version ?? terminal?.terminal_build ?? '—' }}</b></div>
        <div class="settings__row"><span>节点状态</span><b :class="node?.status === 'ONLINE' ? 'qp-up' : 'qp-dim'">{{ node?.status ?? '—' }}</b></div>
        <div class="settings__row"><span>节点最近上报</span><b>{{ timeText(node?.last_seen_at ?? null) }}</b></div>
      </div>
    </div>

    <div class="qp-panel">
      <div class="qp-panel__title">
        当前账户
        <span class="qp-live-badge">真实数据</span>
      </div>
      <div class="settings__rows">
        <div class="settings__row"><span>账号</span><b>{{ account?.login_ref ?? '—' }}</b></div>
        <div class="settings__row"><span>服务器</span><b>{{ account?.server ?? '—' }}</b></div>
        <div class="settings__row"><span>经纪商</span><b>{{ account?.company ?? '—' }}</b></div>
        <div class="settings__row"><span>账户状态</span><b>{{ account?.status ?? '—' }}</b></div>
      </div>
    </div>

    <div class="qp-panel">
      <div class="qp-panel__title">
        桌面连接
        <span class="qp-live-badge">Tauri 生产模式</span>
      </div>
      <div class="settings__rows">
        <div class="settings__row">
          <span>API 地址</span>
          <el-input v-model="connBase" placeholder="http://127.0.0.1:8000" size="small" style="width: 320px" />
        </div>
        <div class="settings__row">
          <span>API Key</span>
          <el-input v-model="connKey" type="password" show-password placeholder="services/api 的 QP_API_KEY" size="small" style="width: 320px" />
        </div>
      </div>
      <div class="settings__conn">
        <el-button size="small" type="primary" @click="saveConn">保存</el-button>
        <el-button size="small" :loading="connTesting" @click="testConn">测试连接</el-button>
        <el-button size="small" text @click="clearConn">清除</el-button>
        <el-tag v-if="connResult === 'ok'" type="success" size="small">连接成功，密钥有效</el-tag>
        <el-tag v-else-if="connResult === 'fail'" type="danger" size="small">失败：{{ connError }}</el-tag>
      </div>
      <div class="settings__note">
        <p>· 开发模式（Vite 代理）无需配置：代理自动注入 X-API-Key，保持相对路径即可。</p>
        <p>· 桌面安装版没有代理：请配置 API 地址与密钥（存于本机 localStorage），保存后立即生效并直连云端。</p>
      </div>
    </div>

    <div class="qp-panel">
      <div class="qp-panel__title">设置说明</div>
      <div class="settings__note">
        <p>· <b>交易参数过滤与 EA 开关</b>：在「过滤设置」「EA 设置」页调整，保存后写入云端 risk_profiles（版本化追加，EA 通过本地 Agent 定期拉取生效）。</p>
        <p>· <b>系统连接配置</b>（数据库 / NATS / Agent 端口等）：由服务侧配置文件与环境变量管理，本页面不提供修改入口。</p>
        <p>· <b>多账户</b>：通过顶部账户选择器切换，各账户数据完全隔离。</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.settings { display: flex; flex-direction: column; gap: 12px; }

.settings__rows { display: flex; flex-direction: column; gap: 6px; }
.settings__row {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 11.5px;
  padding: 3px 0;
  border-bottom: 1px solid rgba(148, 163, 184, 0.07);
}
.settings__row:last-child { border-bottom: none; }
.settings__row span { color: var(--qp-text-dim); white-space: nowrap; }
.settings__row b { font-weight: 600; text-align: right; }

.settings__conn { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }

.settings__note { display: flex; flex-direction: column; gap: 8px; }
.settings__note p {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.7;
  color: var(--qp-text-dim);
}
.settings__note b { color: var(--qp-text); font-weight: 600; }
</style>
