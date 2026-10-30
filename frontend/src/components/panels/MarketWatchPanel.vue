<script setup lang="ts">
import { computed } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'

const store = useDashboardStore()

// 真实数据：GET /api/v1/accounts/{id}/symbols（EA 品种发现上报）
const rows = computed(() => store.symbols)
const visibleCount = computed(() => rows.value.filter((r) => r.visible).length)
const tradableCount = computed(() => rows.value.filter((r) => r.tradable).length)
const selectedCount = computed(() => rows.value.filter((r) => r.selected).length)
</script>

<template>
  <div class="qp-panel mw">
    <div class="qp-panel__title">
      Market Watch
      <span class="qp-live-badge">真实数据</span>
    </div>

    <div class="mw__summary">
      <span>Total <b>{{ rows.length }}</b></span>
      <span>Visible <b>{{ visibleCount }}</b></span>
      <span>Tradable <b>{{ tradableCount }}</b></span>
      <span>Selected <b>{{ selectedCount }}</b></span>
    </div>

    <el-scrollbar class="mw__scroll">
      <table class="mw__table">
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Canonical</th>
            <th>Enabled</th>
            <th>Visible</th>
            <th>Tradable</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="rows.length === 0">
            <td colspan="5" class="mw__empty">{{ store.loading ? 'Loading…' : 'No symbols reported' }}</td>
          </tr>
          <tr v-for="r in rows" :key="r.broker_symbol_id">
            <td class="mw__symbol">
              {{ r.broker_symbol }}
              <span v-if="r.selected" class="mw__flag" title="selected">★</span>
            </td>
            <td class="qp-dim">{{ r.canonical_instrument }}</td>
            <td><b :class="r.enabled ? 'qp-up' : 'qp-down'">{{ r.enabled ? 'Y' : 'N' }}</b></td>
            <td><b :class="r.visible ? 'qp-up' : 'qp-down'">{{ r.visible ? 'Y' : 'N' }}</b></td>
            <td><b :class="r.tradable ? 'qp-up' : 'qp-down'">{{ r.tradable ? 'Y' : 'N' }}</b></td>
          </tr>
        </tbody>
      </table>
    </el-scrollbar>
  </div>
</template>

<style scoped>
.mw__summary {
  display: flex;
  gap: 14px;
  font-size: 11px;
  color: var(--qp-text-dim);
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.mw__summary b { color: var(--qp-accent); font-weight: 700; }
.mw__scroll { flex: 1; }
.mw__table { width: 100%; border-collapse: collapse; font-size: 11.5px; }
.mw__table th {
  text-align: left;
  font-weight: 500;
  color: var(--qp-text-dim);
  font-size: 10px;
  letter-spacing: 0.5px;
  padding: 4px 8px;
  border-bottom: 1px solid var(--qp-border-soft);
  position: sticky;
  top: 0;
  background: var(--qp-panel);
}
.mw__table td { padding: 4px 8px; border-bottom: 1px solid rgba(148, 163, 184, 0.06); }
.mw__symbol { font-weight: 600; white-space: nowrap; }
.mw__flag { color: var(--qp-warn); margin-left: 4px; }
.mw__empty { text-align: center; color: var(--qp-text-dim); padding: 24px 0; }
</style>
