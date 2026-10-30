/** Cloud API client (dev via Vite proxy -> http://127.0.0.1:8000).
 *  All responses use the unified envelope {data, error, request_id, timestamp}. */

export interface ApiEnvelope<T> {
  data: T
  error: { code: string; message: string } | null
  request_id: string
  timestamp: string
}

export interface NodeInfo {
  node_id: string
  device_id: string
  hostname: string
  os_info: string
  agent_version: string
  status: string
  last_seen_at: string | null
  version: number
  terminals: number
  accounts: number
}

export interface TerminalInfo {
  terminal_id: string
  node_id: string
  device_id: string
  terminal_path_hash: string
  terminal_build: number | null
  terminal_company: string | null
  server: string | null
  status: string
  last_seen_at: string | null
}

export interface AccountInfo {
  account_id: string
  terminal_id: string
  node_id: string
  login_ref: string
  server: string
  company: string | null
  currency: string
  margin_mode: string
  account_mode: string
  leverage: number
  status: string
  last_seen_at: string | null
}

export interface AccountSnapshot {
  snapshot_id: string
  account_id: string
  balance: string
  equity: string
  margin: string
  free_margin: string
  currency: string
  open_positions: number
  connected: boolean
  trade_allowed: boolean
  expert_allowed: boolean
  terminal_version: string | null
  ea_version: string | null
  captured_at: string
}

export interface SnapshotPoint {
  balance: string
  equity: string
  margin: string
  free_margin: string
  open_positions: number
  captured_at: string
}

export interface SymbolRow {
  broker_symbol_id: string
  broker_symbol: string
  enabled: boolean
  visible: boolean
  selected: boolean
  tradable: boolean
  canonical_instrument: string
  spec_hash: string | null
  observed_at: string | null
}

export interface PositionRow {
  position_id: string
  account_id: string
  broker_position_ticket: string
  broker_symbol: string
  direction: 'BUY' | 'SELL'
  volume: string
  price_open: string
  current_price: string | null
  sl: string | null
  tp: string | null
  profit: string | null
  swap: string | null
  open_time: string | null
  status: 'OPEN' | 'CLOSED'
  last_reported_at: string | null
  closed_at: string | null
}

export interface DealRow {
  deal_id: string
  account_id: string
  broker_deal_ticket: string
  broker_order_ticket: string
  broker_position_ticket: string
  broker_symbol: string
  direction: 'BUY' | 'SELL'
  entry: 'IN' | 'OUT' | 'INOUT' | 'OUT_BY'
  volume: string
  price: string
  commission: string | null
  swap: string | null
  profit: string | null
  deal_time: string
  magic: number | null
  comment: string | null
}

export interface OrderRow {
  order_id: string
  account_id: string
  broker_ticket: string
  broker_symbol: string
  direction: 'BUY' | 'SELL'
  order_kind: string
  volume: string
  price: string
  sl: string | null
  tp: string | null
  time_setup: string | null
  time_expiration: string | null
  status: 'ACTIVE' | 'REMOVED'
  last_reported_at: string | null
  removed_at: string | null
}

export interface PerfStats {
  closed_trades: number
  wins: number
  losses: number
  win_rate: string | null
  net_profit: string
  gross_profit: string
  gross_loss: string
  profit_factor: string | null
  expectancy: string | null
  avg_win: string | null
  avg_loss: string | null
  max_consecutive_wins: number
  max_consecutive_losses: number
  best_trade: string | null
  worst_trade: string | null
  max_drawdown: string
  recovery_factor: string | null
  total_commission: string
  total_swap: string
  all_deals: number
}

export interface DailyBucket {
  date: string
  profit: string
  loss: string
  net: string
  trades: number
}

export interface PerfPeriod {
  today_net: string
  week_net: string
  month_net: string
  last_trade_net: string | null
  last_trade_at: string | null
}

export interface PerformanceData {
  stats: PerfStats
  daily: DailyBucket[]
  period: PerfPeriod
}

export interface FiltersConfig {
  news_filter: boolean
  high_impact_only: boolean
  session_filter: boolean
  max_spread: boolean
  slippage_guard: boolean
  weekend_block: boolean
  max_daily_orders: number
  max_positions: number
}

export interface ExpertConfig {
  auto_trading: boolean
  sound_alert: boolean
  popup_alert: boolean
  notification_alert: boolean
  close_on_opposite: boolean
  magic_number: number
  order_comment: string
}

export interface AccountConfigData {
  account_id: string | null
  version: number
  status: string
  source: string
  resolved?: boolean
  config: { filters: FiltersConfig; expert: ExpertConfig }
  created_at: string | null
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: { Accept: 'application/json' } })
  if (!res.ok) {
    throw new Error(`HTTP ${res.status} on ${path}`)
  }
  const body = (await res.json()) as ApiEnvelope<T>
  if (body.error) {
    throw new Error(`${body.error.code}: ${body.error.message}`)
  }
  return body.data
}

async function send<T>(path: string, method: 'PUT' | 'POST', payload: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!res.ok) {
    throw new Error(`HTTP ${res.status} on ${path}`)
  }
  const body = (await res.json()) as ApiEnvelope<T>
  if (body.error) {
    throw new Error(`${body.error.code}: ${body.error.message}`)
  }
  return body.data
}

export const api = {
  health: () => get<{ status: string; db: string }>('/healthz'),
  nodes: () => get<NodeInfo[]>('/api/v1/nodes'),
  terminals: () => get<TerminalInfo[]>('/api/v1/terminals'),
  accounts: () => get<AccountInfo[]>('/api/v1/accounts'),
  snapshot: (accountId: string) => get<AccountSnapshot>(`/api/v1/accounts/${accountId}/snapshot`),
  snapshots: (accountId: string, limit = 500) =>
    get<SnapshotPoint[]>(`/api/v1/accounts/${accountId}/snapshots?limit=${limit}`),
  symbols: (accountId: string) => get<SymbolRow[]>(`/api/v1/accounts/${accountId}/symbols`),
  positions: (accountId: string) => get<PositionRow[]>(`/api/v1/accounts/${accountId}/positions`),
  deals: (accountId: string, limit = 500) =>
    get<DealRow[]>(`/api/v1/accounts/${accountId}/deals?limit=${limit}`),
  orders: (accountId: string) => get<OrderRow[]>(`/api/v1/accounts/${accountId}/orders`),
  performance: (accountId: string, days = 30) =>
    get<PerformanceData>(`/api/v1/accounts/${accountId}/performance?days=${days}`),
  getConfig: (accountId: string) => get<AccountConfigData>(`/api/v1/accounts/${accountId}/config`),
  saveConfig: (accountId: string, config: { filters: FiltersConfig; expert: ExpertConfig }) =>
    send<AccountConfigData>(`/api/v1/accounts/${accountId}/config`, 'PUT', config),
}

/** Parse API decimal strings ("9980.00000000", "0E-8") safely. */
export function num(value: string | null | undefined): number | null {
  if (value === null || value === undefined) return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}
