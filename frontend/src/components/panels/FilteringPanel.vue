<script setup lang="ts">
import { ref } from 'vue'

// 示例数据：过滤器配置链路（云端→EA）尚未实现，仅展示目标形态
const switches = ref({
  newsFilter: true,
  highImpactOnly: true,
  sessionFilter: false,
  maxSpread: true,
  slippageGuard: true,
  weekendBlock: false,
})
const limits = [
  { label: '每日最大订单数', value: '10' },
  { label: '最大持仓数', value: '5' },
  { label: '交易方向', value: '双向' },
  { label: '允许品种数', value: '28' },
  { label: '屏蔽品种数', value: '3' },
]
</script>

<template>
  <div class="qp-panel">
    <div class="qp-panel__title">
      过滤设置
      <span class="qp-sample-badge">示例数据</span>
    </div>

    <div class="fs__section">环境过滤器</div>
    <div class="fs__switches">
      <div v-for="(_on, key) in switches" :key="key" class="fs__switch">
        <span>{{ { newsFilter: '新闻', highImpactOnly: '高影响事件', sessionFilter: '交易时段', maxSpread: '最大点差', slippageGuard: '滑点保护', weekendBlock: '周末禁交易' }[key] }}</span>
        <el-switch v-model="switches[key as keyof typeof switches]" size="small" />
      </div>
    </div>

    <div class="fs__section">操作限额</div>
    <div class="qp-rows">
      <div v-for="l in limits" :key="l.label" class="qp-row">
        <span class="qp-row__label">{{ l.label }}</span><b class="qp-row__value">{{ l.value }}</b>
      </div>
    </div>
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
</style>
