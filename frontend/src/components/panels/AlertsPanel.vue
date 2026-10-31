<script setup lang="ts">
import { computed } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'
import type { SystemEventRow } from '../../api'

const store = useDashboardStore()

const events = computed(() => store.events)

const severityLabel: Record<string, string> = {
  INFO: '信息',
  WARN: '警告',
  ERROR: '错误',
}
const severityClass = (s: string) =>
  s === 'WARN' ? 'is-warn' : s === 'ERROR' ? 'is-error' : 'is-info'

const typeLabel: Record<string, string> = {
  'ea.registered': 'EA 注册上线',
  'ea.shutdown': 'EA 停止运行',
  IDENTITY_MISMATCH: '终端身份变更',
}

// 已知事件类型给出人话摘要，未知类型退化为 JSON（不发明数据）
function detail(e: SystemEventRow): string {
  const p = e.payload ?? {}
  switch (e.event_type) {
    case 'ea.registered':
      return `EA 版本 ${String(p.ea_version ?? '—')} · 终端 build ${String(p.terminal_build ?? '—')}`
    case 'ea.shutdown':
      return `原因：${String(p.reason ?? '—')} · EA 版本 ${String(p.ea_version ?? '—')}`
    case 'IDENTITY_MISMATCH':
      return `登录号 ${String(p.previous_login_ref ?? '—')} → ${String(p.new_login_ref ?? '—')}（OBSERVE_ONLY：仅记录，不干预）`
    default:
      return Object.keys(p).length > 0 ? JSON.stringify(p) : '—'
  }
}

const timeText = (iso: string) =>
  new Date(iso).toLocaleString('zh-CN', { hour12: false })
</script>

<template>
  <div class="qp-panel alerts">
    <div class="qp-panel__title">
      系统告警 · 事件流
      <span class="qp-live-badge">system_events 真实数据</span>
    </div>

    <div v-if="events.length === 0" class="alerts__empty">
      暂无事件 —— EA 注册 / 停止 / 身份变更等系统事件将在发生时出现在这里（最近 100 条）。
    </div>

    <table v-else class="alerts__table">
      <thead>
        <tr>
          <th>时间（本地）</th>
          <th>级别</th>
          <th>事件</th>
          <th>来源</th>
          <th>详情</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="e in events" :key="e.event_id">
          <td class="alerts__time">{{ timeText(e.occurred_at) }}</td>
          <td>
            <span class="alerts__sev" :class="severityClass(e.severity)">
              {{ severityLabel[e.severity] ?? e.severity }}
            </span>
          </td>
          <td>{{ typeLabel[e.event_type] ?? e.event_type }}</td>
          <td class="alerts__dim">{{ e.source }}</td>
          <td class="alerts__dim">{{ detail(e) }}</td>
        </tr>
      </tbody>
    </table>

    <div class="alerts__foot">
      共 {{ events.length }} 条 · 按 occurred_at 倒序 · 每 30 秒随仪表盘刷新
    </div>
  </div>
</template>

<style scoped>
.alerts { display: flex; flex-direction: column; gap: 10px; }

.alerts__empty {
  padding: 26px 10px;
  text-align: center;
  font-size: 12px;
  color: var(--qp-text-dim);
}

.alerts__table { width: 100%; border-collapse: collapse; font-size: 11.5px; }
.alerts__table th {
  text-align: left;
  font-size: 10px;
  letter-spacing: 0.8px;
  color: var(--qp-text-dim);
  font-weight: 600;
  padding: 6px 10px;
  border-bottom: 1px solid var(--qp-border);
  white-space: nowrap;
}
.alerts__table td {
  padding: 7px 10px;
  border-bottom: 1px solid rgba(148, 163, 184, 0.08);
  vertical-align: top;
}
.alerts__table tr:hover td { background: rgba(148, 163, 184, 0.04); }

.alerts__time { white-space: nowrap; font-variant-numeric: tabular-nums; }
.alerts__dim { color: var(--qp-text-dim); }

.alerts__sev {
  display: inline-block;
  padding: 1px 7px;
  border-radius: 4px;
  font-size: 9.5px;
  font-weight: 700;
}
.alerts__sev.is-info {
  color: #38bdf8;
  background: rgba(56, 189, 248, 0.14);
}
.alerts__sev.is-warn {
  color: var(--qp-warn);
  background: rgba(245, 158, 11, 0.16);
}
.alerts__sev.is-error {
  color: var(--qp-down);
  background: rgba(239, 68, 68, 0.16);
}

.alerts__foot {
  font-size: 10px;
  color: var(--qp-text-dim);
  border-top: 1px solid var(--qp-border-soft);
  padding-top: 8px;
}
</style>
