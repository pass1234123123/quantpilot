<script setup lang="ts">
import { ref } from 'vue'

const tab = ref('Today')
const tabs = ['Today', 'Week', 'Month', 'Custom']

// 示例数据：盈利统计需成交数据，本切片（OBSERVE_ONLY）尚无成交
const rows = [
  { label: 'Today Profit', value: '+$285.75', pct: '+3.72%', up: true },
  { label: 'Weekly Profit', value: '+$1,256.30', pct: '+13.92%', up: true },
  { label: 'Monthly Profit', value: '+$3,875.60', pct: '+42.38%', up: true },
  { label: 'Net Profit', value: '+$7,650.25', pct: '', up: true },
  { label: 'Last Trade Result', value: '+$68.40', pct: '', up: true },
]
const meta = [
  { label: 'Session', value: 'New York' },
  { label: 'Consecutive Wins', value: '7' },
  { label: 'Consecutive Losses', value: '2' },
]
</script>

<template>
  <div class="qp-panel">
    <div class="qp-panel__title">
      Profit Monitor
      <span class="qp-sample-badge">示例数据</span>
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

    <div class="qp-rows">
      <div v-for="row in rows" :key="row.label" class="qp-row">
        <span class="qp-row__label">{{ row.label }}</span>
        <b class="qp-row__value" :class="row.up ? 'qp-up' : 'qp-down'">
          {{ row.value }}
          <span v-if="row.pct" class="pm__pct">{{ row.pct }}</span>
        </b>
      </div>
    </div>

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
.pm__pct { font-size: 10px; font-weight: 500; opacity: 0.85; }
.pm__meta { margin-top: 10px; }
</style>
