import { defineStore } from 'pinia'
import { api, num, type AccountConfigData, type AccountInfo, type AccountSnapshot, type AiAnalysisRow, type AuditLogRow, type BacktestJobSummary, type CalendarEventRow, type CommandRow, type CreateBacktestPayload, type DealRow, type NodeInfo, type OrderRow, type PerformanceData, type PositionRow, type RiskDecisionRow, type SignalRow, type SnapshotPoint, type StrategyInstanceRow, type StrategyRow, type SymbolRow, type SystemEventRow, type TerminalInfo } from '../api'

export const useDashboardStore = defineStore('dashboard', {
  state: () => ({
    loading: false,
    error: '' as string,
    apiHealthy: false,
    nodes: [] as NodeInfo[],
    terminals: [] as TerminalInfo[],
    accounts: [] as AccountInfo[],
    selectedAccountId: '' as string,
    snapshot: null as AccountSnapshot | null,
    snapshotHistory: [] as SnapshotPoint[],
    symbols: [] as SymbolRow[],
    positions: [] as PositionRow[],
    deals: [] as DealRow[],
    orders: [] as OrderRow[],
    calendar: [] as CalendarEventRow[],
    events: [] as SystemEventRow[],
    performance: null as PerformanceData | null,
    config: null as AccountConfigData | null,
    configSaving: false,
    strategies: [] as StrategyRow[],
    instances: [] as StrategyInstanceRow[],
    signals: [] as SignalRow[],
    commands: [] as CommandRow[],
    decisions: [] as RiskDecisionRow[],
    backtests: [] as BacktestJobSummary[],
    aiAnalyses: [] as AiAnalysisRow[],
    aiRunning: false,
    auditLogs: [] as AuditLogRow[],
    auditLoading: false,
    backtestCreating: false,
    lastRefreshAt: '' as string,
  }),
  getters: {
    selectedAccount(state): AccountInfo | null {
      return state.accounts.find((a) => a.account_id === state.selectedAccountId) ?? null
    },
    marginLevel(state): number | null {
      const s = state.snapshot
      if (!s) return null
      const equity = num(s.equity)
      const margin = num(s.margin)
      if (equity === null || margin === null || margin <= 0) return null
      return (equity / margin) * 100
    },
    floatingPl(state): number | null {
      const s = state.snapshot
      if (!s) return null
      const equity = num(s.equity)
      const balance = num(s.balance)
      if (equity === null || balance === null) return null
      return equity - balance
    },
  },
  actions: {
    async refreshAll() {
      this.loading = true
      this.error = ''
      try {
        this.apiHealthy = false
        await api.health()
        this.apiHealthy = true

        const [nodes, terminals, accounts, events, strategies] = await Promise.all([
          api.nodes(),
          api.terminals(),
          api.accounts(),
          api.events(100).catch(() => [] as SystemEventRow[]),
          api.strategies().catch(() => [] as StrategyRow[]),
        ])
        this.nodes = nodes
        this.terminals = terminals
        this.accounts = accounts
        this.events = events
        this.strategies = strategies

        if (!this.selectedAccountId && accounts.length > 0) {
          this.selectedAccountId = accounts[0].account_id
        }
        if (this.selectedAccountId && !accounts.some((a) => a.account_id === this.selectedAccountId)) {
          this.selectedAccountId = accounts[0]?.account_id ?? ''
        }
        if (this.selectedAccountId) {
          await this.refreshAccountDetail(this.selectedAccountId)
        }
        this.lastRefreshAt = new Date().toLocaleTimeString()
      } catch (e) {
        this.error = e instanceof Error ? e.message : String(e)
      } finally {
        this.loading = false
      }
    },
    async refreshAccountDetail(accountId: string) {
      this.snapshot = null
      this.snapshotHistory = []
      this.symbols = []
      this.positions = []
      this.deals = []
      this.orders = []
      this.calendar = []
      this.performance = null
      this.config = null
      this.instances = []
      this.signals = []
      this.commands = []
      this.decisions = []
      this.backtests = []
      this.aiAnalyses = []
      const [
        snapshot, history, symbols, positions, deals, orders, calendar,
        performance, config, instances, signals, commands, decisions, backtests,
        aiAnalyses,
      ] = await Promise.all([
        api.snapshot(accountId).catch(() => null),
        api.snapshots(accountId, 500).catch(() => [] as SnapshotPoint[]),
        api.symbols(accountId).catch(() => [] as SymbolRow[]),
        api.positions(accountId).catch(() => [] as PositionRow[]),
        api.deals(accountId, 500).catch(() => [] as DealRow[]),
        api.orders(accountId).catch(() => [] as OrderRow[]),
        api.calendar(accountId, 48).catch(() => [] as CalendarEventRow[]),
        api.performance(accountId, 30).catch(() => null),
        api.getConfig(accountId).catch(() => null),
        api.strategyInstances(accountId).catch(() => [] as StrategyInstanceRow[]),
        api.signals(accountId, 100).catch(() => [] as SignalRow[]),
        api.commands(accountId, 100).catch(() => [] as CommandRow[]),
        api.riskDecisions(accountId, 100).catch(() => [] as RiskDecisionRow[]),
        api.backtests(accountId, 50).catch(() => [] as BacktestJobSummary[]),
        api.aiAnalyses(accountId, 100).catch(() => [] as AiAnalysisRow[]),
      ])
      if (this.selectedAccountId !== accountId) return
      this.snapshot = snapshot
      this.snapshotHistory = history
      this.symbols = symbols
      this.positions = positions
      this.deals = deals
      this.orders = orders
      this.calendar = calendar
      this.performance = performance
      this.config = config
      this.instances = instances
      this.signals = signals
      this.commands = commands
      this.decisions = decisions
      this.backtests = backtests
      this.aiAnalyses = aiAnalyses
    },
    /** 手动触发一次 AI 分析（doc 8.4；ADVISORY 仅建议，OFF 时返回 ai_mode=OFF） */
    async runAiAnalysis(brokerSymbol: string, timeframe: string) {
      if (!this.selectedAccountId) return null
      this.aiRunning = true
      try {
        const result = await api.aiAnalyze(this.selectedAccountId, brokerSymbol, timeframe)
        this.aiAnalyses = await api
          .aiAnalyses(this.selectedAccountId, 100)
          .catch(() => [] as AiAnalysisRow[])
        return result
      } catch (e) {
        this.error = e instanceof Error ? e.message : String(e)
        return null
      } finally {
        this.aiRunning = false
      }
    },
    /** 创建回测任务（PRD 15：幂等键由客户端生成；无交易命令） */
    async createBacktest(payload: CreateBacktestPayload) {
      this.backtestCreating = true
      try {
        const detail = await api.createBacktest(payload)
        this.backtests = await api
          .backtests(this.selectedAccountId, 50)
          .catch(() => [] as BacktestJobSummary[])
        return detail
      } catch (e) {
        this.error = e instanceof Error ? e.message : String(e)
        return null
      } finally {
        this.backtestCreating = false
      }
    },
    /** 审计日志查询（phase-2 task 6：全局或按账户/操作过滤，最新在前） */
    async refreshAuditLogs(accountId?: string, action?: string, limit = 100) {
      this.auditLoading = true
      try {
        this.auditLogs = await api.auditLogs(accountId, action, limit)
      } catch (e) {
        this.error = e instanceof Error ? e.message : String(e)
        this.auditLogs = []
      } finally {
        this.auditLoading = false
      }
    },
    async setInstanceStatus(instanceId: string, status: 'ENABLED' | 'DISABLED') {
      if (!this.selectedAccountId) return
      try {
        const updated = await api.setInstanceStatus(this.selectedAccountId, instanceId, status)
        this.instances = this.instances.map((i) => (i.instance_id === instanceId ? updated : i))
      } catch (e) {
        this.error = e instanceof Error ? e.message : String(e)
      }
    },
    async saveConfig() {
      if (!this.selectedAccountId || !this.config) return
      this.configSaving = true
      try {
        this.config = await api.saveConfig(this.selectedAccountId, this.config.config)
      } catch (e) {
        this.error = e instanceof Error ? e.message : String(e)
      } finally {
        this.configSaving = false
      }
    },
    async selectAccount(accountId: string) {
      this.selectedAccountId = accountId
      this.loading = true
      try {
        await this.refreshAccountDetail(accountId)
        this.lastRefreshAt = new Date().toLocaleTimeString()
      } catch (e) {
        this.error = e instanceof Error ? e.message : String(e)
      } finally {
        this.loading = false
      }
    },
  },
})
