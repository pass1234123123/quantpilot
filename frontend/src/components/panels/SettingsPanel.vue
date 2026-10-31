<script setup lang="ts">
import { computed } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'

const store = useDashboardStore()

// 真实环境信息：来自 nodes / terminals / snapshot 上报
const node = computed(() => store.nodes[0] ?? null)
const terminal = computed(() =>
  store.terminals.find((t) => t.node_id === node.value?.node_id) ?? store.terminals[0] ?? null,
)
const account = computed(() => store.selectedAccount)
const snap = computed(() => store.snapshot)

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

.settings__note { display: flex; flex-direction: column; gap: 8px; }
.settings__note p {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.7;
  color: var(--qp-text-dim);
}
.settings__note b { color: var(--qp-text); font-weight: 600; }
</style>
