<script setup lang="ts">
import { computed } from 'vue'
import { useDashboardStore } from '../stores/dashboard'
import { num } from '../api'

const store = useDashboardStore()

const snap = computed(() => store.snapshot)

function fmtMoney(value: string | null | undefined, currency = ''): string {
  const n = num(value)
  if (n === null) return '—'
  const sign = n > 0 ? '' : ''
  return `${sign}${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${currency ? ' ' + currency : ''}`
}

const floating = computed(() => store.floatingPl)
const floatingPct = computed(() => {
  const f = floating.value
  const balance = num(snap.value?.balance)
  if (f === null || balance === null || balance === 0) return null
  return (f / balance) * 100
})

const marginLevel = computed(() => store.marginLevel)

const serverTime = computed(() => {
  if (!snap.value?.captured_at) return '—'
  const d = new Date(snap.value.captured_at)
  return d.toLocaleString('en-GB', { hour12: false })
})

const cards = computed(() => [
  {
    label: 'Account Balance',
    value: fmtMoney(snap.value?.balance),
    sub: snap.value?.currency ?? '',
    tone: '',
  },
  {
    label: 'Equity',
    value: fmtMoney(snap.value?.equity),
    sub: snap.value?.currency ?? '',
    tone: '',
  },
  {
    label: 'Floating P/L',
    value:
      floating.value === null
        ? '—'
        : `${floating.value >= 0 ? '+' : ''}${floating.value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`,
    sub:
      floatingPct.value === null
        ? ''
        : `${floatingPct.value >= 0 ? '+' : ''}${floatingPct.value.toFixed(2)}%`,
    tone: (floating.value ?? 0) >= 0 ? 'up' : 'down',
  },
  { label: 'Used Margin', value: fmtMoney(snap.value?.margin), sub: snap.value?.currency ?? '', tone: '' },
  { label: 'Free Margin', value: fmtMoney(snap.value?.free_margin), sub: snap.value?.currency ?? '', tone: '' },
  {
    label: 'Margin Level',
    value: marginLevel.value === null ? '—' : `${marginLevel.value.toFixed(2)}%`,
    sub: marginLevel.value === null ? 'No Margin Used' : marginLevel.value >= 500 ? 'Safe' : marginLevel.value >= 200 ? 'Moderate' : 'Low',
    tone: marginLevel.value === null ? '' : marginLevel.value >= 200 ? 'up' : 'down',
  },
  {
    label: 'Server Time',
    value: serverTime.value,
    sub: snap.value?.connected ? 'CONNECTED' : 'OFFLINE',
    tone: snap.value?.connected ? 'up' : 'down',
  },
])
</script>

<template>
  <div class="kpi-band">
    <div v-for="card in cards" :key="card.label" class="kpi-card">
      <div class="kpi-card__label">{{ card.label }}</div>
      <div class="kpi-card__value" :class="`tone-${card.tone}`">{{ card.value }}</div>
      <div class="kpi-card__sub" :class="`tone-${card.tone}`">{{ card.sub }}</div>
    </div>
  </div>
</template>

<style scoped>
.kpi-band {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 10px;
  margin-bottom: 12px;
}

.kpi-card {
  background: var(--qp-panel);
  border: 1px solid var(--qp-border);
  border-radius: 10px;
  padding: 12px 14px;
  min-width: 0;
}

.kpi-card__label {
  font-size: 10.5px;
  letter-spacing: 0.6px;
  color: var(--qp-text-dim);
  text-transform: uppercase;
  margin-bottom: 6px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.kpi-card__value {
  font-size: 19px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.kpi-card__sub {
  font-size: 11px;
  color: var(--qp-text-dim);
  margin-top: 3px;
  min-height: 14px;
}

.tone-up { color: var(--qp-up); }
.tone-down { color: var(--qp-down); }

@media (max-width: 1500px) {
  .kpi-band { grid-template-columns: repeat(4, 1fr); }
}
</style>
