<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { createChart, LineSeries, type IChartApi, type UTCTimestamp } from 'lightweight-charts'
import { useDashboardStore } from '../../stores/dashboard'
import { num } from '../../api'

const store = useDashboardStore()
const el = ref<HTMLElement | null>(null)
let chart: IChartApi | null = null
let balanceSeries: ReturnType<IChartApi['addSeries']> | null = null
let equitySeries: ReturnType<IChartApi['addSeries']> | null = null

function toTime(iso: string): UTCTimestamp {
  return Math.floor(new Date(iso).getTime() / 1000) as UTCTimestamp
}

function render() {
  if (!chart || !balanceSeries || !equitySeries) return
  const points = store.snapshotHistory
  if (points.length === 0) {
    balanceSeries.setData([])
    equitySeries.setData([])
    return
  }
  const balance = points
    .map((p) => ({ time: toTime(p.captured_at), value: num(p.balance) ?? 0 }))
    .filter((p, i, arr) => i === 0 || p.time > arr[i - 1].time) // dedupe same-second timestamps
  const equity = points
    .map((p) => ({ time: toTime(p.captured_at), value: num(p.equity) ?? 0 }))
    .filter((p, i, arr) => i === 0 || p.time > arr[i - 1].time)
  balanceSeries.setData(balance)
  equitySeries.setData(equity)
  chart.timeScale().fitContent()
}

onMounted(() => {
  if (!el.value) return
  chart = createChart(el.value, {
    height: 240,
    layout: {
      background: { color: 'transparent' },
      textColor: '#7d8fa3',
      fontSize: 10,
    },
    grid: {
      vertLines: { color: 'rgba(148, 163, 184, 0.08)' },
      horzLines: { color: 'rgba(148, 163, 184, 0.08)' },
    },
    rightPriceScale: { borderColor: 'rgba(148, 163, 184, 0.2)' },
    timeScale: { borderColor: 'rgba(148, 163, 184, 0.2)', timeVisible: true },
    autoSize: false,
  })
  balanceSeries = chart.addSeries(LineSeries, { color: '#00b0c0', lineWidth: 2, title: 'Balance' })
  equitySeries = chart.addSeries(LineSeries, { color: '#22c55e', lineWidth: 2, title: 'Equity' })
  render()
})

watch(
  () => store.snapshotHistory,
  () => render(),
  { deep: false },
)

onBeforeUnmount(() => {
  chart?.remove()
  chart = null
})

const stats = [
  // 真实数据：由快照历史计算
  { label: 'Snapshots', key: 'count' },
  { label: 'Since', key: 'first' },
  { label: 'Latest Equity', key: 'last' },
] as const
</script>

<template>
  <div class="qp-panel">
    <div class="qp-panel__title">
      Equity &amp; Balance Graph
      <span class="qp-live-badge">真实数据</span>
    </div>

    <div ref="el" class="eq__chart" />

    <div class="eq__stats">
      <div class="eq__stat">
        <div class="eq__stat-label">{{ stats[0].label }}</div>
        <div class="eq__stat-value">{{ store.snapshotHistory.length }}</div>
      </div>
      <div class="eq__stat">
        <div class="eq__stat-label">{{ stats[1].label }}</div>
        <div class="eq__stat-value">
          {{ store.snapshotHistory[0]?.captured_at?.slice(0, 16).replace('T', ' ') || '—' }}
        </div>
      </div>
      <div class="eq__stat">
        <div class="eq__stat-label">{{ stats[2].label }}</div>
        <div class="eq__stat-value">{{ store.snapshotHistory.at(-1)?.equity ?? '—' }}</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.eq__chart { width: 100%; }
.eq__stats {
  display: flex;
  gap: 10px;
  margin-top: 10px;
}
.eq__stat {
  flex: 1;
  background: rgba(148, 163, 184, 0.05);
  border: 1px solid var(--qp-border-soft);
  border-radius: 8px;
  padding: 8px 10px;
}
.eq__stat-label { font-size: 10px; color: var(--qp-text-dim); margin-bottom: 3px; }
.eq__stat-value { font-size: 14px; font-weight: 700; font-variant-numeric: tabular-nums; }
</style>
