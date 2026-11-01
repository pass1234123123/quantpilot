<script setup lang="ts">
import { computed, ref } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'
import { getApiBase, setApiBase, testApiBase, validateApiBase } from '../../api'

const store = useDashboardStore()

// 真实环境信息：来自 nodes / terminals / snapshot 上报
const node = computed(() => store.nodes[0] ?? null)
const terminal = computed(() =>
  store.terminals.find((t) => t.node_id === node.value?.node_id) ?? store.terminals[0] ?? null,
)
const account = computed(() => store.selectedAccount)
const snap = computed(() => store.snapshot)

// 桌面连接（Tauri 生产模式无 Vite 代理，需直连 API）。
// R3 P1-01/P1-02：此处仅维护 API 地址；登录凭据（JWT / 个人 qpk_ Key）
// 统一在登录页管理并保存于系统凭据管理器，Settings 不再收集任何密钥。
const connBase = ref(getApiBase())
const connBusy = ref(false)
const connResult = ref<'' | 'ok' | 'fail'>('')
const connError = ref('')

async function saveConn() {
  if (connBusy.value) return
  const raw = connBase.value.trim()
  const check = validateApiBase(raw)
  if (!check.ok) {
    connResult.value = 'fail'
    connError.value = check.error ?? 'API 地址无效'
    return
  }
  connBusy.value = true
  connResult.value = ''
  connError.value = ''
  try {
    await testApiBase(raw) // 先连通测试，成功才落盘（R3 P1-01）
    setApiBase(raw)
    connBase.value = getApiBase()
    connResult.value = 'ok'
  } catch (e) {
    connResult.value = 'fail'
    connError.value = e instanceof Error ? e.message : String(e)
  } finally {
    connBusy.value = false
  }
}

const timeText = (iso: string | null) =>
  iso ? new Date(iso).toLocaleString('zh-CN', { hour12: false }) : '—'

// EA 运行模式（phase-3 P2）：真实值来自 EA 心跳上报（run_mode 字段），
// 不再硬编码；显示随所选账户最新快照变化
const RUN_MODE_LABELS: Record<string, string> = {
  OBSERVE_ONLY: 'OBSERVE_ONLY（只读观察，不执行）',
  DRY_RUN: 'DRY_RUN（预检回执，不下单）',
  LIVE: 'LIVE（真实执行，仅限模拟账户）',
}
const runModeText = computed(() => {
  const mode = snap.value?.ea_run_mode
  if (!mode) return '—（等待 EA 心跳上报）'
  return RUN_MODE_LABELS[mode] ?? `${mode}（未知模式，请核对 EA 版本）`
})
</script>

<template>
  <div class="settings">
    <div class="qp-panel">
      <div class="qp-panel__title">
        系统信息
        <span class="qp-live-badge">真实数据</span>
      </div>
      <div class="settings__rows">
        <div class="settings__row"><span>EA 运行模式</span><b :class="snap?.ea_run_mode ? 'qp-up' : 'qp-dim'">{{ runModeText }}</b></div>
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
      </div>
      <div class="settings__conn">
        <el-button size="small" type="primary" :loading="connBusy" @click="saveConn">测试并保存</el-button>
        <el-tag v-if="connResult === 'ok'" type="success" size="small">连接成功，已保存</el-tag>
        <el-tag v-else-if="connResult === 'fail'" type="danger" size="small">失败：{{ connError }}</el-tag>
      </div>
      <div class="settings__note">
        <p>· 保存前自动连通测试（GET /healthz）；地址格式要求协议 + 主机 + 端口（如 http://127.0.0.1:8000）。</p>
        <p>· 登录使用用户名密码或个人 API Key（qpk_），均在登录页完成，凭据保存于系统凭据管理器。</p>
        <p>· 共享服务密钥仅用于 Agent / EA 机器通道，不能作为用户身份登录（R2 P1-01）。</p>
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
