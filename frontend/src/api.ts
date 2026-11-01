/** Cloud API client. All requests go to the configured API base
 *  (qp.apiBase, set on the first-run ConnectionSetup page or in
 *  Settings); responses use the unified envelope {data, error,
 *  request_id, timestamp}. Dev and desktop share the same direct
 *  connection path - no Vite proxy masking (R3 P1-01). */

import {
  clearAuthToken,
  clearLoginApiKey,
  getAuthToken,
  getLoginApiKey,
  getSessionUser,
  setAuthToken,
  setLoginApiKey,
  setSessionUser,
} from './secure-store'

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
  ea_run_mode: string | null
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
  /** 最新点差观测（EA 每小时 spec 刷新时上报，云端仅存最新值） */
  spread_points: number | null
  bid: string | null
  ask: string | null
  quote_observed_at: string | null
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

export interface CalendarEventRow {
  calendar_event_id: string
  event_uid: string
  country_code: string
  currency: string
  name: string
  importance: 'NONE' | 'LOW' | 'MODERATE' | 'HIGH'
  multiplier: string
  digits: number
  event_time: string
  actual: string | null
  forecast: string | null
  previous: string | null
  observed_at: string
}

export interface SystemEventRow {
  event_id: string
  source: string
  event_type: string
  severity: string
  payload: Record<string, unknown> | null
  occurred_at: string
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

/** AI 运行模式（doc 8.5）：OFF 关闭 / ADVISORY 仅建议展示 */
export interface AiModeConfig {
  mode: 'OFF' | 'ADVISORY'
}

export interface AccountConfigData {
  account_id: string | null
  version: number
  status: string
  source: string
  resolved?: boolean
  config: { filters: FiltersConfig; expert: ExpertConfig; ai?: AiModeConfig }
  created_at: string | null
}

export interface StrategyRow {
  strategy_id: string
  code: string
  name: string
  description: string | null
  status: string
  strategy_version_id: string
  version: string
  artifact_hash: string
  config_schema: Record<string, unknown>
  created_at: string
}

export interface StrategyInstanceRow {
  instance_id: string
  account_id: string
  strategy_code: string
  strategy_version: string
  strategy_version_id: string
  broker_symbol: string
  timeframe: string
  config: Record<string, unknown>
  config_revision: number
  status: 'ENABLED' | 'DISABLED'
  created_at: string
  updated_at: string
}

export interface SignalPayload {
  direction: 'BUY' | 'SELL'
  strength: string
  reference_price: string | null
  proposed_stop_loss?: string | null
  proposed_take_profit?: string | null
  reason_codes: string[]
  valid_until?: string | null
  market_data_at?: string | null
}

export interface SignalRow {
  id: string
  signal_id: string
  account_id: string
  strategy_instance_id: string
  signal_json: SignalPayload
  source_bar_time: string
  generated_at: string
  expires_at: string | null
}

export interface CommandRow {
  command_id: string
  account_id: string
  command_type: string
  payload: Record<string, unknown>
  status: string
  expires_at: string | null
  created_by: string
  strategy_instance_id: string | null
  signal_id: string | null
  delivery_attempts: number
  delivered_at: string | null
  acked_at: string | null
  ack_code: string | null
  ack_message: string | null
  reconciled: boolean
  unknown_at: string | null
  expired_at: string | null
  created_at: string
  updated_at: string
}

export interface RiskDecisionRow {
  id: string
  account_id: string
  command_id: string
  signal_id: string | null
  decision: string
  reason_codes: string[] | null
  config_version: number
  decided_at: string
}

export interface BacktestJobSummary {
  job_id: string
  account_id: string
  strategy_code: string
  broker_symbol: string
  timeframe: string
  status: 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED'
  range_start: string
  range_end: string
  config_revision: number
  initial_balance: string
  volume: string
  data_hash: string | null
  error: string
  created_at: string
  completed_at: string | null
}

export interface BacktestMetrics {
  initial_balance: string
  final_balance: string
  net_profit: string
  max_drawdown: string
  max_drawdown_pct: number
  profit_factor: string | null
  win_rate: number
  expectancy: string
  sharpe: number | null
  sortino: number | null
  recovery_factor: string | null
  exposure: number
  average_holding_minutes: string
  trade_count: number
  winning_trades: number
  losing_trades: number
  cost_breakdown: Record<string, string>
}

export interface BacktestReport {
  job_id: string
  strategy: {
    code: string
    version: string
    config: Record<string, unknown>
    config_revision: number
  }
  data: {
    bar_count: number
    sha256: string
    first_bar_time: string
    last_bar_time: string
  }
  assumptions: Record<string, unknown>
  trades: Array<{
    source_bar_time: string
    entry_time: string
    exit_time: string
    direction: 'BUY' | 'SELL'
    volume: string
    entry_price: string
    exit_price: string
    gross_profit: string
    net_profit: string
    costs: Record<string, string>
    exit_reason: string
    holding_minutes: string
    reason_codes: string[]
  }>
  equity_curve: Array<{ bar_time: string; equity: string }>
  metrics: BacktestMetrics
}

export interface BacktestJobDetail extends BacktestJobSummary {
  strategy_version: string
  config: Record<string, unknown>
  cost_model: Record<string, unknown>
  idempotency_key: string
  request_hash: string
  metrics: BacktestMetrics | null
  report: BacktestReport | null
  report_uri: string | null
  updated_at: string
}

/** 回测成本假设（backtest_engine.BacktestCostModel）：显式研究假设 */
export interface BacktestCostModelInput {
  spread_per_side: string
  slippage_per_side: string
  commission_per_lot_per_side: string
  swap_per_lot_per_bar: string
  contract_size: string
  quote_to_account_rate: string
}

export interface CreateBacktestPayload {
  strategy_instance_id: string
  range_start: string
  range_end: string
  initial_balance: string
  volume: string
  cost_model: BacktestCostModelInput
}

/** AI 分析 doc 8.4：output_json 严格结构（统计版 regime 分类器） */
export interface AiStrategyScore {
  strategy_id: string
  score: number
  reason_codes: string[]
}

export interface AiAnalysisDoc {
  analysis_id: string
  model_provider: string
  model_id: string
  prompt_version: string
  created_at: string
  input_snapshot_id: string
  market_regime: 'TREND_UP' | 'TREND_DOWN' | 'RANGE' | 'HIGH_VOL' | 'UNKNOWN'
  regime_confidence: number
  strategy_scores: AiStrategyScore[]
  signal_decision: 'ALLOW' | 'REVIEW'
  risk_flags: string[]
  explanation: string
  expires_at: string
  source_bar_time: string
  duration_ms: number
  cost: string
}

export interface AiAnalysisRow {
  id: string
  analysis_id: string
  account_id: string
  broker_symbol: string
  timeframe: string
  model_provider: string
  model_id: string
  prompt_version: string
  input_hash: string
  output_json: AiAnalysisDoc
  source_bar_time: string
  created_at: string
  expires_at: string
  duration_ms: number
}

export interface AiAnalyzeResult {
  ai_mode: 'OFF' | 'ADVISORY'
  analysis: AiAnalysisDoc | null
}

export interface AuditLogRow {
  id: string
  account_id: string
  actor: string
  action: string
  object_type: string
  object_id: string | null
  payload: Record<string, unknown> | null
  created_at: string
}

// API base configuration (R3 P1-01). The desktop shell (Tauri production)
// has no Vite dev proxy: without an explicit base, requests hit the
// webview origin and never reach the cloud API - the first-install login
// deadlock. The base is configured on the first-run ConnectionSetup page
// BEFORE the login gate, validated, and connectivity-tested. Dev mode
// configures it too (direct connection) so dev and production share the
// exact same request path - a proxy cannot mask a desktop bug again.
// The base URL is not a secret: localStorage is fine.

export function getApiBase(): string {
  return (localStorage.getItem('qp.apiBase') ?? '').trim().replace(/\/+$/, '')
}

export function setApiBase(value: string): void {
  localStorage.setItem('qp.apiBase', value.trim().replace(/\/+$/, ''))
}

function withBase(path: string): string {
  const base = getApiBase()
  return base ? `${base}${path}` : path
}

/** Loopback hosts where plain HTTP is acceptable (local dev only, R3.1 P1).
 *  URL.hostname lowercases itself and keeps IPv6 brackets ("[::1]"). */
const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '::1', '[::1]'])

