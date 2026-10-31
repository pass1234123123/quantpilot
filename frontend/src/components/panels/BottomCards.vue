<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'
import { num } from '../../api'

const store = useDashboardStore()
const account = computed(() => store.selectedAccount)
const snap = computed(() => store.snapshot)
const config = computed(() => store.config)

// 真实数据：Permission/Trading Status ← snapshot 权限位；Account Info ← accounts
const allOperational = computed(
  () => !!snap.value?.connected && !!snap.value?.trade_allowed && !!snap.value?.expert_allowed,
)

const yesNo = (v: boolean | undefined) => (v ? '是' : '否')
const onOff = (v: boolean | undefined) => (v === undefined ? '—' : v ? '开启' : '关闭')

// 新闻过滤配置状态（云端 risk_profiles）
const newsFilter = computed(() => config.value?.config.filters.news_filter)
const highImpactOnly = computed(() => config.value?.config.filters.high_impact_only)

// 财经日历：EA 上报的 MT5 内置日历（真实经纪商数据），倒计时每秒刷新
const nowMs = ref(Date.now())
let tick: number | undefined
onMounted(() => {
  tick = window.setInterval(() => {
    nowMs.value = Date.now()
  }, 1000)
})
onUnmounted(() => {
  if (tick !== undefined) window.clearInterval(tick)
})

const upcomingEvents = computed(() =>
  store.calendar.filter((e) => new Date(e.event_time).getTime() > nowMs.value),
)
const nextEvent = computed(() => upcomingEvents.value[0] ?? null)
const laterEvents = computed(() => upcomingEvents.value.slice(1, 4))

const countdown = computed(() => {
  const ev = nextEvent.value
  if (!ev) return ''
  const diff = Math.max(0, Math.floor((new Date(ev.event_time).getTime() - nowMs.value) / 1000))
  const d = Math.floor(diff / 86400)
  const h = Math.floor((diff % 86400) / 3600)
  const m = Math.floor((diff % 3600) / 60)
  const s = diff % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return d > 0 ? `${d}天 ${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(h)}:${pad(m)}:${pad(s)}`
})

const importanceLabel: Record<string, string> = {
  NONE: '无',
  LOW: '低',
  MODERATE: '中',
  HIGH: '高',
}
const eventTimeText = (iso: string) =>
  new Date(iso).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })

// 点差与滑点配置状态
const maxSpread = computed(() => config.value?.config.filters.max_spread)
const slippageGuard = computed(() => config.value?.config.filters.slippage_guard)

// 品种点差观测：EA 每小时 spec 刷新时上报 spread/bid/ask，
// 云端 symbol_quotes 仅保留每品种最新一次观测（不存逐跳行情）
const spreadSummary = computed(() => {
  const rows = store.symbols.filter((s) => s.spread_points !== null)
  if (!rows.length) return null
  const pts = rows.map((s) => s.spread_points as number)
  let at = ''
  for (const s of rows) {
    if (s.quote_observed_at && s.quote_observed_at > at) at = s.quote_observed_at
  }
  return {
    min: Math.min(...pts),
    max: Math.max(...pts),
    count: rows.length,
    at: at ? new Date(at).toLocaleTimeString('zh-CN', { hour12: false }) : '',
  }
})

// 累计佣金：performance 统计（deals 投影 SUM(commission)，真实经纪商数据）
const commissionNum = computed(() => num(store.performance?.stats.total_commission ?? null))
const commissionText = computed(() =>
  commissionNum.value === null
    ? null
    : commissionNum.value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
)
const currency = computed(() => snap.value?.currency ?? '')
</script>

