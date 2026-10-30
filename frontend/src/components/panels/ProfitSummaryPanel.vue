<script setup lang="ts">
// 示例数据：盈亏柱状图需成交数据，本切片（OBSERVE_ONLY）尚无成交
const bars = [
  { m: '20 May', profit: 900, loss: 200 },
  { m: '05 Jun', profit: 1250, loss: 350 },
  { m: '20 Jun', profit: 700, loss: 600 },
  { m: '05 Jul', profit: 1600, loss: 150 },
  { m: '20 Jul', profit: 1100, loss: 420 },
  { m: '05 Aug', profit: 1850, loss: 280 },
  { m: '20 Aug', profit: 600, loss: 750 },
  { m: '05 Sep', profit: 1400, loss: 190 },
  { m: '20 Sep', profit: 1700, loss: 330 },
  { m: '05 Oct', profit: 1000, loss: 520 },
  { m: '20 Oct', profit: 1950, loss: 110 },
]
const maxAbs = Math.max(...bars.flatMap((b) => [b.profit, b.loss]))
</script>

<template>
  <div class="qp-panel">
    <div class="qp-panel__title">
      Profit Monitor (Summary)
      <span class="qp-sample-badge">示例数据</span>
    </div>

    <div class="ps__chart">
      <div class="ps__zero" />
      <div v-for="b in bars" :key="b.m" class="ps__col">
        <div class="ps__pos">
          <div class="ps__bar ps__bar--profit" :style="{ height: (b.profit / maxAbs) * 100 + '%' }" />
        </div>
        <div class="ps__neg">
          <div class="ps__bar ps__bar--loss" :style="{ height: (b.loss / maxAbs) * 100 + '%' }" />
        </div>
        <div class="ps__label">{{ b.m }}</div>
      </div>
    </div>

    <div class="ps__legend">
      <span><i class="ps__dot ps__dot--profit" />Profit (USD)</span>
      <span><i class="ps__dot ps__dot--loss" />Loss (USD)</span>
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
</style>
