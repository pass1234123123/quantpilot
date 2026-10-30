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
}

/** Parse API decimal strings ("9980.00000000", "0E-8") safely. */
export function num(value: string | null | undefined): number | null {
  if (value === null || value === undefined) return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}