/** Structural validation BEFORE any network attempt (R3 P1-01 + R3.1 P1):
 *  scheme http/https, non-empty host, port (if present) 1-65535, and no
 *  path/query/fragment - the client appends /api/v1/... itself.
 *  HTTP is reserved for loopback: a remote base must be HTTPS or the JWT
 *  Bearer token, the personal qpk key and trading traffic would cross
 *  the network in clear text. Shared by ConnectionSetup (first run) and
 *  SettingsPanel - one function, identical verdicts on both pages. */
export function validateApiBase(value: string): { ok: boolean; error?: string } {
  const raw = value.trim()
  if (!raw) return { ok: false, error: 'API 地址不能为空' }
  let url: URL
  try {
    url = new URL(raw)
  } catch {
    return { ok: false, error: '格式无效：需以 http:// 或 https:// 开头' }
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    return { ok: false, error: '协议仅支持 http:// 或 https://' }
  }
  if (url.protocol === 'http:' && !LOCAL_HOSTS.has(url.hostname)) {
    return {
      ok: false,
      error: '远程地址必须使用 https://（明文 HTTP 仅限本机 localhost / 127.0.0.1 / ::1）',
    }
  }
  if (!url.hostname) return { ok: false, error: '主机名不能为空' }
  if (url.port) {
    const port = Number(url.port)
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      return { ok: false, error: '端口必须在 1-65535 之间' }
    }
  }
  if (url.pathname !== '/' && url.pathname !== '') {
    return { ok: false, error: '地址不应包含路径（仅协议+主机+端口）' }
  }
  if (url.search || url.hash) return { ok: false, error: '地址不应包含查询参数' }
  return { ok: true }
}

