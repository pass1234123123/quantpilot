<script setup lang="ts">
import { computed } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'
import { canWrite } from '../../api'

const store = useDashboardStore()

// Role gating is INTERACTION-only; the backend re-checks on every call.
const writable = computed(() => canWrite())

const config = computed(() => store.config)

// 云端 risk_profiles 配置（OBSERVE_ONLY：本切片仅存储与展示，EA 端只记录不执行）
const filters = computed(() => config.value?.config.filters ?? null)

const switches: { key: string; label: string }[] = [
  { key: 'newsFilter', label: '新闻' },
  { key: 'highImpactOnly', label: '高影响事件' },
  { key: 'sessionFilter', label: '交易时段' },
  { key: 'maxSpread', label: '最大点差' },
  { key: 'slippageGuard', label: '滑点保护' },
  { key: 'weekendBlock', label: '周末禁交易' },
]

/** camelCase 面板键 -> snake_case 配置字段 */
const fieldOf = (key: string) =>
  key.replace(/[A-Z]/g, (c) => '_' + c.toLowerCase())

const isOn = (key: string) => {
  const f = filters.value as Record<string, unknown> | null
  return f ? Boolean(f[fieldOf(key)]) : false
}
const toggle = (key: string, value: boolean) => {
  const f = filters.value as unknown as Record<string, unknown> | null
  if (f) f[fieldOf(key)] = value
}

const versionLabel = computed(() =>
  config.value && config.value.version > 0
    ? `云端配置 v${config.value.version}`
    : '默认配置（未保存）',
)

const save = () => store.saveConfig()
</script>

<template>
  <div class="qp-panel">
    <div class="qp-panel__title">
      过滤设置
      <span v-if="filters" class="qp-live-badge">{{ versionLabel }}</span>
      <span v-else class="qp-sample-badge">加载中</span>
    </div>

    <template v-if="filters">
      <div class="fs__section">环境过滤器</div>
      <div class="fs__switches">
        <div v-for="s in switches" :key="s.key" class="fs__switch">
          <span>{{ s.label }}</span>
          <el-switch :model-value="isOn(s.key)" size="small" @update:model-value="(v: string | number | boolean) => toggle(s.key, Boolean(v))" />
        </div>
      </div>

      <div class="fs__section">操作限额</div>
      <div class="qp-rows">
        <div class="qp-row">
          <span class="qp-row__label">每日最大订单数</span>
          <el-input-number
            v-model="filters.max_daily_orders"
            size="small"
            :min="1"
            :max="1000"
            controls-position="right"
            class="fs__number"
          />
        </div>
        <div class="qp-row">
          <span class="qp-row__label">最大持仓数</span>
          <el-input-number
            v-model="filters.max_positions"
            size="small"
            :min="1"
            :max="500"
            controls-position="right"
            class="fs__number"
          />
        </div>
      </div>

      <button class="fs__save" :disabled="store.configSaving || !writable" @click="save">
        {{ store.configSaving ? '保存中…' : '保存到云端' }}
      </button>
      <div class="fs__note">OBSERVE_ONLY：配置仅存储与下发记录，不驱动任何交易行为</div>
    </template>
    <div v-else class="fs__empty">暂无配置数据</div>
  </div>
</template>

<style scoped>
.fs__section {
  font-size: 10px;
  letter-spacing: 1px;
  color: var(--qp-text-dim);
  text-transform: uppercase;
  margin: 4px 0 8px;
}
.fs__switches {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px 14px;
  margin-bottom: 12px;
}
.fs__switch {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 12px;
  color: var(--qp-text-dim);
  padding: 2px 0;
}
.fs__number {
  width: 110px;
}
.fs__save {
  margin-top: 10px;
  width: 100%;
  padding: 6px 0;
  font-size: 12px;
  border-radius: 6px;
  border: 1px solid var(--qp-border-soft);
  background: rgba(34, 197, 94, 0.08);
  color: var(--qp-text);
  cursor: pointer;
  transition: background 0.15s;
}
.fs__save:hover:not(:disabled) {
  background: rgba(34, 197, 94, 0.16);
}
.fs__save:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.fs__note {
  margin-top: 6px;
  font-size: 10px;
  color: var(--qp-text-dim);
  text-align: center;
}
.fs__empty {
  font-size: 12px;
  color: var(--qp-text-dim);
  text-align: center;
  padding: 20px 0;
}
</style>
