<script setup lang="ts">
import { computed } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'
import { num } from '../../api'

const store = useDashboardStore()

const currency = computed(() => store.snapshot?.currency ?? 'USD')
const hasSnapshot = computed(() => store.snapshot !== null)

// 保证金占用率 = 已用保证金 / 净权益（0..1）
const marginUsage = computed(() => {
  const s = store.snapshot
  if (!s) return null
  const equity = num(s.equity)
  const margin = num(s.margin)
  if (equity === null || margin === null || equity <= 0) return null
  return Math.min(margin / equity, 1)
})

const gaugePercent = computed(() => (marginUsage.value === null ? null : marginUsage.value * 100))
const gaugeAngle = computed(() => 180 * (1 - Math.min(marginUsage.value ?? 0, 1)))

// 当前回撤：快照历史（最近 500 条）内净值峰值 -> 当前净值
const currentDrawdown = computed(() => {
  const s = store.snapshot
  if (!s) return null
  const equity = num(s.equity)
  if (equity === null || equity <= 0) return null
  const peaks = store.snapshotHistory
    .map((p) => num(p.equity))
    .filter((v): v is number => v !== null && v > 0)
  if (peaks.length === 0) return null
  const peak = Math.max(...peaks, equity)
  return Math.max((peak - equity) / peak, 0)
})

const openPositions = computed(() => store.positions.filter((p) => p.status === 'OPEN'))
const activeOrders = computed(() => store.orders.filter((o) => o.status === 'ACTIVE'))
const totalLots = computed(() => openPositions.value.reduce((acc, p) => acc + (num(p.volume) ?? 0), 0))

function money(v: number | null, signed = false): string {
  if (v === null) return '—'
  const s = v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  return (signed && v > 0 ? '+' : '') + s
}

function pct(v: number | null): string {
  return v === null ? '—' : (v * 100).toFixed(2) + '%'
}

const riskState = computed(() => {
  const u = marginUsage.value
  if (u === null) return { label: '—', cls: '' }
  if (u < 0.3) return { label: '低风险', cls: 'qp-up' }
  if (u < 0.7) return { label: '中风险', cls: 'qp-warn' }
  return { label: '高风险', cls: 'qp-down' }
})

const rows = computed(() => {
  const s = store.snapshot
  const perf = store.performance
  return [
    { label: '浮动盈亏', value: `${money(store.floatingPl, true)} ${currency.value}`, status: (store.floatingPl ?? 0) >= 0 ? 'ok' : 'warn' },
    { label: '保证金水平', value: store.marginLevel === null ? '—' : store.marginLevel.toFixed(2) + '%', status: (store.marginLevel ?? 999) >= 200 ? 'ok' : 'warn' },
    { label: '今日盈亏', value: `${money(num(perf?.period.today_net) ?? null, true)} ${currency.value}`, status: (num(perf?.period.today_net) ?? 0) >= 0 ? 'ok' : 'warn' },
    { label: '当前回撤(峰值)', value: pct(currentDrawdown.value), status: (currentDrawdown.value ?? 0) < 0.1 ? 'ok' : 'warn' },
    { label: '历史最大回撤', value: money(num(perf?.stats.max_drawdown) ?? null), status: 'ok' },
    { label: '持仓数 / 挂单数', value: `${openPositions.value.length} / ${activeOrders.value.length}`, status: 'ok' },
    { label: '总持仓手数', value: totalLots.value.toFixed(2), status: 'ok' },
    { label: '净值 / 结余', value: `${money(num(s?.equity) ?? null)} / ${money(num(s?.balance) ?? null)}`, status: 'ok' },
  ]
})
</script>

<template>
  <div class="qp-panel risk">
    <div class="qp-panel__title">
      风险管理与状态
      <span v-if="hasSnapshot" class="qp-live-badge">真实观测</span>
    </div>

    <div class="risk__note">OBSERVE_ONLY：以下为账户实时观测值（不触发任何交易动作）</div>

    <div class="risk__gauge">
      <svg viewBox="0 0 200 110" class="risk__gauge-svg">
        <defs>
          <linearGradient id="gaugeGrad" x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" stop-color="#22c55e" />
            <stop offset="60%" stop-color="#f59e0b" />
            <stop offset="100%" stop-color="#ef4444" />
          </linearGradient>
        </defs>
        <path d="M 15 100 A 85 85 0 0 1 185 100" fill="none" stroke="rgba(148,163,184,0.15)" stroke-width="13" stroke-linecap="round" />
        <path
          d="M 15 100 A 85 85 0 0 1 185 100"
          fill="none"
          stroke="url(#gaugeGrad)"
          stroke-width="13"
          stroke-linecap="round"
          :stroke-dasharray="`${(gaugeAngle / 180) * 267} 267`"
        />
        <text x="100" y="78" text-anchor="middle" class="risk__gauge-value">
          {{ gaugePercent === null ? '—' : gaugePercent.toFixed(2) + '%' }}
        </text>
        <text x="100" y="96" text-anchor="middle" class="risk__gauge-label">保证金占用率</text>
      </svg>
      <div class="risk__gauge-state" :class="riskState.cls">{{ riskState.label }}</div>
    </div>

    <div class="qp-rows">
      <div v-for="row in rows" :key="row.label" class="qp-row">
        <span class="qp-row__label">{{ row.label }}</span>
        <b class="qp-row__value">
          <span
            class="risk__dot"
            :class="row.status === 'ok' ? 'is-ok' : 'is-warn'"
          />{{ row.value }}
        </b>
      </div>
    </div>
  </div>
</template>

<style scoped>
.risk__mode {
  display: flex;
  gap: 6px;
  margin-bottom: 10px;
}
.risk__mode-item {
  flex: 1;
  text-align: center;
  font-size: 10.5px;
  padding: 5px 2px;
  border-radius: 6px;
  border: 1px solid var(--qp-border-soft);
  color: var(--qp-text-dim);
  cursor: pointer;
}
.risk__mode-item.is-active {
  color: var(--qp-accent);
  border-color: var(--qp-accent);
  background: var(--qp-accent-soft);
  font-weight: 600;
}
.risk__note {
  font-size: 10px;
  color: var(--qp-text-dim);
  margin-bottom: 6px;
}
.risk__gauge { text-align: center; margin: 4px 0 10px; }
.risk__gauge-svg { width: 170px; height: auto; }
.risk__gauge-value { fill: var(--qp-text); font-size: 22px; font-weight: 700; }
.risk__gauge-label { fill: var(--qp-text-dim); font-size: 9px; letter-spacing: 1px; }
.risk__gauge-state { font-size: 11px; font-weight: 700; letter-spacing: 2px; }
.risk__dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  margin-right: 6px;
  vertical-align: 1px;
}
.risk__dot.is-ok { background: var(--qp-up); }
.risk__dot.is-warn { background: var(--qp-warn); }
</style>
