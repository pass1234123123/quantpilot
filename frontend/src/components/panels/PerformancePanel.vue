<script setup lang="ts">
import { computed } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'
import { num } from '../../api'

const store = useDashboardStore()

const stats = computed(() => store.performance?.stats ?? null)
const currency = computed(() => store.snapshot?.currency ?? 'USD')
const pendingOrders = computed(() => store.orders.filter((o) => o.status === 'ACTIVE').length)

function money(v: string | null | undefined, signed = false): string {
  const n = num(v ?? null)
  if (n === null) return '—'
  const s = n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  return (signed && n > 0 ? '+' : '') + s
}

function pct(v: string | null | undefined): string {
  const n = num(v ?? null)
  return n === null ? '—' : (n * 100).toFixed(2) + '%'
}

function plain(v: string | null | undefined): string {
  const n = num(v ?? null)
  return n === null ? '—' : String(n)
}

const cells = computed(() => {
  if (!stats.value) return []
  const s = stats.value
  return [
    { label: '总交易次数', value: String(s.closed_trades) },
    { label: '胜率', value: pct(s.win_rate) },
    { label: '利润因子', value: plain(s.profit_factor) },
    { label: '恢复因子', value: plain(s.recovery_factor) },
    { label: '期望值', value: money(s.expectancy, true) + ' ' + currency.value },
    { label: '最大回撤', value: money(s.max_drawdown), tone: 'down', extra: '累计净值回落' },
    { label: '平均盈利', value: money(s.avg_win) + ' ' + currency.value, tone: 'up' },
    { label: '平均亏损', value: money(s.avg_loss) + ' ' + currency.value, tone: 'down' },
    { label: '连胜次数', value: String(s.max_consecutive_wins), tone: 'up' },
    { label: '连亏次数', value: String(s.max_consecutive_losses), tone: 'down' },
    { label: '未平仓订单', value: String(pendingOrders.value) },
    { label: '最佳交易', value: money(s.best_trade, true), tone: 'up' },
  ]
})
</script>

<template>
  <div class="qp-panel">
    <div class="qp-panel__title">
      绩效信息
      <span v-if="stats" class="qp-live-badge">EA 真实数据</span>
    </div>
    <div v-if="stats && stats.closed_trades > 0" class="perf-grid">
      <div v-for="s in cells" :key="s.label" class="perf-cell">
        <div class="perf-cell__label">{{ s.label }}</div>
        <div class="perf-cell__value" :class="s.tone === 'down' ? 'qp-down' : s.tone === 'up' ? 'qp-up' : ''">
          {{ s.value }}
        </div>
        <div v-if="s.extra" class="perf-cell__extra qp-down">{{ s.extra }}</div>
      </div>
    </div>
    <div v-else class="perf-empty">
      暂无平仓成交记录（EA 每 60 秒同步账户历史成交）
    </div>
  </div>
</template>

<style scoped>
.perf-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
  flex: 1;
}
.perf-cell {
  background: rgba(148, 163, 184, 0.05);
  border: 1px solid var(--qp-border-soft);
  border-radius: 8px;
  padding: 9px 10px;
  min-width: 0;
}
.perf-cell__label { font-size: 10px; color: var(--qp-text-dim); margin-bottom: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.perf-cell__value { font-size: 15px; font-weight: 700; font-variant-numeric: tabular-nums; }
.perf-cell__extra { font-size: 10.5px; margin-top: 2px; }
.perf-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--qp-text-dim);
  font-size: 12px;
}
</style>
