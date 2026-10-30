<script setup lang="ts">
import { ref } from 'vue'

// 示例数据：EA 参数下发链路（命令通道）尚未实现，仅展示目标形态
const switches = ref({
  autoTrading: true,
  soundAlert: false,
  popupAlert: true,
  notificationAlert: false,
  closeOnOpposite: true,
})
const params = [
  { label: '魔术号', value: '123456' },
  { label: '订单注释', value: 'QuantPilot' },
  { label: '滑点', value: '10' },
  { label: '交易方向', value: '双向' },
  { label: '最大持仓数', value: '5' },
]
</script>

<template>
  <div class="qp-panel">
    <div class="qp-panel__title">
      EA 设置
      <span class="qp-sample-badge">示例数据</span>
    </div>

    <div class="es__switches">
      <div v-for="(_on, key) in switches" :key="key" class="es__switch">
        <span>{{ { autoTrading: '自动交易', soundAlert: '声音提醒', popupAlert: '弹窗提醒', notificationAlert: '通知提醒', closeOnOpposite: '反向信号平仓' }[key] }}</span>
        <el-switch v-model="switches[key as keyof typeof switches]" size="small" />
      </div>
    </div>

    <div class="qp-rows">
      <div v-for="p in params" :key="p.label" class="qp-row">
        <span class="qp-row__label">{{ p.label }}</span><b class="qp-row__value">{{ p.value }}</b>
      </div>
    </div>
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
</style>
