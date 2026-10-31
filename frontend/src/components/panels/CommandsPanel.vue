<script setup lang="ts">
import { computed } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'
import type { CommandRow } from '../../api'

const store = useDashboardStore()

const commands = computed(() => store.commands)
const decisions = computed(() => store.decisions)

const timeText = (iso: string) =>
  new Date(iso).toLocaleString('zh-CN', { hour12: false })

const statusClass = (s: string) =>
  s === 'ACKED' ? 'is-acked'
    : s === 'REJECTED' ? 'is-rejected'
    : s === 'EXPIRED' || s === 'UNKNOWN' ? 'is-stale'
    : 'is-queued'

// payload 摘要：不发明数据，仅提取已知键
function payloadText(p: CommandRow['payload']): string {
  const parts: string[] = []
  if (p.broker_symbol) parts.push(String(p.broker_symbol))
  if (p.direction) parts.push(String(p.direction))
  if (p.volume) parts.push(`${String(p.volume)} 手`)
  return parts.join(' · ') || '—'
}

const decisionClass = (d: string) => (d === 'ALLOW' ? 'is-acked' : 'is-rejected')
</script>

<template>
  <div class="qp-panel cm">
    <div class="qp-panel__title">
      命令通道 · 状态机与风控审计
      <span class="qp-live-badge">commands / risk_decisions 真实数据</span>
    </div>

    <div class="cm__section">命令流（最近 100 条）</div>
    <div v-if="commands.length === 0" class="cm__empty">暂无命令</div>
    <table v-else class="cm__table">
      <thead>
        <tr>
          <th>创建时间（本地）</th>
          <th>类型</th>
          <th>内容</th>
          <th>来源</th>
          <th>状态</th>
          <th>回执</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="c in commands" :key="c.command_id">
          <td class="cm__dim">{{ timeText(c.created_at) }}</td>
          <td>{{ c.command_type }}</td>
          <td>{{ payloadText(c.payload) }}</td>
          <td class="cm__dim">{{ c.created_by }}</td>
          <td>
            <span class="cm__status" :class="statusClass(c.status)">{{ c.status }}</span>
          </td>
          <td class="cm__dim">{{ c.ack_code ?? '—' }}</td>
        </tr>
      </tbody>
    </table>

    <div class="cm__section">风控闸门决定（最近 100 条）</div>
    <div v-if="decisions.length === 0" class="cm__empty">暂无风控决定</div>
    <table v-else class="cm__table">
      <thead>
        <tr>
          <th>决定时间（本地）</th>
          <th>决定</th>
          <th>原因码</th>
          <th>配置版本</th>
          <th>命令</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="d in decisions" :key="d.id">
          <td class="cm__dim">{{ timeText(d.decided_at) }}</td>
          <td>
            <span class="cm__status" :class="decisionClass(d.decision)">{{ d.decision }}</span>
          </td>
          <td class="cm__dim">{{ (d.reason_codes ?? []).join(' · ') || '—' }}</td>
          <td>v{{ d.config_version }}</td>
          <td class="cm__dim">{{ d.command_id ? d.command_id.slice(0, 8) + '…' : '—' }}</td>
        </tr>
      </tbody>
    </table>

    <div class="cm__foot">
      EA 按自身运行模式回执命令：OBSERVE_ONLY 仅回执；DRY_RUN 预检（不下单）；LIVE 真实执行（仅限模拟账户，真实账户硬性拒绝）。当前模式见「设置」页 EA 运行模式；REJECTED 永不投递
    </div>
  </div>
</template>

<style scoped>
.cm { display: flex; flex-direction: column; gap: 10px; }

.cm__section {
  font-size: 10px;
  letter-spacing: 1px;
  color: var(--qp-text-dim);
  text-transform: uppercase;
}

.cm__empty {
  padding: 18px 10px;
  text-align: center;
  font-size: 12px;
  color: var(--qp-text-dim);
}

.cm__table { width: 100%; border-collapse: collapse; font-size: 11.5px; }
.cm__table th {
  text-align: left;
  font-size: 10px;
  letter-spacing: 0.8px;
  color: var(--qp-text-dim);
  font-weight: 600;
  padding: 6px 10px;
  border-bottom: 1px solid var(--qp-border);
  white-space: nowrap;
}
.cm__table td {
  padding: 7px 10px;
  border-bottom: 1px solid rgba(148, 163, 184, 0.08);
  vertical-align: top;
}
.cm__table tr:hover td { background: rgba(148, 163, 184, 0.04); }
.cm__dim { color: var(--qp-text-dim); white-space: nowrap; font-variant-numeric: tabular-nums; }

.cm__status {
  display: inline-block;
  padding: 1px 7px;
  border-radius: 4px;
  font-size: 9.5px;
  font-weight: 700;
}
.cm__status.is-acked { color: var(--qp-up); background: rgba(34, 197, 94, 0.14); }
.cm__status.is-rejected { color: var(--qp-down); background: rgba(239, 68, 68, 0.16); }
.cm__status.is-stale { color: var(--qp-warn); background: rgba(245, 158, 11, 0.16); }
.cm__status.is-queued { color: #38bdf8; background: rgba(56, 189, 248, 0.14); }

.cm__foot {
  font-size: 10px;
  color: var(--qp-text-dim);
  border-top: 1px solid var(--qp-border-soft);
  padding-top: 8px;
}
</style>
