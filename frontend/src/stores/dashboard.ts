import { defineStore } from 'pinia'
import { api, num, type AccountInfo, type AccountSnapshot, type NodeInfo, type SnapshotPoint, type SymbolRow, type TerminalInfo } from '../api'

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

        const [nodes, terminals, accounts] = await Promise.all([
          api.nodes(),
          api.terminals(),
          api.accounts(),
        ])
        this.nodes = nodes
        this.terminals = terminals
        this.accounts = accounts

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
      const [snapshot, history, symbols] = await Promise.all([
        api.snapshot(accountId).catch(() => null),
        api.snapshots(accountId, 500).catch(() => [] as SnapshotPoint[]),
        api.symbols(accountId).catch(() => [] as SymbolRow[]),
      ])
      if (this.selectedAccountId !== accountId) return
      this.snapshot = snapshot
      this.snapshotHistory = history
      this.symbols = symbols
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