<template>
  <div class="bc">
    <!-- 新闻过滤（云端配置 + EA 上报的 MT5 内置日历） -->
    <div class="qp-panel bc__card">
      <div class="qp-panel__title">
        新闻过滤
        <span class="qp-live-badge">配置+日历真实</span>
      </div>
      <div class="bc__news">
        <div class="bc__news-row"><span>新闻过滤</span><b :class="newsFilter ? 'qp-up' : 'qp-dim'">{{ onOff(newsFilter) }}</b></div>
        <div class="bc__news-row"><span>仅高影响事件</span><b :class="highImpactOnly ? 'qp-up' : 'qp-dim'">{{ onOff(highImpactOnly) }}</b></div>
        <div class="bc__countdown">
          <template v-if="nextEvent">
            <div class="bc__countdown-label">下一财经事件（MT5 内置日历）</div>
            <div class="bc__countdown-time">{{ countdown }}</div>
            <div class="bc__countdown-name">
              <span class="bc__imp" :class="`bc__imp--${nextEvent.importance.toLowerCase()}`">
                {{ importanceLabel[nextEvent.importance] ?? nextEvent.importance }}
              </span>
              {{ nextEvent.currency || nextEvent.country_code }} {{ nextEvent.name }}
            </div>
            <div class="bc__countdown-name bc__dim">{{ eventTimeText(nextEvent.event_time) }}（本地时间）</div>
          </template>
          <template v-else>
            <div class="bc__countdown-label">下一财经事件</div>
            <div class="bc__countdown-name">
              {{ store.calendar.length > 0 ? '未来 48 小时内无事件' : '待上报（EA 每 10 分钟刷新日历）' }}
            </div>
          </template>
        </div>
        <div v-for="ev in laterEvents" :key="ev.event_uid" class="bc__news-row">
          <span class="bc__event-later">
            {{ eventTimeText(ev.event_time) }}
            <b class="bc__imp-inline" :class="`bc__imp--${ev.importance.toLowerCase()}`">{{ importanceLabel[ev.importance] ?? ev.importance }}</b>
            {{ ev.currency || ev.country_code }} {{ ev.name }}
          </span>
        </div>
      </div>
    </div>

    <!-- 点差与佣金（配置 + EA 上报点差观测 + deals 佣金统计） -->
    <div class="qp-panel bc__card">
      <div class="qp-panel__title">
        点差与佣金
        <span class="qp-live-badge">配置+观测真实</span>
      </div>
      <div class="bc__rows">
        <div class="bc__row"><span>最大点差过滤</span><b :class="maxSpread ? 'qp-up' : 'qp-dim'">{{ onOff(maxSpread) }}</b></div>
        <div class="bc__row"><span>滑点保护</span><b :class="slippageGuard ? 'qp-up' : 'qp-dim'">{{ onOff(slippageGuard) }}</b></div>
        <div class="bc__row">
          <span>品种点差观测</span>
          <b v-if="spreadSummary" class="qp-up">{{ spreadSummary.min }}–{{ spreadSummary.max }} 点 · {{ spreadSummary.count }} 品种</b>
          <b v-else class="qp-dim">待上报（EA 每小时刷新）</b>
        </div>
        <div class="bc__row">
          <span>累计佣金</span>
          <b v-if="commissionText !== null" :class="commissionNum !== null && commissionNum < 0 ? 'qp-down' : 'qp-up'">{{ commissionText }} {{ currency }}</b>
          <b v-else class="qp-dim">待统计</b>
        </div>
        <div v-if="spreadSummary?.at" class="bc__row bc__row--note"><span>点差观测时间</span><b class="qp-dim">{{ spreadSummary.at }}</b></div>
      </div>
    </div>

    <!-- 权限信号（真实数据） -->
    <div class="qp-panel bc__card bc__card--signal">
      <div class="qp-panel__title">
        权限信号
        <span class="qp-live-badge">真实数据</span>
      </div>
      <div class="bc__shield" :class="{ 'is-ok': allOperational }">
        <div class="bc__shield-icon">🛡</div>
        <div class="bc__shield-text">
          {{ allOperational ? '全系统正常运行' : '权限不完整' }}
        </div>
      </div>
      <div class="bc__rows">
        <div class="bc__row"><span>已连接</span><b :class="snap?.connected ? 'qp-up' : 'qp-down'">{{ yesNo(snap?.connected) }}</b></div>
        <div class="bc__row"><span>允许交易</span><b :class="snap?.trade_allowed ? 'qp-up' : 'qp-down'">{{ yesNo(snap?.trade_allowed) }}</b></div>
        <div class="bc__row"><span>允许 EA</span><b :class="snap?.expert_allowed ? 'qp-up' : 'qp-down'">{{ yesNo(snap?.expert_allowed) }}</b></div>
      </div>
    </div>

    <!-- 交易状态（真实数据） -->
    <div class="qp-panel bc__card">
      <div class="qp-panel__title">
        交易状态
        <span class="qp-live-badge">真实数据</span>
      </div>
      <div class="bc__rows">
        <div class="bc__row"><span>账户状态</span><b class="qp-warn">{{ account?.status || '—' }}</b></div>
        <div class="bc__row"><span>持仓数量</span><b>{{ snap?.open_positions ?? '—' }}</b></div>
        <div class="bc__row"><span>EA 版本</span><b>{{ snap?.ea_version || '—' }}</b></div>
        <div class="bc__row"><span>终端版本</span><b>{{ snap?.terminal_version || '—' }}</b></div>
      </div>
    </div>

    <!-- 账户信息（真实数据） -->
    <div class="qp-panel bc__card">
      <div class="qp-panel__title">
        账户信息
        <span class="qp-live-badge">真实数据</span>
      </div>
      <div class="bc__rows">
        <div class="bc__row"><span>账户模式</span><b>{{ account?.account_mode || '—' }}</b></div>
        <div class="bc__row"><span>杠杆</span><b>1:{{ account?.leverage ?? '—' }}</b></div>
        <div class="bc__row"><span>经纪商</span><b class="bc__ellip">{{ account?.company || account?.server || '—' }}</b></div>
        <div class="bc__row"><span>账号</span><b>{{ account?.login_ref || '—' }}</b></div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.bc {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 10px;
  margin-top: 12px;
}
.bc__card { gap: 0; }
.bc__rows { display: flex; flex-direction: column; gap: 6px; }
.bc__row {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 11.5px;
  padding: 3px 0;
  border-bottom: 1px solid rgba(148, 163, 184, 0.07);
}
.bc__row:last-child { border-bottom: none; }
.bc__row span { color: var(--qp-text-dim); white-space: nowrap; }
.bc__row b { font-weight: 600; text-align: right; }
.bc__ellip { max-width: 130px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.qp-warn { color: var(--qp-warn); }

.bc__news-row {
  display: flex;
  justify-content: space-between;
  font-size: 11.5px;
  padding: 3px 0;
}
.bc__news-row span { color: var(--qp-text-dim); }

.bc__countdown {
  margin-top: 8px;
  text-align: center;
  border: 1px solid var(--qp-border-soft);
  border-radius: 8px;
  padding: 8px;
  background: rgba(245, 158, 11, 0.05);
}
.bc__countdown-label { font-size: 9px; letter-spacing: 1px; color: var(--qp-text-dim); }
.bc__countdown-time { font-size: 20px; font-weight: 700; color: var(--qp-warn); font-variant-numeric: tabular-nums; }
.bc__countdown-name { font-size: 10px; color: var(--qp-text-dim); }
.bc__dim { opacity: 0.65; }

.bc__imp {
  display: inline-block;
  padding: 0 5px;
  margin-right: 4px;
  border-radius: 4px;
  font-size: 9px;
  font-weight: 700;
  line-height: 14px;
  vertical-align: 1px;
  color: var(--qp-text);
  background: rgba(148, 163, 184, 0.18);
}
.bc__imp--high { background: rgba(239, 68, 68, 0.25); }
.bc__imp--moderate { background: rgba(245, 158, 11, 0.25); }
.bc__imp-inline { font-weight: 700; margin: 0 3px; }
.bc__imp-inline.bc__imp--high, .bc__imp--high.bc__imp-inline { color: var(--qp-down); }
.bc__imp-inline.bc__imp--moderate { color: var(--qp-warn); }
.bc__event-later {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 10px;
  color: var(--qp-text-dim);
}

.bc__shield {
  text-align: center;
  padding: 10px 6px;
  border-radius: 8px;
  margin-bottom: 10px;
  border: 1px solid rgba(239, 68, 68, 0.4);
  background: rgba(239, 68, 68, 0.06);
}
.bc__shield.is-ok {
  border-color: rgba(34, 197, 94, 0.4);
  background: rgba(34, 197, 94, 0.06);
}
.bc__shield-icon { font-size: 26px; margin-bottom: 4px; }
.bc__shield-text { font-size: 10.5px; font-weight: 700; letter-spacing: 0.6px; color: var(--qp-text); }

@media (max-width: 1400px) {
  .bc { grid-template-columns: repeat(3, 1fr); }
}
</style>
