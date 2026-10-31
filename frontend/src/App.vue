<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref } from 'vue'
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
import AlertsPanel from './components/panels/AlertsPanel.vue'
import NewsFilterPanel from './components/panels/NewsFilterPanel.vue'
import SettingsPanel from './components/panels/SettingsPanel.vue'
import HelpPanel from './components/panels/HelpPanel.vue'
import StrategiesPanel from './components/panels/StrategiesPanel.vue'
import SignalsPanel from './components/panels/SignalsPanel.vue'
import CommandsPanel from './components/panels/CommandsPanel.vue'
import BacktestsPanel from './components/panels/BacktestsPanel.vue'
import AiPanel from './components/panels/AiPanel.vue'
import AuditPanel from './components/panels/AuditPanel.vue'

// 轻量视图状态机（不引入 vue-router，零新依赖）
type ViewKey =
  | 'dashboard'
  | 'performance'
  | 'risk'
  | 'strategies'
  | 'signals'
  | 'commands'
  | 'backtests'
  | 'ai'
  | 'audit'
  | 'filtering'
  | 'expert'
  | 'journal'
  | 'news'
  | 'market'
  | 'alerts'
  | 'settings'
  | 'help'

const activeView = ref<ViewKey>('dashboard')

const viewMeta: Record<ViewKey, { title: string; sub: string }> = {
  dashboard: { title: '主仪表盘', sub: '账户管理 · OBSERVE_ONLY 只读观察切片' },
  performance: { title: '绩效分析', sub: '基于真实成交历史统计 · OBSERVE_ONLY' },
  risk: { title: '风险管理', sub: '账户风险指标 · 真实快照数据' },
  strategies: { title: '策略引擎', sub: '策略目录与实例绑定 · 建议性信号' },
  signals: { title: '交易信号', sub: '云端收盘 bar 评估 · 边沿触发去重' },
  commands: { title: '命令通道', sub: '命令状态机与风控闸门审计 · OBSERVE_ONLY' },
  backtests: { title: '回测研究', sub: '可复现历史回测任务与指标报告' },
  ai: { title: 'AI 分析', sub: '市场状态识别与策略适用性 · ADVISORY 建议性 · 不参与交易许可' },
  audit: { title: '审计日志', sub: '状态变更操作留痕 · best-effort 写入 · X-API-Key 网关之后' },
  filtering: { title: '过滤设置', sub: '云端版本化配置 · EA 拉取生效' },
  expert: { title: 'EA 设置', sub: '云端版本化配置 · EA 拉取生效' },
  journal: { title: '交易日志', sub: '成交与挂单历史 · 真实经纪商数据' },
  news: { title: '新闻过滤', sub: 'MT5 内置财经日历 · EA 每 10 分钟刷新' },
  market: { title: '行情监控', sub: '品种点差观测 · EA 每小时刷新' },
  alerts: { title: '系统告警', sub: 'system_events 事件流 · 真实数据' },
  settings: { title: '设置', sub: '系统与账户信息 · 只读' },
  help: { title: '帮助与支持', sub: '架构链路与常见问题' },
}
const viewTitle = computed(() => viewMeta[activeView.value].title)
const viewSub = computed(() => viewMeta[activeView.value].sub)

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
    <SidebarNav :active="activeView" @select="activeView = $event as ViewKey" />

    <div class="main">
      <header class="header">
        <div class="header__title">
          <h1>{{ viewTitle }}</h1>
          <span>{{ viewSub }}</span>
        </div>
        <div class="header__actions">
          <el-select
            v-if="store.accounts.length > 0"
            :model-value="store.selectedAccountId"
            placeholder="选择账户"
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
            刷新
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

      <!-- 仪表盘：完整面板组合 -->
      <template v-if="activeView === 'dashboard'">
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
      </template>

      <!-- 绩效：统计 + 盈利监控 + 净值曲线 -->
      <template v-else-if="activeView === 'performance'">
        <KpiBand />
        <main class="grid">
          <PerformancePanel />
          <ProfitMonitorPanel />
          <ProfitSummaryPanel />
          <EquityGraphPanel class="grid__full" />
        </main>
      </template>

      <!-- 风险管理 -->
      <template v-else-if="activeView === 'risk'">
        <KpiBand />
        <main class="view">
          <RiskPanel />
        </main>
      </template>

      <!-- 其余单面板视图 -->
      <main v-else class="view">
        <StrategiesPanel v-if="activeView === 'strategies'" />
        <SignalsPanel v-else-if="activeView === 'signals'" />
        <CommandsPanel v-else-if="activeView === 'commands'" />
        <BacktestsPanel v-else-if="activeView === 'backtests'" />
        <AiPanel v-else-if="activeView === 'ai'" />
        <AuditPanel v-else-if="activeView === 'audit'" />
        <FilteringPanel v-else-if="activeView === 'filtering'" />
        <ExpertPanel v-else-if="activeView === 'expert'" />
        <TradeJournalPanel v-else-if="activeView === 'journal'" />
        <NewsFilterPanel v-else-if="activeView === 'news'" />
        <MarketWatchPanel v-else-if="activeView === 'market'" />
        <AlertsPanel v-else-if="activeView === 'alerts'" />
        <SettingsPanel v-else-if="activeView === 'settings'" />
        <HelpPanel v-else-if="activeView === 'help'" />
      </main>

      <footer class="footer">
        <span>QuantPilot AI · 智能交易生态系统</span>
        <span>版本 0.1.0（前端切片）</span>
        <span class="footer__status" :class="store.apiHealthy ? 'is-ok' : 'is-bad'">
          {{ store.apiHealthy ? '全系统正常运行' : 'API 离线' }}
        </span>
        <span class="footer__time">最后刷新：{{ store.lastRefreshAt || '—' }}</span>
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
.grid__full { grid-column: 1 / -1; }

/* 单面板视图：面板占满整行 */
.view {
  display: grid;
  grid-template-columns: 1fr;
  gap: 12px;
}

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