/** Connectivity probe: GET {base}/healthz - a public, credential-free
 *  endpoint. 5s timeout. Throws a user-facing message on failure.
 *  Persists nothing: callers save the base only after this passes.
 *  TLS is never relaxed (R3.1 P1): an invalid/self-signed certificate
 *  fails the fetch (TypeError -> the network-error message below); there
 *  is no escape hatch anywhere in this client to skip certificate
 *  verification. */
export async function testApiBase(value: string): Promise<void> {
  const check = validateApiBase(value)
  if (!check.ok) throw new Error(check.error)
  const base = value.trim().replace(/\/+$/, '')
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), 5000)
  try {
    const res = await fetch(`${base}/healthz`, {
      headers: { Accept: 'application/json' },
      signal: ctrl.signal,
    })
    if (!res.ok) throw new Error(`API 响应异常：HTTP ${res.status}`)
    const body = (await res.json().catch(() => null)) as ApiEnvelope<unknown> | null
    if (!body || typeof body !== 'object' || !('data' in body)) {
      throw new Error('目标不是 QuantPilot API（响应缺少统一结构）')
    }
  } catch (e) {
    if (e instanceof Error && e.name === 'AbortError') {
      throw new Error('连接超时（5 秒）：请确认 API 已启动且地址正确')
    }
    if (e instanceof TypeError) {
      throw new Error('无法连接到 API：网络错误或地址不可达')
    }
    throw e
  } finally {
    clearTimeout(timer)
  }
}

// R3 P1-02: the only accepted identities are the logged-in user's JWT
// (password login) or the personal qpk_ key entered ON THE LOGIN PAGE.
// The retired R2 Settings-slot key is no longer a credential source, and
// the shared service key is a machine lane that never identifies a
// person - neither is ever sent here.
function authHeaders(): Record<string, string> {
  const token = getAuthToken()
  if (token) return { Authorization: `Bearer ${token}` }
  const key = getLoginApiKey()
  if (key) return { 'X-API-Key': key }
  return {}
}

type AuthRequiredListener = () => void
let authRequiredListener: AuthRequiredListener | null = null

