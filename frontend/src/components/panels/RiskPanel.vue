<script setup lang="ts">
import { ref } from 'vue'

const riskMode = ref('% of Balance')
const modes = ['% of Balance', '% of Equity', 'Fixed Lot Size']

// 示例数据：风控规则尚未接入后端（OBSERVE_ONLY 切片无交易执行）
const gaugePercent = 0.85
const gaugeAngle = 180 * (1 - Math.min(gaugePercent / 5, 1)) // 0..5% mapped to semicircle

const riskRows = [
  { label: 'Total Risk', value: '0.85%', status: 'ok' },
  { label: 'Risk Per Trade', value: '1.00%', status: 'ok' },
  { label: 'Max Daily Loss', value: '5.00%', status: 'ok' },
  { label: 'Max Drawdown', value: '10.00%', status: 'ok' },
  { label: 'Max Open Positions', value: '5', status: 'ok' },
  { label: 'Max Spread', value: '30 pts', status: 'ok' },
  { label: 'News Block', value: 'Enabled', status: 'warn' },
]
</script>

<template>
  <div class="qp-panel risk">
    <div class="qp-panel__title">
      Risk Management &amp; Status
      <span class="qp-sample-badge">示例数据</span>
    </div>

    <div class="risk__mode">
      <div
        v-for="m in modes"
        :key="m"
        class="risk__mode-item"
        :class="{ 'is-active': m === riskMode }"
        @click="riskMode = m"
      >
        {{ m }}
      </div>
    </div>

    <div class="risk__params qp-rows">
      <div class="qp-row"><span class="qp-row__label">Risk %</span><b class="qp-row__value">1.00</b></div>
      <div class="qp-row"><span class="qp-row__label">Lot Size</span><b class="qp-row__value">0.10</b></div>
      <div class="qp-row"><span class="qp-row__label">Max Per Trade</span><b class="qp-row__value">100.00</b></div>
    </div>

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
        <text x="100" y="78" text-anchor="middle" class="risk__gauge-value">0.85%</text>
        <text x="100" y="96" text-anchor="middle" class="risk__gauge-label">Total Risk</text>
      </svg>
      <div class="risk__gauge-state qp-up">LOW RISK</div>
    </div>

    <div class="qp-rows">
      <div v-for="row in riskRows" :key="row.label" class="qp-row">
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
.risk__params { margin-bottom: 6px; }
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
