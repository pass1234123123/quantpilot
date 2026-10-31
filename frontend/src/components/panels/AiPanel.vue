<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'
import type { AiAnalysisDoc } from '../../api'

const store = useDashboardStore()

const TIMEFRAMES = ['M5', 'M15', 'M30', 'H1', 'H4'] as const

const REGIME_META: Record<string, { label: string; cls: string }> = {
  TREND_UP: { label: '上升趋势', cls: 'is-up' },
  TREND_DOWN: { label: '下降趋势', cls: 'is-down' },
  RANGE: { label: '区间震荡', cls: 'is-range' },
  HIGH_VOL: { label: '高波动', cls: 'is-vol' },
  UNKNOWN: { label: '未知', cls: 'is-unknown' },
}

// ---- 触发表单：品种优先取策略实例绑定的（有 bar 数据），退回行情列表 ----
const symbolOptions = computed(() => {
  const set = new Set<string>()
  store.instances.forEach((i) => set.add(i.broker_symbol))
  store.symbols.forEach((s) => set.add(s.broker_symbol))
  return [...set]
})

const formSymbol = ref('')
const formTimeframe = ref<string>('M15')

watch(
  () => [store.selectedAccountId, store.instances.length],
  () => {
    const inst = store.instances[0]
    if (inst) {
      formSymbol.value = inst.broker_symbol
      formTimeframe.value = inst.timeframe
    } else if (!formSymbol.value && symbolOptions.value.length > 0) {
      formSymbol.value = symbolOptions.value[0]
    }
  },
  { immediate: true },
)

const aiMode = computed(() => store.config?.config?.ai?.mode ?? 'ADVISORY')
const modeIsOff = computed(() => aiMode.value === 'OFF')

const lastResult = ref<AiAnalysisDoc | null>(null)
const offNotice = ref(false)

async function runAnalysis() {
  if (!formSymbol.value || !formTimeframe.value) return
  offNotice.value = false
  const result = await store.runAiAnalysis(formSymbol.value, formTimeframe.value)
  if (result) {
    if (result.ai_mode === 'OFF' || result.analysis === null) {
      offNotice.value = true
    } else {
      lastResult.value = result.analysis
    }
  }
}

// ---- 历史列表与详情（点击行切换，未选时展示最新一条） ----
const selectedAnalysisId = ref<string | null>(null)

const analyses = computed(() => store.aiAnalyses)

const selectedDoc = computed<AiAnalysisDoc | null>(() => {
  const sel = analyses.value.find((a) => a.analysis_id === selectedAnalysisId.value)
  if (sel) return sel.output_json
  const latest = lastResult.value ?? analyses.value[0]?.output_json ?? null
  return latest
})

const selectedIsFromHistory = computed(() =>
  analyses.value.some((a) => a.analysis_id === selectedAnalysisId.value),
)

function toggleRow(analysisId: string) {
  selectedAnalysisId.value = selectedAnalysisId.value === analysisId ? null : analysisId
  lastResult.value = null
}

const timeText = (iso: string) =>
  new Date(iso).toLocaleString('zh-CN', { hour12: false })

const regimeMeta = (regime: string) => REGIME_META[regime] ?? REGIME_META.UNKNOWN

const confPct = (v: number) => `${(v * 100).toFixed(1)}%`

const decisionClass = (d: string) => (d === 'ALLOW' ? 'is-allow' : 'is-review')
</script>

