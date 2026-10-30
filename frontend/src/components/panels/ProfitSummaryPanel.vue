<script setup lang="ts">
import { computed } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'
import { num } from '../../api'

const store = useDashboardStore()

// 最近 14 个交易日的日盈亏柱状图（按平仓成交聚合）
const bars = computed(() => {
  const daily = store.performance?.daily ?? []
  return daily.slice(-14).map((b) => {
    const d = new Date(b.date + 'T00:00:00Z')
    const label = `${d.getUTCMonth() + 1}/${d.getUTCDate()}`
    return {
      m: label,
      profit: num(b.profit) ?? 0,
      loss: num(b.loss) ?? 0,
      trades: b.trades,
    }
  })
})

const hasData = computed(() => bars.value.some((b) => b.trades > 0))
const maxAbs = computed(() => {
  const values = bars.value.flatMap((b) => [b.profit, b.loss])
  return values.length > 0 ? Math.max(...values, 1) : 1
})
</script>

<template>
  <div class="qp-panel">
    <div class="qp-panel__title">
      盈亏汇总
      <span v-if="hasData" class="qp-live-badge">EA 真实数据</span>
    </div>

    <div v-if="hasData" class="ps__chart">
      <div class="ps__zero" />
      <div v-for="b in bars" :key="b.m" class="ps__col" :title="`${b.m}：${b.trades} 笔平仓`">
        <div class="ps__pos">
          <div v-if="b.profit > 0" class="ps__bar ps__bar--profit" :style="{ height: (b.profit / maxAbs) * 100 + '%' }" />
        </div>
        <div class="ps__neg">
          <div v-if="b.loss > 0" class="ps__bar ps__bar--loss" :style="{ height: (b.loss / maxAbs) * 100 + '%' }" />
        </div>
        <div class="ps__label">{{ b.m }}</div>
      </div>
    </div>
    <div v-else class="ps__empty">暂无平仓成交记录（EA 每 60 秒同步账户历史成交）</div>

    <div class="ps__legend">
      <span><i class="ps__dot ps__dot--profit" />盈利 (USD)</span>
      <span><i class="ps__dot ps__dot--loss" />亏损 (USD)</span>
    </div>
  </div>
</template>

<style scoped>
.ps__chart {
  flex: 1;
  display: flex;
  gap: 5px;
  min-height: 150px;
  position: relative;
  padding-top: 6px;
}
.ps__zero {
  position: absolute;
  left: 0;
  right: 0;
  top: 50%;
  border-top: 1px dashed rgba(148, 163, 184, 0.3);
}
.ps__col { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.ps__pos { flex: 1; display: flex; align-items: flex-end; justify-content: center; }
.ps__neg { flex: 1; display: flex; align-items: flex-start; justify-content: center; }
.ps__bar { width: 55%; border-radius: 2px 2px 0 0; }
.ps__bar--profit { background: linear-gradient(180deg, #00d0e0, #007082); }
.ps__bar--loss { background: linear-gradient(180deg, #ef4444, #7f2b2b); border-radius: 0 0 2px 2px; }
.ps__label { font-size: 8.5px; color: var(--qp-text-dim); text-align: center; margin-top: 4px; white-space: nowrap; }
.ps__legend { display: flex; gap: 14px; justify-content: center; font-size: 11px; color: var(--qp-text-dim); margin-top: 10px; }
.ps__dot { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 5px; vertical-align: -1px; }
.ps__dot--profit { background: #00d0e0; }
.ps__dot--loss { background: #ef4444; }
.ps__empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--qp-text-dim);
  font-size: 12px;
  min-height: 150px;
}
</style>
