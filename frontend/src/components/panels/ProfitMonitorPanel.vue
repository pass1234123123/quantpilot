<script setup lang="ts">
import { computed, ref } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'
import { num } from '../../api'

const store = useDashboardStore()
const tab = ref('今日')
const tabs = ['今日', '本周', '本月', '全部']

const period = computed(() => store.performance?.period ?? null)
const stats = computed(() => store.performance?.stats ?? null)
const currency = computed(() => store.snapshot?.currency ?? 'USD')
const hasData = computed(() => (stats.value?.closed_trades ?? 0) > 0)

function money(v: string | null | undefined, signed = true): string {
  const n = num(v ?? null)
  if (n === null) return '—'
  const s = n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  return (signed && n > 0 ? '+' : '') + s
}

function selectedValue(): { label: string; value: string; up: boolean } {
  const p = period.value
  if (!p) return { label: '本期盈亏', value: '—', up: true }
  const map: Record<string, string> = {
    今日: p.today_net,
    本周: p.week_net,
    本月: p.month_net,
    全部: stats.value?.net_profit ?? '0',
  }
  const v = num(map[tab.value]) ?? 0
  return { label: `${tab.value}盈亏`, value: money(map[tab.value]), up: v >= 0 }
}

const row = computed(selectedValue)

const rows = computed(() => [
  { label: '今日盈亏', value: money(period.value?.today_net), up: (num(period.value?.today_net) ?? 0) >= 0 },
  { label: '本周盈亏', value: money(period.value?.week_net), up: (num(period.value?.week_net) ?? 0) >= 0 },
  { label: '本月盈亏', value: money(period.value?.month_net), up: (num(period.value?.month_net) ?? 0) >= 0 },
  { label: '净利润', value: money(stats.value?.net_profit), up: (num(stats.value?.net_profit) ?? 0) >= 0 },
  {
    label: '最后一笔结果',
    value: money(period.value?.last_trade_net),
    up: (num(period.value?.last_trade_net) ?? 0) >= 0,
  },
])

const meta = computed(() => [
  { label: '总成交笔数', value: String(stats.value?.all_deals ?? 0) },
  { label: '连胜次数', value: String(stats.value?.max_consecutive_wins ?? 0) },
  { label: '连亏次数', value: String(stats.value?.max_consecutive_losses ?? 0) },
])
</script>

<template>
  <div class="qp-panel">
    <div class="qp-panel__title">
      盈亏监控
      <span v-if="hasData" class="qp-live-badge">EA 真实数据</span>
    </div>

    <div class="pm__tabs">
      <div
        v-for="t in tabs"
        :key="t"
        class="pm__tab"
        :class="{ 'is-active': t === tab }"
        @click="tab = t"
      >
        {{ t }}
      </div>
    </div>

    <div class="pm__hero">
      <div class="pm__hero-label">{{ row.label }}（{{ currency }}）</div>
      <div class="pm__hero-value" :class="row.up ? 'qp-up' : 'qp-down'">{{ row.value }}</div>
    </div>

    <div v-if="hasData" class="qp-rows">
      <div v-for="r in rows" :key="r.label" class="qp-row">
        <span class="qp-row__label">{{ r.label }}</span>
        <b class="qp-row__value" :class="r.up ? 'qp-up' : 'qp-down'">{{ r.value }}</b>
      </div>
    </div>
    <div v-else class="pm__empty">暂无平仓成交记录（EA 每 60 秒同步账户历史成交）</div>

    <div class="qp-rows pm__meta">
      <div v-for="m in meta" :key="m.label" class="qp-row">
        <span class="qp-row__label">{{ m.label }}</span><b class="qp-row__value">{{ m.value }}</b>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pm__tabs { display: flex; gap: 6px; margin-bottom: 10px; }
.pm__tab {
  flex: 1;
  text-align: center;
  font-size: 11px;
  padding: 4px 0;
  border-radius: 6px;
  border: 1px solid var(--qp-border-soft);
  color: var(--qp-text-dim);
  cursor: pointer;
}
.pm__tab.is-active { color: var(--qp-accent); border-color: var(--qp-accent); background: var(--qp-accent-soft); font-weight: 600; }
.pm__hero {
  text-align: center;
  padding: 10px 0 12px;
  border: 1px solid var(--qp-border-soft);
  border-radius: 8px;
  margin-bottom: 10px;
  background: rgba(148, 163, 184, 0.05);
}
.pm__hero-label { font-size: 10.5px; color: var(--qp-text-dim); margin-bottom: 4px; }
.pm__hero-value { font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; }
.pm__empty { text-align: center; color: var(--qp-text-dim); font-size: 12px; padding: 14px 0; }
.pm__meta { margin-top: 10px; }
</style>