/** The app registers this to bounce the UI back to the login gate when
 *  a session dies (401: expired, revoked, disabled user, role change).
 *  The session never degrades into the shared service key. */
export function setAuthRequiredListener(fn: AuthRequiredListener | null): void {
  authRequiredListener = fn
}

// A 401 means the session is gone: clear BOTH credential slots (the JWT
// and the personal login key are mutually exclusive by design) and notify
// the gate. There is no fallback identity to degrade into (P1-01).
function dropSession(): void {
  void clearAuthToken().catch(() => {})
  void clearLoginApiKey().catch(() => {})
  setSessionUser(null)
  authRequiredListener?.()
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(withBase(path), {
    headers: { Accept: 'application/json', ...authHeaders() },
  })
  if (res.status === 401) dropSession()
  if (!res.ok) {
    throw new Error(`HTTP ${res.status} on ${path}`)
  }
  const body = (await res.json()) as ApiEnvelope<T>
  if (body.error) {
    throw new Error(`${body.error.code}: ${body.error.message}`)
  }
  return body.data
}

async function send<T>(
  path: string,
  method: 'PUT' | 'POST',
  payload: unknown,
  extraHeaders?: Record<string, string>,
): Promise<T> {
  const res = await fetch(withBase(path), {
    method,
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      ...authHeaders(),
      ...(extraHeaders ?? {}),
    },
    body: JSON.stringify(payload),
  })
  if (res.status === 401) dropSession()
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as ApiEnvelope<T> | null
    if (body?.error) {
      throw new Error(`${body.error.code}: ${body.error.message}`)
    }
    throw new Error(`HTTP ${res.status} on ${path}`)
  }
  const body = (await res.json()) as ApiEnvelope<T>
  if (body.error) {
    throw new Error(`${body.error.code}: ${body.error.message}`)
  }
  return body.data
}

export interface AuthUser {
  user_id: string
  username: string
  role: string
}

/** Role helpers for INTERACTION gating only (hide/disable buttons). The
 *  backend re-validates the role on every request - not a boundary. */
export function canWrite(): boolean {
  const role = getSessionUser()?.role
  return role === 'ADMIN' || role === 'TRADER'
}

export function canResearch(): boolean {
  const role = getSessionUser()?.role
  return role === 'ADMIN' || role === 'TRADER' || role === 'ANALYST'
}

interface LoginResponse {
  token: string
  token_type: string
  expires_in: number
  user: AuthUser
}

