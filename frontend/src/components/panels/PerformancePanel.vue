<script setup lang="ts">
// 示例数据：绩效统计需交易成交数据，本切片（OBSERVE_ONLY）尚无成交
const stats = [
  { label: '总交易次数', value: '128' },
  { label: '胜率', value: '67.19%' },
  { label: '利润因子', value: '2.31' },
  { label: '恢复因子', value: '3.85' },
  { label: '期望值', value: '18.74 USD' },
  { label: '最大回撤', value: '18.45%', tone: 'down', extra: '-$565.20' },
  { label: '平均盈利', value: '45.32 USD' },
  { label: '平均亏损', value: '21.85 USD' },
  { label: '连胜次数', value: '7', tone: 'up' },
  { label: '连亏次数', value: '2', tone: 'down' },
  { label: '未平仓订单', value: '4' },
  { label: '最佳交易', value: '+$128.40', tone: 'up' },
]
</script>

<template>
  <div class="qp-panel">
    <div class="qp-panel__title">
      绩效信息
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
