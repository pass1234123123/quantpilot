<script setup lang="ts">
import { computed, ref } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'
import { api, num, type BacktestJobDetail } from '../../api'

const store = useDashboardStore()

const jobs = computed(() => store.backtests)
const selected = ref<BacktestJobDetail | null>(null)
const loadingDetail = ref(false)

const timeText = (iso: string) =>
  new Date(iso).toLocaleString('zh-CN', { hour12: false })

const statusClass = (s: string) =>
  s === 'COMPLETED' ? 'is-done' : s === 'FAILED' ? 'is-fail' : 'is-run'

async function openDetail(jobId: string) {
  if (selected.value?.job_id === jobId) {
    selected.value = null
    return
  }
  loadingDetail.value = true
  try {
    selected.value = await api.backtest(jobId)
  } catch (e) {
    store.error = e instanceof Error ? e.message : String(e)
    selected.value = null
  } finally {
    loadingDetail.value = false
  }
}

const money = (v: string | null | undefined, signed = false) => {
  const n = num(v ?? null)
  if (n === null) return '—'
  const s = n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  return (signed && n > 0 ? '+' : '') + s
}

const ratioText = (v: string | number | null | undefined) => {
  if (v === null || v === undefined) return '—'
  const n = typeof v === 'number' ? v : num(v)
  return n === null ? '—' : n.toLocaleString('en-US', { maximumFractionDigits: 4 })
}

const pct = (v: number | null | undefined) =>
  v === null || v === undefined ? '—' : `${(v * 100).toFixed(1)}%`

const netUp = computed(() => ((num(selected.value?.metrics?.net_profit) ?? 0) >= 0))
</script>

<template>
  <div class="qp-panel bt">
    <div class="qp-panel__title">
      历史回测 · 任务与报告
      <span class="qp-live-badge">backtest_jobs 真实数据</span>
    </div>

    <div v-if="jobs.length === 0" class="bt__empty">
      暂无回测任务 —— 通过 POST /api/v1/backtests 创建（需 Idempotency-Key）。
    </div>

    <table v-else class="bt__table">
      <thead>
        <tr>
          <th>创建时间（本地）</th>
          <th>策略</th>
          <th>品种/周期</th>
          <th>区间</th>
          <th>手数</th>
          <th>状态</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="j in jobs"
          :key="j.job_id"
          :class="{ 'is-selected': selected?.job_id === j.job_id }"
          @click="openDetail(j.job_id)"
        >
          <td class="bt__dim">{{ timeText(j.created_at) }}</td>
          <td>{{ j.strategy_code }}</td>
          <td>{{ j.broker_symbol }} / {{ j.timeframe }}</td>
          <td class="bt__dim">{{ timeText(j.range_start) }} → {{ timeText(j.range_end) }}</td>
          <td>{{ j.volume }}</td>
          <td>
            <span class="bt__status" :class="statusClass(j.status)">{{ j.status }}</span>
          </td>
          <td class="bt__dim">{{ selected?.job_id === j.job_id ? '收起' : '详情' }}</td>
        </tr>
      </tbody>
    </table>

    <div v-if="loadingDetail" class="bt__empty">加载报告中…</div>

    <div v-if="selected?.metrics" class="bt__detail">
      <div class="bt__detail-head">
        报告 · {{ selected.strategy_code }} v{{ selected.strategy_version }} ·
        data_hash <code>{{ selected.data_hash?.slice(0, 12) }}…</code> ·
        rev {{ selected.config_revision }}
      </div>

      <div class="bt__metrics">
        <div class="bt__metric">
          <span>净利润</span>
          <b :class="netUp ? 'qp-up' : 'qp-down'">
            {{ money(selected.metrics.net_profit, true) }}
          </b>
        </div>
        <div class="bt__metric">
          <span>期末余额</span>
          <b>{{ money(selected.metrics.final_balance) }}</b>
        </div>
        <div class="bt__metric">
          <span>最大回撤</span>
          <b>{{ money(selected.metrics.max_drawdown) }}（{{ pct(selected.metrics.max_drawdown_pct) }}）</b>
        </div>
        <div class="bt__metric">
          <span>Profit Factor</span>
          <b>{{ ratioText(selected.metrics.profit_factor) }}</b>
        </div>
        <div class="bt__metric">
          <span>胜率</span>
          <b>{{ pct(selected.metrics.win_rate) }}</b>
        </div>
        <div class="bt__metric">
          <span>期望值</span>
          <b>{{ money(selected.metrics.expectancy) }}</b>
        </div>
        <div class="bt__metric">
          <span>Sharpe</span>
          <b>{{ ratioText(selected.metrics.sharpe) }}</b>
        </div>
        <div class="bt__metric">
          <span>Sortino</span>
          <b>{{ ratioText(selected.metrics.sortino) }}</b>
        </div>
        <div class="bt__metric">
          <span>交易数</span>
          <b>{{ selected.metrics.trade_count }}（{{ selected.metrics.winning_trades }}胜 / {{ selected.metrics.losing_trades }}负）</b>
        </div>
        <div class="bt__metric">
          <span>持仓暴露</span>
          <b>{{ pct(selected.metrics.exposure) }}</b>
        </div>
        <div class="bt__metric">
          <span>平均持仓</span>
          <b>{{ ratioText(selected.metrics.average_holding_minutes) }} 分钟</b>
        </div>
        <div class="bt__metric">
          <span>成本合计</span>
          <b>{{ money(selected.metrics.cost_breakdown.spread) }} 点差 · {{ money(selected.metrics.cost_breakdown.slippage) }} 滑点 · {{ money(selected.metrics.cost_breakdown.commission) }} 佣金 · {{ money(selected.metrics.cost_breakdown.swap) }} swap</b>
        </div>
      </div>

      <div v-if="selected.error" class="bt__fail">任务错误：{{ selected.error }}</div>

      <div class="bt__note">
        研究假设：信号 bar 收盘生成，下一根 bar 开盘全量成交；SL/TP 同根保守止损优先。
        回测不创建交易命令，不影响 OBSERVE_ONLY。
      </div>
    </div>
  </div>