<template>
  <div class="qp-panel ai">
    <div class="qp-panel__title">
      AI 分析中心 · 市场状态与策略适用性
      <span class="qp-live-badge">ai_analyses 真实数据</span>
    </div>

    <div class="ai__mode">
      <span>运行模式（doc 8.5）</span>
      <b :class="modeIsOff ? 'is-off' : 'is-advisory'">{{ aiMode }}</b>
      <span class="ai__mode-note">
        ADVISORY 仅建议展示，不参与交易许可；AI 永不绕过云端风控闸门。
      </span>
    </div>

    <div class="ai__trigger">
      <el-select
        v-model="formSymbol"
        placeholder="品种"
        size="small"
        filterable
        style="width: 160px"
      >
        <el-option v-for="s in symbolOptions" :key="s" :value="s" :label="s" />
      </el-select>
      <el-select v-model="formTimeframe" size="small" style="width: 100px">
        <el-option v-for="tf in TIMEFRAMES" :key="tf" :value="tf" :label="tf" />
      </el-select>
      <el-button
        size="small"
        type="primary"
        :loading="store.aiRunning"
        :disabled="modeIsOff"
        @click="runAnalysis"
      >
        运行分析
      </el-button>
      <span v-if="modeIsOff" class="ai__off-tip">
        当前账户 AI 模式为 OFF：不运行分析（可在账户配置中改回 ADVISORY）。
      </span>
      <span v-else class="ai__hint">对最新收盘 bar 触发一次分析；相同特征快照幂等返回。</span>
    </div>

    <div v-if="offNotice" class="ai__off-notice">
      本次未生成分析：账户 AI 模式为 OFF。
    </div>

    <div v-if="selectedDoc" class="ai__detail">
      <div class="ai__detail-head">
        {{ selectedIsFromHistory ? '历史分析' : '最新分析' }} ·
        {{ selectedDoc.model_provider }} · {{ selectedDoc.model_id }} ·
        {{ selectedDoc.prompt_version }} · bar {{ timeText(selectedDoc.source_bar_time) }}
      </div>

      <div class="ai__regime-row">
        <span class="ai__regime" :class="regimeMeta(selectedDoc.market_regime).cls">
          {{ selectedDoc.market_regime }} · {{ regimeMeta(selectedDoc.market_regime).label }}
        </span>
        <span class="ai__conf">
          置信度
          <i>{{ confPct(selectedDoc.regime_confidence) }}</i>
        </span>
        <span class="ai__decision" :class="decisionClass(selectedDoc.signal_decision)">
          {{ selectedDoc.signal_decision === 'ALLOW' ? '信号建议：允许' : '信号建议：需复核' }}
        </span>
        <span
          v-for="f in selectedDoc.risk_flags"
          :key="f"
          class="ai__flag"
        >
          {{ f }}
        </span>
      </div>

      <div class="ai__explanation">{{ selectedDoc.explanation }}</div>

      <div v-if="selectedDoc.strategy_scores.length > 0" class="ai__scores">
        <table class="ai__scores-table">
          <thead>
            <tr>
              <th>策略（ENABLED 实例）</th>
              <th>regime 适用评分</th>
              <th>原因码</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="s in selectedDoc.strategy_scores" :key="s.strategy_id">
              <td>{{ s.strategy_id }}</td>
              <td class="ai__score-cell">
                <i class="ai__score-bar">
                  <b :style="{ width: `${Math.round(s.score * 100)}%` }" />
                </i>
                {{ s.score.toFixed(2) }}
              </td>
              <td class="ai__dim">{{ s.reason_codes.join(' / ') || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-else class="ai__explanation ai__dim">
        该品种/周期下没有 ENABLED 策略实例，无策略适用评分。
      </div>

      <div class="ai__prov">
        input <code>{{ selectedDoc.input_snapshot_id.slice(0, 19) }}…</code> ·
        耗时 {{ selectedDoc.duration_ms }}ms · 成本 {{ selectedDoc.cost }} ·
        生成 {{ timeText(selectedDoc.created_at) }} · 失效 {{ timeText(selectedDoc.expires_at) }}
      </div>
    </div>

    <div v-if="analyses.length === 0" class="ai__empty">
      暂无分析记录 —— 选择品种与周期后点击「运行分析」，或等待 EA 上报 bar 触发 bar 级分析。
    </div>

    <table v-else class="ai__table">
      <thead>
        <tr>
          <th>生成时间（本地）</th>
          <th>品种/周期</th>
          <th>市场状态</th>
          <th>置信度</th>
          <th>信号建议</th>
          <th>源 bar</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="a in analyses"
          :key="a.id"
          :class="{ 'is-selected': selectedDoc?.analysis_id === a.analysis_id }"
          @click="toggleRow(a.analysis_id)"
        >
          <td class="ai__dim">{{ timeText(a.created_at) }}</td>
          <td>{{ a.broker_symbol }} / {{ a.timeframe }}</td>
          <td>
            <span class="ai__regime-sm" :class="regimeMeta(a.output_json.market_regime).cls">
              {{ a.output_json.market_regime }}
            </span>
          </td>
          <td>{{ confPct(a.output_json.regime_confidence) }}</td>
          <td>
            <span :class="decisionClass(a.output_json.signal_decision)">
              {{ a.output_json.signal_decision }}
            </span>
          </td>
          <td class="ai__dim">{{ timeText(a.output_json.source_bar_time) }}</td>
          <td class="ai__dim">
            {{ selectedDoc?.analysis_id === a.analysis_id ? '收起' : '详情' }}
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.ai { display: flex; flex-direction: column; gap: 12px; }

.ai__mode {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 11.5px;
  color: var(--qp-text-dim);
}
.ai__mode b {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 4px;
  font-size: 10.5px;
  letter-spacing: 0.6px;
}
.ai__mode b.is-advisory { color: var(--qp-accent); background: rgba(0, 176, 192, 0.14); }
.ai__mode b.is-off { color: var(--qp-text-dim); background: rgba(148, 163, 184, 0.14); }
.ai__mode-note { font-size: 10.5px; }

.ai__trigger { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.ai__hint, .ai__off-tip { font-size: 10.5px; color: var(--qp-text-dim); }

.ai__off-notice {
  font-size: 11px;
  color: var(--qp-down);
  padding: 8px 10px;
  border: 1px solid rgba(239, 68, 68, 0.25);
  border-radius: 6px;
}

.ai__detail {
  border: 1px solid var(--qp-border-soft);
  border-radius: 8px;
  padding: 12px;
  background: rgba(148, 163, 184, 0.04);
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.ai__detail-head { font-size: 11px; color: var(--qp-text-dim); }
.ai__detail-head code { font-size: 10.5px; }

.ai__regime-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }

.ai__regime {
  display: inline-block;
  padding: 3px 10px;
  border-radius: 5px;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.5px;
}
.ai__regime.is-up { color: var(--qp-up); background: rgba(34, 197, 94, 0.14); }
.ai__regime.is-down { color: var(--qp-down); background: rgba(239, 68, 68, 0.14); }
.ai__regime.is-range { color: #38bdf8; background: rgba(56, 189, 248, 0.14); }
.ai__regime.is-vol { color: #fbbf24; background: rgba(251, 191, 36, 0.14); }
.ai__regime.is-unknown { color: var(--qp-text-dim); background: rgba(148, 163, 184, 0.14); }

.ai__regime-sm {
  display: inline-block;
  padding: 1px 7px;
  border-radius: 4px;
  font-size: 9.5px;
  font-weight: 700;
}
.ai__regime-sm.is-up { color: var(--qp-up); background: rgba(34, 197, 94, 0.14); }
.ai__regime-sm.is-down { color: var(--qp-down); background: rgba(239, 68, 68, 0.14); }
.ai__regime-sm.is-range { color: #38bdf8; background: rgba(56, 189, 248, 0.14); }
.ai__regime-sm.is-vol { color: #fbbf24; background: rgba(251, 191, 36, 0.14); }
.ai__regime-sm.is-unknown { color: var(--qp-text-dim); background: rgba(148, 163, 184, 0.14); }

.ai__conf { font-size: 11.5px; color: var(--qp-text-dim); }
.ai__conf i { font-style: normal; font-weight: 700; color: var(--qp-text); font-variant-numeric: tabular-nums; }

.ai__decision {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 4px;
}
.ai__decision.is-allow { color: var(--qp-up); background: rgba(34, 197, 94, 0.12); }
.ai__decision.is-review { color: #fbbf24; background: rgba(251, 191, 36, 0.12); }

.ai__flag {
  font-size: 10px;
  color: #fbbf24;
  border: 1px solid rgba(251, 191, 36, 0.35);
  border-radius: 4px;
  padding: 1px 7px;
}

.ai__explanation { font-size: 11.5px; line-height: 1.6; }

.ai__scores-table { width: 100%; border-collapse: collapse; font-size: 11.5px; }
.ai__scores-table th {
  text-align: left;
  font-size: 10px;
  letter-spacing: 0.8px;
  color: var(--qp-text-dim);
  font-weight: 600;
  padding: 6px 10px;
  border-bottom: 1px solid var(--qp-border);
  white-space: nowrap;
}
.ai__scores-table td {
  padding: 6px 10px;
  border-bottom: 1px solid rgba(148, 163, 184, 0.08);
  vertical-align: middle;
}
.ai__score-cell { display: flex; align-items: center; gap: 8px; font-variant-numeric: tabular-nums; }
.ai__score-bar {
  flex: 0 0 90px;
  height: 6px;
  border-radius: 3px;
  background: rgba(148, 163, 184, 0.15);
  overflow: hidden;
  display: inline-block;
}
.ai__score-bar b { display: block; height: 100%; background: var(--qp-accent); border-radius: 3px; }

.ai__prov { font-size: 10px; color: var(--qp-text-dim); }
.ai__prov code { font-size: 10px; }

.ai__empty {
  padding: 26px 10px;
  text-align: center;
  font-size: 12px;
  color: var(--qp-text-dim);
}

.ai__table { width: 100%; border-collapse: collapse; font-size: 11.5px; }
.ai__table th {
  text-align: left;
  font-size: 10px;
  letter-spacing: 0.8px;
  color: var(--qp-text-dim);
  font-weight: 600;
  padding: 6px 10px;
  border-bottom: 1px solid var(--qp-border);
  white-space: nowrap;
}
.ai__table td {
  padding: 7px 10px;
  border-bottom: 1px solid rgba(148, 163, 184, 0.08);
  vertical-align: top;
}
.ai__table tbody tr { cursor: pointer; }
.ai__table tbody tr:hover td { background: rgba(148, 163, 184, 0.04); }
.ai__table tr.is-selected td { background: rgba(0, 176, 192, 0.07); }
.ai__dim { color: var(--qp-text-dim); white-space: nowrap; font-variant-numeric: tabular-nums; }
</style>
