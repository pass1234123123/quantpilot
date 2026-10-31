<script setup lang="ts">
import { computed } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'

const store = useDashboardStore()

const strategies = computed(() => store.strategies)
const instances = computed(() => store.instances)

const timeText = (iso: string) =>
  new Date(iso).toLocaleString('zh-CN', { hour12: false })

function toggle(i: { instance_id: string; status: 'ENABLED' | 'DISABLED' }) {
  void store.setInstanceStatus(i.instance_id, i.status === 'ENABLED' ? 'DISABLED' : 'ENABLED')
}
</script>

<template>
  <div class="qp-panel sp">
    <div class="qp-panel__title">
      策略引擎 · 目录与实例
      <span class="qp-live-badge">strategies / strategy_instances 真实数据</span>
    </div>

    <div class="sp__section">策略目录（固定版本）</div>
    <div class="sp__catalog">
      <div v-for="s in strategies" :key="s.strategy_version_id" class="sp__card">
        <div class="sp__card-head">
          <b>{{ s.code }}</b>
          <span class="sp__tag" :class="s.status === 'ACTIVE' ? 'is-on' : 'is-off'">{{ s.status }}</span>
        </div>
        <div class="sp__card-desc">{{ s.description ?? '—' }}</div>
        <div class="sp__card-meta">版本 {{ s.version }} · warmup 门槛见 config_schema</div>
      </div>
      <div v-if="strategies.length === 0" class="sp__empty">暂无策略目录数据</div>
    </div>

    <div class="sp__section">账户实例绑定（账户 + 策略 + 品种 + 周期唯一）</div>
    <table v-if="instances.length > 0" class="sp__table">
      <thead>
        <tr>
          <th>策略</th>
          <th>版本</th>
          <th>品种</th>
          <th>周期</th>
          <th>参数修订</th>
          <th>状态</th>
          <th>操作</th>
          <th>更新时间（本地）</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="i in instances" :key="i.instance_id">
          <td>{{ i.strategy_code }}</td>
          <td>{{ i.strategy_version }}</td>
          <td>{{ i.broker_symbol }}</td>
          <td>{{ i.timeframe }}</td>
          <td>rev {{ i.config_revision }}</td>
          <td>
            <span class="sp__tag" :class="i.status === 'ENABLED' ? 'is-on' : 'is-off'">{{ i.status }}</span>
          </td>
          <td>
            <button class="sp__btn" @click="toggle(i)">
              {{ i.status === 'ENABLED' ? '停用' : '启用' }}
            </button>
          </td>
          <td class="sp__dim">{{ timeText(i.updated_at) }}</td>
        </tr>
      </tbody>
    </table>
    <div v-else class="sp__empty">当前账户暂无策略实例（通过 API POST /strategy-instances 绑定）</div>

    <div class="sp__foot">
      信号为建议性（PRD 7.1），实例启停仅影响云端评估；EA 实际执行行为由其运行模式决定（见「设置」页）
    </div>
  </div>
</template>

<style scoped>
.sp { display: flex; flex-direction: column; gap: 10px; }

.sp__section {
  font-size: 10px;
  letter-spacing: 1px;
  color: var(--qp-text-dim);
  text-transform: uppercase;
}

.sp__catalog {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 10px;
}
.sp__card {
  border: 1px solid var(--qp-border-soft);
  border-radius: 8px;
  padding: 10px 12px;
  background: rgba(148, 163, 184, 0.04);
}
.sp__card-head { display: flex; justify-content: space-between; align-items: center; font-size: 13px; }
.sp__card-desc { font-size: 11px; color: var(--qp-text-dim); margin-top: 4px; }
.sp__card-meta { font-size: 10px; color: var(--qp-text-dim); margin-top: 6px; }

.sp__tag {
  padding: 1px 7px;
  border-radius: 4px;
  font-size: 9.5px;
  font-weight: 700;
}
.sp__tag.is-on { color: var(--qp-up); background: rgba(34, 197, 94, 0.14); }
.sp__tag.is-off { color: var(--qp-text-dim); background: rgba(148, 163, 184, 0.12); }

.sp__table { width: 100%; border-collapse: collapse; font-size: 11.5px; }
.sp__table th {
  text-align: left;
  font-size: 10px;
  letter-spacing: 0.8px;
  color: var(--qp-text-dim);
  font-weight: 600;
  padding: 6px 10px;
  border-bottom: 1px solid var(--qp-border);
  white-space: nowrap;
}
.sp__table td {
  padding: 7px 10px;
  border-bottom: 1px solid rgba(148, 163, 184, 0.08);
  vertical-align: top;
}
.sp__table tr:hover td { background: rgba(148, 163, 184, 0.04); }
.sp__dim { color: var(--qp-text-dim); white-space: nowrap; font-variant-numeric: tabular-nums; }

.sp__btn {
  font-size: 10.5px;
  padding: 2px 10px;
  border-radius: 5px;
  border: 1px solid var(--qp-border-soft);
  background: rgba(0, 176, 192, 0.08);
  color: var(--qp-text);
  cursor: pointer;
}
.sp__btn:hover { background: rgba(0, 176, 192, 0.16); }

.sp__empty {
  padding: 20px 10px;
  text-align: center;
  font-size: 12px;
  color: var(--qp-text-dim);
}

.sp__foot {
  font-size: 10px;
  color: var(--qp-text-dim);
  border-top: 1px solid var(--qp-border-soft);
  padding-top: 8px;
}
</style>
