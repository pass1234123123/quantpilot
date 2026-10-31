<script setup lang="ts">
import { computed } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'
import { num } from '../../api'

const store = useDashboardStore()

const config = computed(() => store.config)
const newsFilter = computed(() => config.value?.config.filters.news_filter)
const highImpactOnly = computed(() => config.value?.config.filters.high_impact_only)

const onOff = (v: boolean | undefined) => (v === undefined ? '—' : v ? '开启' : '关闭')

// EA 上报的 MT5 内置财经日历（48 小时窗口，真实经纪商数据）
const events = computed(() => store.calendar)

const importanceLabel: Record<string, string> = {
  NONE: '无',
  LOW: '低',
  MODERATE: '中',
  HIGH: '高',
}

const fmtVal = (v: string | null) => {
  if (v === null) return '—'
  const n = num(v)
  return n === null ? v : n.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

const timeText = (iso: string) =>
  new Date(iso).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })

const highCount = computed(() => events.value.filter((e) => e.importance === 'HIGH').length)
</script>

<template>
  <div class="qp-panel news">
    <div class="qp-panel__title">
      新闻过滤 · 财经日历
      <span class="qp-live-badge">配置+日历真实</span>
    </div>

    <div class="news__cfg">
      <div class="news__cfg-row">
        <span>新闻过滤</span>
        <b :class="newsFilter ? 'qp-up' : 'qp-dim'">{{ onOff(newsFilter) }}</b>
      </div>
      <div class="news__cfg-row">
        <span>仅高影响事件</span>
        <b :class="highImpactOnly ? 'qp-up' : 'qp-dim'">{{ onOff(highImpactOnly) }}</b>
      </div>
      <div class="news__cfg-row">
        <span>窗口内高影响事件</span>
        <b>{{ highCount }} 条</b>
      </div>
      <div class="news__cfg-row">
        <span>窗口</span>
        <b>过去 2 小时 ~ 未来 48 小时（UTC）</b>
      </div>
    </div>

    <div v-if="events.length === 0" class="news__empty">
      待上报 —— EA 每 10 分钟刷新一次 MT5 内置日历，云端暂无该账户的日历数据。
    </div>

    <table v-else class="news__table">
      <thead>
        <tr>
          <th>时间（本地）</th>
          <th>货币</th>
          <th>事件</th>
          <th>重要性</th>
          <th>前值</th>
          <th>预测</th>
          <th>实际</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="ev in events" :key="ev.event_uid" :class="{ 'is-high': ev.importance === 'HIGH' }">
          <td class="news__time">{{ timeText(ev.event_time) }}</td>
          <td class="news__ccy">{{ ev.currency || ev.country_code }}</td>
          <td>{{ ev.name }}</td>
          <td>
            <span class="news__imp" :class="`news__imp--${ev.importance.toLowerCase()}`">
              {{ importanceLabel[ev.importance] ?? ev.importance }}
            </span>
          </td>
          <td class="news__num">{{ fmtVal(ev.previous) }}</td>
          <td class="news__num">{{ fmtVal(ev.forecast) }}</td>
          <td class="news__num news__actual">{{ fmtVal(ev.actual) }}</td>
        </tr>
      </tbody>
    </table>

    <div class="news__foot">
      共 {{ events.length }} 条 · event_time 为 UTC · 实际值在事件发布后由 EA 后续刷新带入
    </div>
  </div>
</template>

<style scoped>
.news { display: flex; flex-direction: column; gap: 10px; }

.news__cfg {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
  padding: 10px;
  border: 1px solid var(--qp-border-soft);
  border-radius: 8px;
  background: rgba(0, 176, 192, 0.04);
}
.news__cfg-row { display: flex; flex-direction: column; gap: 3px; font-size: 11px; }
.news__cfg-row span { color: var(--qp-text-dim); font-size: 10px; }
.news__cfg-row b { font-weight: 600; }

.news__empty {
  padding: 26px 10px;
  text-align: center;
  font-size: 12px;
  color: var(--qp-text-dim);
}

.news__table { width: 100%; border-collapse: collapse; font-size: 11.5px; }
.news__table th {
  text-align: left;
  font-size: 10px;
  letter-spacing: 0.8px;
  color: var(--qp-text-dim);
  font-weight: 600;
  padding: 6px 10px;
  border-bottom: 1px solid var(--qp-border);
  white-space: nowrap;
}
.news__table td {
  padding: 6px 10px;
  border-bottom: 1px solid rgba(148, 163, 184, 0.08);
}
.news__table tr:hover td { background: rgba(148, 163, 184, 0.04); }
.news__table tr.is-high td { background: rgba(239, 68, 68, 0.045); }
.news__table tr.is-high:hover td { background: rgba(239, 68, 68, 0.08); }

.news__time { white-space: nowrap; font-variant-numeric: tabular-nums; }
.news__ccy { font-weight: 600; }
.news__num { font-variant-numeric: tabular-nums; color: var(--qp-text-dim); }
.news__actual { color: var(--qp-text); font-weight: 600; }

.news__imp {
  display: inline-block;
  padding: 1px 7px;
  border-radius: 4px;
  font-size: 9.5px;
  font-weight: 700;
  color: var(--qp-text);
  background: rgba(148, 163, 184, 0.18);
}
.news__imp--high { background: rgba(239, 68, 68, 0.25); }
.news__imp--moderate { background: rgba(245, 158, 11, 0.25); }

.news__foot {
  font-size: 10px;
  color: var(--qp-text-dim);
  border-top: 1px solid var(--qp-border-soft);
  padding-top: 8px;
}

@media (max-width: 1400px) {
  .news__cfg { grid-template-columns: repeat(2, 1fr); }
}
</style>
