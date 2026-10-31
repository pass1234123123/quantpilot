<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { createChart, LineSeries, type IChartApi, type UTCTimestamp } from 'lightweight-charts'
import { useDashboardStore } from '../../stores/dashboard'
import { api, canResearch, num, type BacktestJobDetail } from '../../api'

const store = useDashboardStore()

// Role gating is INTERACTION-only; the backend re-checks on every call.
const researchable = computed(() => canResearch())

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

const costText = (field: string) => {
  const cm = (selected.value?.cost_model ?? {}) as Record<string, unknown>
  const v = cm[field]
  return v === null || v === undefined ? '—' : String(v)
}

// ---- 创建表单（PRD 15：幂等键客户端生成；显式成本假设，默认非零） ----
const formInstanceId = ref('')
const formRange = ref<[Date, Date]>([
  new Date(Date.now() - 7 * 24 * 60 * 60 * 1000),
  new Date(),
])
const formBalance = ref(10000)
const formVolume = ref(1)

/** 参考主流 ECN 欧元账户的保守默认假设，均可在表单中修改 */
const formCost = ref({
  spread_per_side: '0.00006',
  slippage_per_side: '0.00002',
  commission_per_lot_per_side: '3.5',
  swap_per_lot_per_bar: '0.01',
  contract_size: '100000',
  quote_to_account_rate: '1',
})

watch(
  () => [store.selectedAccountId, store.instances],
  () => {
    if (!formInstanceId.value || !store.instances.some((i) => i.instance_id === formInstanceId.value)) {
      formInstanceId.value = store.instances[0]?.instance_id ?? ''
    }
  },
  { immediate: true },
)

const allCostsZero = computed(() => {
  const c = formCost.value
  return ['spread_per_side', 'slippage_per_side', 'commission_per_lot_per_side', 'swap_per_lot_per_bar']
    .every((k) => num(c[k as keyof typeof c]) === 0)
})

const createMsg = ref('')
const creating = computed(() => store.backtestCreating)

async function submitCreate() {
  createMsg.value = ''
  if (!formInstanceId.value || !formRange.value?.[0] || !formRange.value?.[1]) {
    createMsg.value = '请选择策略实例和回测区间。'
    return
  }
  const detail = await store.createBacktest({
    strategy_instance_id: formInstanceId.value,
    range_start: formRange.value[0].toISOString(),
    range_end: formRange.value[1].toISOString(),
    initial_balance: String(formBalance.value),
    volume: String(formVolume.value),
    cost_model: { ...formCost.value },
  })
  if (detail) {
    selected.value = detail
    createMsg.value = `任务已创建：${detail.job_id}（${detail.status}）`
  } else {
    createMsg.value = `创建失败：${store.error}`
  }
}

// ---- 权益曲线（backtest report.equity_curve） ----
const chartEl = ref<HTMLElement | null>(null)
let chart: IChartApi | null = null
let equitySeries: ReturnType<IChartApi['addSeries']> | null = null

function toTime(iso: string): UTCTimestamp {
  return Math.floor(new Date(iso).getTime() / 1000) as UTCTimestamp
}

function renderEquity() {
  if (!chart || !equitySeries) return
  const curve = selected.value?.report?.equity_curve ?? []
  const points = curve
    .map((p) => ({ time: toTime(p.bar_time), value: num(p.equity) ?? 0 }))
    .filter((p, i, arr) => i === 0 || p.time > arr[i - 1].time)
  equitySeries.setData(points)
  if (points.length > 0) chart.timeScale().fitContent()
}

onMounted(() => {
  if (!chartEl.value) return
  chart = createChart(chartEl.value, {
    height: 220,
    layout: {
      background: { color: 'transparent' },
      textColor: '#7d8fa3',
      fontSize: 10,
    },
    grid: {
      vertLines: { color: 'rgba(148, 163, 184, 0.08)' },
      horzLines: { color: 'rgba(148, 163, 184, 0.08)' },
    },
    rightPriceScale: { borderColor: 'rgba(148, 163, 184, 0.2)' },
    timeScale: { borderColor: 'rgba(148, 163, 184, 0.2)', timeVisible: true },
    autoSize: false,
  })
  equitySeries = chart.addSeries(LineSeries, {
    color: '#22c55e',
    lineWidth: 2,
    title: '权益',
  })
  renderEquity()
})

watch(
  () => selected.value?.report?.equity_curve,
  () => renderEquity(),
)

onBeforeUnmount(() => {
  chart?.remove()
  chart = null
  equitySeries = null
})
</script>

