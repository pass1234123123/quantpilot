<script setup lang="ts">
import { computed } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'

const store = useDashboardStore()

const signals = computed(() => store.signals)
const instanceCode = computed(() => {
  const map = new Map<string, string>()
  for (const i of store.instances) map.set(i.instance_id, i.strategy_code)
  return map
})

const timeText = (iso: string) =>
  new Date(iso).toLocaleString('zh-CN', { hour12: false })

const codeOf = (id: string) => instanceCode.value.get(id) ?? '—'
</script>

<template>
  <div class="qp-panel sg">
    <div class="qp-panel__title">
      交易信号 · 云端策略引擎
      <span class="qp-live-badge">strategy_signals 真实数据</span>
    </div>

    <div v-if="signals.length === 0" class="sg__empty">
      暂无信号 —— 收盘 bar 落库后由策略引擎评估产生（边沿触发 + 去重，最近 100 条）。
    </div>

    <table v-else class="sg__table">
      <thead>
        <tr>
          <th>生成时间（本地）</th>
          <th>策略</th>
          <th>方向</th>
          <th>强度</th>
          <th>参考价</th>
          <th>建议 SL</th>
          <th>建议 TP</th>
          <th>触发原因</th>
          <th>来源 Bar</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="s in signals" :key="s.id">
          <td class="sg__dim">{{ timeText(s.generated_at) }}</td>
          <td>{{ codeOf(s.strategy_instance_id) }}</td>
          <td>
            <span class="sg__dir" :class="s.signal_json.direction === 'BUY' ? 'is-buy' : 'is-sell'">
              {{ s.signal_json.direction }}
            </span>
          </td>
          <td>{{ s.signal_json.strength }}</td>
          <td>{{ s.signal_json.reference_price ?? '—' }}</td>
          <td>{{ s.signal_json.proposed_stop_loss ?? '—' }}</td>
          <td>{{ s.signal_json.proposed_take_profit ?? '—' }}</td>
          <td class="sg__dim">{{ (s.signal_json.reason_codes ?? []).join(' · ') || '—' }}</td>
          <td class="sg__dim">{{ timeText(s.source_bar_time) }}</td>
        </tr>
      </tbody>
    </table>

    <div class="sg__foot">
      信号仅为建议（PRD 7.1：策略不直接下单）；是否生成命令由账户 automation 开关与风控闸门决定
    </div>
  </div>
</template>

<style scoped>
.sg { display: flex; flex-direction: column; gap: 10px; }

.sg__empty {
  padding: 26px 10px;
  text-align: center;
  font-size: 12px;
  color: var(--qp-text-dim);
}

.sg__table { width: 100%; border-collapse: collapse; font-size: 11.5px; }
.sg__table th {
  text-align: left;
  font-size: 10px;
  letter-spacing: 0.8px;
  color: var(--qp-text-dim);
  font-weight: 600;
  padding: 6px 10px;
  border-bottom: 1px solid var(--qp-border);
  white-space: nowrap;
}
.sg__table td {
  padding: 7px 10px;
  border-bottom: 1px solid rgba(148, 163, 184, 0.08);
  vertical-align: top;
}
.sg__table tr:hover td { background: rgba(148, 163, 184, 0.04); }
.sg__dim { color: var(--qp-text-dim); white-space: nowrap; font-variant-numeric: tabular-nums; }

.sg__dir {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 4px;
  font-size: 9.5px;
  font-weight: 700;
}
.sg__dir.is-buy { color: var(--qp-up); background: rgba(34, 197, 94, 0.14); }
.sg__dir.is-sell { color: var(--qp-down); background: rgba(239, 68, 68, 0.16); }

.sg__foot {
  font-size: 10px;
  color: var(--qp-text-dim);
  border-top: 1px solid var(--qp-border-soft);
  padding-top: 8px;
}
</style>
