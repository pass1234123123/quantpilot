<script setup lang="ts">
// 示例数据：绩效统计需交易成交数据，本切片（OBSERVE_ONLY）尚无成交
const stats = [
  { label: 'Total Trades', value: '128' },
  { label: 'Win Rate', value: '67.19%' },
  { label: 'Profit Factor', value: '2.31' },
  { label: 'Recovery Factor', value: '3.85' },
  { label: 'Expectancy', value: '18.74 USD' },
  { label: 'Max Drawdown', value: '18.45%', tone: 'down', extra: '-$565.20' },
  { label: 'Average Profit', value: '45.32 USD' },
  { label: 'Average Loss', value: '21.85 USD' },
  { label: 'Consecutive Wins', value: '7', tone: 'up' },
  { label: 'Consecutive Losses', value: '2', tone: 'down' },
  { label: 'Open Trades', value: '4' },
  { label: 'Best Trade', value: '+$128.40', tone: 'up' },
]
</script>

<template>
  <div class="qp-panel">
    <div class="qp-panel__title">
      Performance Info
      <span class="qp-sample-badge">示例数据</span>
    </div>
    <div class="perf-grid">
      <div v-for="s in stats" :key="s.label" class="perf-cell">
        <div class="perf-cell__label">{{ s.label }}</div>
        <div class="perf-cell__value" :class="s.tone === 'down' ? 'qp-down' : s.tone === 'up' ? 'qp-up' : ''">
          {{ s.value }}
        </div>
        <div v-if="s.extra" class="perf-cell__extra qp-down">{{ s.extra }}</div>
      </div>
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
</style>