<template>
  <div class="qp-panel bt">
    <div class="qp-panel__title">
      历史回测 · 创建 · 任务与报告
      <span class="qp-live-badge">backtest_jobs 真实数据</span>
    </div>

    <!-- 创建表单 -->
    <div class="bt__form">
      <div class="bt__form-row">
        <el-select
          v-model="formInstanceId"
          placeholder="策略实例"
          size="small"
          filterable
          style="width: 300px"
        >
          <el-option
            v-for="i in store.instances"
            :key="i.instance_id"
            :value="i.instance_id"
            :label="`${i.strategy_code} · ${i.broker_symbol} · ${i.timeframe} · ${i.status}`"
          />
        </el-select>
        <el-date-picker
          v-model="formRange"
          type="datetimerange"
          size="small"
          range-separator="→"
          start-placeholder="开始"
          end-placeholder="结束"
          style="width: 360px"
        />
        <span class="bt__field">
          初始资金
          <el-input-number v-model="formBalance" :min="1" :step="1000" size="small" style="width: 130px" />
        </span>
        <span class="bt__field">
          手数
          <el-input-number v-model="formVolume" :min="0.01" :step="0.01" size="small" style="width: 110px" />
        </span>
      </div>

      <div class="bt__cost">
        <div class="bt__cost-title">
          成本假设（显式研究假设；价格类为每边报价单位，往返 ×2）
        </div>
        <div class="bt__cost-grid">
          <label class="bt__cost-item">
            <span>点差/边</span>
            <el-input v-model="formCost.spread_per_side" size="small" />
          </label>
          <label class="bt__cost-item">
            <span>滑点/边</span>
            <el-input v-model="formCost.slippage_per_side" size="small" />
          </label>
          <label class="bt__cost-item">
            <span>佣金（货币/手/边）</span>
            <el-input v-model="formCost.commission_per_lot_per_side" size="small" />
          </label>
          <label class="bt__cost-item">
            <span>隔夜利息（货币/手/bar）</span>
            <el-input v-model="formCost.swap_per_lot_per_bar" size="small" />
          </label>
          <label class="bt__cost-item">
            <span>合约大小</span>
            <el-input v-model="formCost.contract_size" size="small" />
          </label>
          <label class="bt__cost-item">
            <span>报价→账户汇率</span>
            <el-input v-model="formCost.quote_to_account_rate" size="small" />
          </label>
        </div>
        <div v-if="allCostsZero" class="bt__cost-warn">
          四项交易成本均为 0：回测结果将明显偏乐观。建议至少配置点差与佣金。
        </div>
      </div>

      <div class="bt__form-actions">
        <el-button
          type="primary"
          size="small"
          :loading="creating"
          :disabled="store.instances.length === 0 || !researchable"
          @click="submitCreate"
        >
          创建回测任务
        </el-button>
        <span v-if="store.instances.length === 0" class="bt__form-tip">
          当前账户没有策略实例，请先在策略引擎面板创建/绑定实例。
        </span>
        <span v-else class="bt__form-tip">同一幂等键重复提交返回原任务；不产生任何交易命令。</span>
      </div>
      <div v-if="createMsg" class="bt__create-msg" :class="{ 'is-fail': createMsg.startsWith('创建失败') }">
        {{ createMsg }}
      </div>
    </div>

    <div v-if="jobs.length === 0" class="bt__empty">
      暂无回测任务 —— 通过上方表单创建（POST /api/v1/backtests，需 Idempotency-Key）。
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
        rev {{ selected.config_revision }} ·
        成本假设 点差/边 {{ costText('spread_per_side') }} · 滑点/边 {{ costText('slippage_per_side') }} ·
        佣金 {{ costText('commission_per_lot_per_side') }} · swap/bar {{ costText('swap_per_lot_per_bar') }}
      </div>

      <div ref="chartEl" class="bt__chart" />

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

.bt__form {
  border: 1px solid var(--qp-border-soft);
  border-radius: 8px;
  padding: 12px;
  background: rgba(148, 163, 184, 0.04);
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.bt__form-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.bt__field {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  color: var(--qp-text-dim);
}

.bt__cost { display: flex; flex-direction: column; gap: 8px; }
.bt__cost-title { font-size: 11px; color: var(--qp-text-dim); }
.bt__cost-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 8px 14px;
}
.bt__cost-item {
  display: flex;
  flex-direction: column;
  gap: 3px;
  font-size: 10.5px;
  color: var(--qp-text-dim);
}
.bt__cost-warn {
  font-size: 11px;
  color: #fbbf24;
  border: 1px solid rgba(251, 191, 36, 0.3);
  border-radius: 6px;
  padding: 6px 10px;
  background: rgba(251, 191, 36, 0.06);
}

.bt__form-actions { display: flex; align-items: center; gap: 10px; }
.bt__form-tip { font-size: 10.5px; color: var(--qp-text-dim); }
.bt__create-msg { font-size: 11px; color: var(--qp-up); }
.bt__create-msg.is-fail { color: var(--qp-down); }

.bt__chart { width: 100%; }

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
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.bt__detail-head { font-size: 11px; color: var(--qp-text-dim); }
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
