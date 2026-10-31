import { defineStore } from 'pinia'
import { api, num, type AccountConfigData, type AccountInfo, type AccountSnapshot, type CalendarEventRow, type DealRow, type NodeInfo, type OrderRow, type PerformanceData, type PositionRow, type SnapshotPoint, type SymbolRow, type SystemEventRow, type TerminalInfo } from '../api'

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

        const [nodes, terminals, accounts, events] = await Promise.all([
          api.nodes(),
          api.terminals(),
          api.accounts(),
          api.events(100).catch(() => [] as SystemEventRow[]),
        ])
        this.nodes = nodes
        this.terminals = terminals
        this.accounts = accounts
        this.events = events

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
      const [snapshot, history, symbols, positions, deals, orders, calendar, performance, config] = await Promise.all([
        api.snapshot(accountId).catch(() => null),
        api.snapshots(accountId, 500).catch(() => [] as SnapshotPoint[]),
        api.symbols(accountId).catch(() => [] as SymbolRow[]),
        api.positions(accountId).catch(() => [] as PositionRow[]),
        api.deals(accountId, 500).catch(() => [] as DealRow[]),
        api.orders(accountId).catch(() => [] as OrderRow[]),
        api.calendar(accountId, 48).catch(() => [] as CalendarEventRow[]),
        api.performance(accountId, 30).catch(() => null),
        api.getConfig(accountId).catch(() => null),
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
