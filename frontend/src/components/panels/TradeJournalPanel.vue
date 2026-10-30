<script setup lang="ts">
import { computed } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'
import { num } from '../../api'

const store = useDashboardStore()

const rows = computed(() => store.positions)
const openRows = computed(() => rows.value.filter((r) => r.status === 'OPEN'))

// 浮动盈亏 = 仓位利润 + 库存费（swap）
function floating(row: { profit: string | null; swap: string | null }): number | null {
  const p = num(row.profit)
  const s = num(row.swap)
  if (p === null) return null
  return p + (s ?? 0)
}

const openFloatingTotal = computed(() =>
  openRows.value.reduce((acc, r) => acc + (floating(r) ?? 0), 0),
)

function fmt(value: number | null, digits = 2): string {
  if (value === null) return '—'
  return value.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

function fmtPrice(row: { current_price: string | null }): string {
  const n = num(row.current_price)
  return n === null ? '—' : n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 5 })
}

function fmtTime(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function directionLabel(d: 'BUY' | 'SELL'): string {
  return d === 'BUY' ? '买入' : '卖出'
}
</script>

<template>
  <div class="qp-panel">
    <div class="qp-panel__title">
      持仓明细（EA 实时同步）
      <span v-if="openRows.length > 0" class="tj__total" :class="openFloatingTotal >= 0 ? 'qp-up' : 'qp-down'">
        持仓浮动盈亏 {{ openFloatingTotal >= 0 ? '+' : '' }}{{ fmt(openFloatingTotal) }}
        {{ store.snapshot?.currency ?? '' }}
      </span>
    </div>

    <el-table v-if="rows.length > 0" :data="rows" size="small" class="tj__table" row-key="position_id">
      <el-table-column prop="broker_position_ticket" label="持仓号" width="110" />
      <el-table-column prop="broker_symbol" label="品种" width="100" />
      <el-table-column label="方向" width="70">
        <template #default="{ row }">
          <span :class="row.direction === 'BUY' ? 'qp-up' : 'qp-down'">{{ directionLabel(row.direction) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="手数" width="80" align="right">
        <template #default="{ row }">{{ fmt(num(row.volume)) }}</template>
      </el-table-column>
      <el-table-column label="开仓价" width="100" align="right">
        <template #default="{ row }">{{ fmt(num(row.price_open)) }}</template>
      </el-table-column>
      <el-table-column label="现价" width="100" align="right">
        <template #default="{ row }">{{ fmtPrice(row) }}</template>
      </el-table-column>
      <el-table-column label="止损/止盈" width="130" align="right">
        <template #default="{ row }">
          {{ row.sl === null ? '—' : fmt(num(row.sl)) }} / {{ row.tp === null ? '—' : fmt(num(row.tp)) }}
        </template>
      </el-table-column>
      <el-table-column label="浮动盈亏" width="120" align="right">
        <template #default="{ row }">
          <span :class="(floating(row) ?? 0) >= 0 ? 'qp-up' : 'qp-down'">
            {{ (floating(row) ?? 0) >= 0 ? '+' : '' }}{{ fmt(floating(row)) }}
          </span>
        </template>
      </el-table-column>
      <el-table-column label="开仓时间" width="120">
        <template #default="{ row }">{{ fmtTime(row.open_time) }}</template>
      </el-table-column>
      <el-table-column label="状态" min-width="90">
        <template #default="{ row }">
          <el-tag v-if="row.status === 'OPEN'" type="success" size="small" effect="dark">持仓中</el-tag>
          <el-tag v-else type="info" size="small">已平仓 {{ fmtTime(row.closed_at) }}</el-tag>
        </template>
      </el-table-column>
    </el-table>

    <el-empty v-else description="当前账户暂无持仓记录（EA 每 10 秒同步一次）" :image-size="60" />
  </div>
</template>

<style scoped>
.tj__table {
  width: 100%;
}
.tj__total {
  font-size: 12px;
  font-weight: 600;
  margin-left: 12px;
}
</style>
