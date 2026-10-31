<script setup lang="ts">
import { computed } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'
import { canWrite } from '../../api'

const store = useDashboardStore()

// Role gating is INTERACTION-only; the backend re-checks on every call.
const writable = computed(() => canWrite())

const config = computed(() => store.config)

// 云端 risk_profiles 配置（OBSERVE_ONLY：本切片仅存储与展示，EA 端只记录不执行）
const expert = computed(() => config.value?.config.expert ?? null)

const switches: { key: string; label: string }[] = [
  { key: 'autoTrading', label: '自动交易' },
  { key: 'soundAlert', label: '声音提醒' },
  { key: 'popupAlert', label: '弹窗提醒' },
  { key: 'notificationAlert', label: '通知提醒' },
  { key: 'closeOnOpposite', label: '反向信号平仓' },
]

/** camelCase 面板键 -> snake_case 配置字段 */
const fieldOf = (key: string) =>
  key.replace(/[A-Z]/g, (c) => '_' + c.toLowerCase())

const isOn = (key: string) => {
  const e = expert.value as unknown as Record<string, unknown> | null
  return e ? Boolean(e[fieldOf(key)]) : false
}
const toggle = (key: string, value: boolean) => {
  const e = expert.value as unknown as Record<string, unknown> | null
  if (e) e[fieldOf(key)] = value
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
      EA 设置
      <span v-if="expert" class="qp-live-badge">{{ versionLabel }}</span>
      <span v-else class="qp-sample-badge">加载中</span>
    </div>

    <template v-if="expert">
      <div class="es__switches">
        <div v-for="s in switches" :key="s.key" class="es__switch">
          <span>{{ s.label }}</span>
          <el-switch :model-value="isOn(s.key)" size="small" @update:model-value="(v: string | number | boolean) => toggle(s.key, Boolean(v))" />
        </div>
      </div>

      <div class="qp-rows">
        <div class="qp-row">
          <span class="qp-row__label">魔术号</span>
          <el-input-number
            v-model="expert.magic_number"
            size="small"
            :min="0"
            :step="1"
            :step-strictly="true"
            controls-position="right"
            class="es__number"
          />
        </div>
        <div class="qp-row">
          <span class="qp-row__label">订单注释</span>
          <el-input v-model="expert.order_comment" size="small" maxlength="63" class="es__input" />
        </div>
      </div>

      <button class="es__save" :disabled="store.configSaving || !writable" @click="save">
        {{ store.configSaving ? '保存中…' : '保存到云端' }}
      </button>
      <div class="es__note">OBSERVE_ONLY：配置仅存储与下发记录，不驱动任何交易行为</div>
    </template>
    <div v-else class="es__empty">暂无配置数据</div>
  </div>
</template>

<style scoped>
.es__switches {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 12px;
}
.es__switch {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 12px;
  color: var(--qp-text-dim);
}
.es__number {
  width: 130px;
}
.es__input {
  width: 130px;
}
.es__save {
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
.es__save:hover:not(:disabled) {
  background: rgba(34, 197, 94, 0.16);
}
.es__save:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.es__note {
  margin-top: 6px;
  font-size: 10px;
  color: var(--qp-text-dim);
  text-align: center;
}
.es__empty {
  font-size: 12px;
  color: var(--qp-text-dim);
  text-align: center;
  padding: 20px 0;
}
</style>