</template>

<style scoped>
.bt { display: flex; flex-direction: column; gap: 10px; }

.bt__empty {
  padding: 26px 10px;
  text-align: center;
  font-size: 12px;
  color: var(--qp-text-dim);
}

.bt__table { width: 100%; border-collapse: collapse; font-size: 11.5px; }
.bt__table th {
  text-align: left;
  font-size: 10px;
  letter-spacing: 0.8px;
  color: var(--qp-text-dim);
  font-weight: 600;
  padding: 6px 10px;
  border-bottom: 1px solid var(--qp-border);
  white-space: nowrap;
}
.bt__table td {
  padding: 7px 10px;
  border-bottom: 1px solid rgba(148, 163, 184, 0.08);
  vertical-align: top;
}
.bt__table tbody tr { cursor: pointer; }
.bt__table tbody tr:hover td { background: rgba(148, 163, 184, 0.04); }
.bt__table tr.is-selected td { background: rgba(0, 176, 192, 0.07); }
.bt__dim { color: var(--qp-text-dim); white-space: nowrap; font-variant-numeric: tabular-nums; }

.bt__status {
  display: inline-block;
  padding: 1px 7px;
  border-radius: 4px;
  font-size: 9.5px;
  font-weight: 700;
}
.bt__status.is-done { color: var(--qp-up); background: rgba(34, 197, 94, 0.14); }
.bt__status.is-fail { color: var(--qp-down); background: rgba(239, 68, 68, 0.16); }
.bt__status.is-run { color: #38bdf8; background: rgba(56, 189, 248, 0.14); }

.bt__detail {
  border: 1px solid var(--qp-border-soft);
  border-radius: 8px;
  padding: 12px;
  background: rgba(148, 163, 184, 0.04);
}
.bt__detail-head { font-size: 11px; color: var(--qp-text-dim); margin-bottom: 10px; }
.bt__detail-head code { font-size: 10.5px; }

.bt__metrics {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
  gap: 8px 16px;
}
.bt__metric {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  font-size: 11.5px;
  padding: 4px 0;
  border-bottom: 1px solid rgba(148, 163, 184, 0.08);
}
.bt__metric span { color: var(--qp-text-dim); }
.bt__metric b { font-variant-numeric: tabular-nums; text-align: right; }

.bt__fail {
  margin-top: 10px;
  font-size: 11px;
  color: var(--qp-down);
}

.bt__note {
  margin-top: 10px;
  font-size: 10px;
  color: var(--qp-text-dim);
}
</style>
