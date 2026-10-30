<script setup lang="ts">
import { ref } from 'vue'

// 示例数据：过滤器配置链路（云端→EA）尚未实现，仅展示目标形态
const switches = ref({
  newsFilter: true,
  highImpactOnly: true,
  sessionFilter: false,
  maxSpread: true,
  slippageGuard: true,
  weekendBlock: false,
})
const limits = [
  { label: 'Max Orders Per Day', value: '10' },
  { label: 'Max Open Positions', value: '5' },
  { label: 'Trade Direction', value: 'Both' },
  { label: 'Allowed Symbols', value: '28' },
  { label: 'Blocked Symbols', value: '3' },
]
</script>

<template>
  <div class="qp-panel">
    <div class="qp-panel__title">
      Filtering Settings
      <span class="qp-sample-badge">示例数据</span>
    </div>

    <div class="fs__section">Environmental Filters</div>
    <div class="fs__switches">
      <div v-for="(_on, key) in switches" :key="key" class="fs__switch">
        <span>{{ { newsFilter: 'News', highImpactOnly: 'High Impact', sessionFilter: 'Session', maxSpread: 'Max Spread', slippageGuard: 'Slippage Guard', weekendBlock: 'Weekend Block' }[key] }}</span>
        <el-switch v-model="switches[key as keyof typeof switches]" size="small" />
      </div>
    </div>

    <div class="fs__section">Operational Limits</div>
    <div class="qp-rows">
      <div v-for="l in limits" :key="l.label" class="qp-row">
        <span class="qp-row__label">{{ l.label }}</span><b class="qp-row__value">{{ l.value }}</b>
      </div>
    </div>
  </div>
</template>

<style scoped>
.fs__section {
  font-size: 10px;
  letter-spacing: 1px;
  color: var(--qp-text-dim);
  text-transform: uppercase;
  margin: 4px 0 8px;
}
.fs__switches {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px 14px;
  margin-bottom: 12px;
}
.fs__switch {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 12px;
  color: var(--qp-text-dim);
  padding: 2px 0;
}
</style>
