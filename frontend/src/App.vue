<script setup lang="ts">
import { onMounted, onBeforeUnmount } from 'vue'
import { useDashboardStore } from './stores/dashboard'
import SidebarNav from './components/SidebarNav.vue'
import KpiBand from './components/KpiBand.vue'
import RiskPanel from './components/panels/RiskPanel.vue'
import PerformancePanel from './components/panels/PerformancePanel.vue'
import ProfitMonitorPanel from './components/panels/ProfitMonitorPanel.vue'
import FilteringPanel from './components/panels/FilteringPanel.vue'
import ExpertPanel from './components/panels/ExpertPanel.vue'
import ProfitSummaryPanel from './components/panels/ProfitSummaryPanel.vue'
import EquityGraphPanel from './components/panels/EquityGraphPanel.vue'
import MarketWatchPanel from './components/panels/MarketWatchPanel.vue'
import TradeJournalPanel from './components/panels/TradeJournalPanel.vue'
import BottomCards from './components/panels/BottomCards.vue'

const store = useDashboardStore()
let timer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  store.refreshAll()
  timer = setInterval(() => store.refreshAll(), 30000)
})
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div class="shell">
    <SidebarNav />

    <div class="main">
      <header class="header">
        <div class="header__title">
          <h1>MASTER DASHBOARD</h1>
          <span>Account Management · OBSERVE_ONLY 切片</span>
        </div>
        <div class="header__actions">
          <el-select
            v-if="store.accounts.length > 0"
            :model-value="store.selectedAccountId"
            placeholder="Select Account"
            size="small"
            style="width: 260px"
            @update:model-value="store.selectAccount($event as string)"
          >
            <el-option
              v-for="a in store.accounts"
              :key="a.account_id"
              :value="a.account_id"
              :label="`${a.login_ref} · ${a.server} · ${a.currency}`"
            />
          </el-select>
          <el-button size="small" :loading="store.loading" @click="store.refreshAll()">
            Refresh
          </el-button>
        </div>
      </header>

      <el-alert
        v-if="store.error"
        :title="`API 错误：${store.error}（请确认 services/api 已启动）`"
        type="error"
        show-icon
        :closable="false"
        style="margin-bottom: 12px"
      />

      <KpiBand />

      <main class="grid">
        <RiskPanel />
        <PerformancePanel />
        <ProfitMonitorPanel />
        <FilteringPanel />
        <ExpertPanel />
        <ProfitSummaryPanel />
        <EquityGraphPanel class="grid__equity" />
        <MarketWatchPanel class="grid__market" />
        <TradeJournalPanel class="grid__journal" />
      </main>

      <BottomCards />

      <footer class="footer">
        <span>QuantPilot AI · Smart Trade Ecosystem</span>
        <span>Version 0.1.0 (Frontend Slice)</span>
        <span class="footer__status" :class="store.apiHealthy ? 'is-ok' : 'is-bad'">
          {{ store.apiHealthy ? 'All Systems Operational' : 'API Offline' }}
        </span>
        <span class="footer__time">Last Refresh: {{ store.lastRefreshAt || '—' }}</span>
      </footer>
    </div>
  </div>
</template>

<style scoped>
.shell {
  display: flex;
  height: 100vh;
  overflow: hidden;
}

.main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  padding: 16px 18px 10px;
  overflow-y: auto;
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  margin-bottom: 14px;
}

.header__title h1 {
  margin: 0;
  font-size: 17px;
  letter-spacing: 2.5px;
  font-weight: 700;
}
.header__title span {
  font-size: 11px;
  color: var(--qp-text-dim);
  letter-spacing: 0.5px;
}

.header__actions { display: flex; gap: 10px; align-items: center; }

.grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
}
.grid__equity { grid-column: span 2; }
.grid__journal { grid-column: span 3; }

.footer {
  display: flex;
  align-items: center;
  gap: 22px;
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px solid var(--qp-border-soft);
  font-size: 11px;
  color: var(--qp-text-dim);
}
.footer__status { display: flex; align-items: center; gap: 6px; }
.footer__status::before {
  content: '';
  width: 7px;
  height: 7px;
  border-radius: 50%;
}
.footer__status.is-ok { color: var(--qp-up); }
.footer__status.is-ok::before { background: var(--qp-up); box-shadow: 0 0 6px var(--qp-up); }
.footer__status.is-bad { color: var(--qp-down); }
.footer__status.is-bad::before { background: var(--qp-down); }
.footer__time { margin-left: auto; }

@media (max-width: 1400px) {
  .grid { grid-template-columns: repeat(2, 1fr); }
  .grid__journal { grid-column: span 2; }
}
</style>