export const api = {
  // Login posts WITHOUT authHeaders(): the backend rejects requests that
  // carry an Authorization header, and a stale token must not block a
  // fresh login (the login route is credential-exempt server-side).
  // The token goes to the OS credential store on desktop (sessionStorage
  // in browser dev) - never localStorage (P1-03).
  login: async (username: string, password: string): Promise<AuthUser> => {
    const res = await fetch(withBase('/api/v1/auth/login'), {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    })
    const body = (await res.json().catch(() => null)) as ApiEnvelope<LoginResponse> | null
    if (!res.ok || !body || body.error || !body.data?.token) {
      const code = body?.error?.code ?? `HTTP ${res.status}`
      const message = body?.error?.message ?? '登录失败'
      throw new Error(`${code}: ${message}`)
    }
    await clearLoginApiKey().catch(() => {})
    await setAuthToken(body.data.token)
    setSessionUser(body.data.user)
    return body.data.user
  },
  /** R3 P1-02: personal API key login. The key must be a qpk_ personal
   *  key - a shared service key is a machine lane and is rejected here.
   *  It is NOT a JWT and never masquerades as one: it lives in its own
   *  credential slot and travels via X-API-Key; the backend resolves it
   *  to its user (role, grants, revocation) on every request, so a
   *  revoked key stops working immediately. */
  loginWithApiKey: async (key: string): Promise<AuthUser> => {
    const trimmed = key.trim()
    if (!trimmed.startsWith('qpk_')) {
      throw new Error('个人 API Key 必须以 qpk_ 开头；共享服务密钥不能用于登录')
    }
    // clear the JWT first: the backend ignores X-API-Key while an
    // Authorization header is present, and a stale token must not leak
    // into the new session
    await clearAuthToken().catch(() => {})
    await setLoginApiKey(trimmed)
    try {
      const me = await get<AuthUser>('/api/v1/auth/me')
      setSessionUser(me)
      return me
    } catch (e) {
      await clearLoginApiKey().catch(() => {})
      throw e
    }
  },
  logout: () => {
    dropSession()
  },
  currentUser: (): AuthUser | null => {
    return getSessionUser()
  },
  /** Reload-safe session restore: re-validate EITHER stored credential
   *  (the JWT from password login, or the personal login key) against
   *  the backend instead of trusting any cached copy. Returns null when
   *  there is no usable session. */
  verifySession: async (): Promise<AuthUser | null> => {
    if (!getAuthToken() && !getLoginApiKey()) return null
    try {
      const me = await get<AuthUser>('/api/v1/auth/me')
      setSessionUser(me)
      return me
    } catch {
      // 401 already cleared the session via get(); anything else also
      // means "no usable session right now" - the login gate decides.
      return null
    }
  },
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
  calendar: (accountId: string, hours = 48) =>
    get<CalendarEventRow[]>(`/api/v1/accounts/${accountId}/calendar?hours=${hours}`),
  events: (limit = 100) => get<SystemEventRow[]>(`/api/v1/events?limit=${limit}`),
  getConfig: (accountId: string) => get<AccountConfigData>(`/api/v1/accounts/${accountId}/config`),
  saveConfig: (accountId: string, config: { filters: FiltersConfig; expert: ExpertConfig }) =>
    send<AccountConfigData>(`/api/v1/accounts/${accountId}/config`, 'PUT', config),
  strategies: () => get<StrategyRow[]>('/api/v1/strategies'),
  strategyInstances: (accountId: string) =>
    get<StrategyInstanceRow[]>(`/api/v1/accounts/${accountId}/strategy-instances`),
  setInstanceStatus: (accountId: string, instanceId: string, status: 'ENABLED' | 'DISABLED') =>
    send<StrategyInstanceRow>(
      `/api/v1/accounts/${accountId}/strategy-instances/${instanceId}/status`,
      'POST',
      { status },
    ),
  signals: (accountId: string, limit = 100) =>
    get<SignalRow[]>(`/api/v1/accounts/${accountId}/signals?limit=${limit}`),
  commands: (accountId: string, limit = 100) =>
    get<CommandRow[]>(`/api/v1/commands?account_id=${accountId}&limit=${limit}`),
  riskDecisions: (accountId: string, limit = 100) =>
    get<RiskDecisionRow[]>(`/api/v1/risk/decisions?account_id=${accountId}&limit=${limit}`),
  backtests: (accountId: string, limit = 50) =>
    get<BacktestJobSummary[]>(`/api/v1/backtests?account_id=${accountId}&limit=${limit}`),
  backtest: (jobId: string) => get<BacktestJobDetail>(`/api/v1/backtests/${jobId}`),
  createBacktest: (payload: CreateBacktestPayload) =>
    send<BacktestJobDetail>(
      '/api/v1/backtests',
      'POST',
      payload,
      { 'Idempotency-Key': crypto.randomUUID() },
    ),
  aiAnalyses: (accountId: string, limit = 100) =>
    get<AiAnalysisRow[]>(`/api/v1/accounts/${accountId}/ai/analyses?limit=${limit}`),
  aiAnalyze: (accountId: string, brokerSymbol: string, timeframe: string) =>
    send<AiAnalyzeResult>(
      `/api/v1/accounts/${accountId}/ai/analyze`,
      'POST',
      { broker_symbol: brokerSymbol, timeframe },
    ),
  auditLogs: (accountId?: string, action?: string, limit = 100) =>
    get<AuditLogRow[]>(
      `/api/v1/audit-logs?limit=${limit}${accountId ? `&account_id=${accountId}` : ''}${
        action ? `&action=${encodeURIComponent(action)}` : ''
      }`,
    ),
}

/** Parse API decimal strings ("9980.00000000", "0E-8") safely. */
export function num(value: string | null | undefined): number | null {
  if (value === null || value === undefined) return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}
