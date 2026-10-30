<script setup lang="ts">
// 示例数据：交易日志需成交数据（orders/deals 表），本切片尚无交易执行
const trades = [
  { id: 1, symbol: 'XAUUSD', type: '买入', mode: 'AI', profit: 68.4, rr: 2.1, open: '05.20 08:12', close: '05.20 09:45', dur: '1时33分', exit: '止盈触发' },
  { id: 2, symbol: 'EURUSD', type: '卖出', mode: '量化', profit: -21.5, rr: 1.0, open: '05.20 06:40', close: '05.20 07:55', dur: '1时15分', exit: '止损触发' },
  { id: 3, symbol: 'GBPUSD', type: '买入', mode: 'AI', profit: 45.2, rr: 1.8, open: '05.19 14:22', close: '05.19 16:08', dur: '1时46分', exit: '信号平仓' },
  { id: 4, symbol: 'USDCAD', type: '卖出', mode: '量化', profit: 12.8, rr: 1.5, open: '05.19 09:30', close: '05.19 11:02', dur: '1时32分', exit: '信号平仓' },
  { id: 5, symbol: 'USDJPY', type: '买入', mode: 'AI', profit: 89.6, rr: 2.4, open: '05.18 15:05', close: '05.18 18:44', dur: '3时39分', exit: '止盈触发' },
]
</script>

<template>
  <div class="qp-panel">
    <div class="qp-panel__title">
      交易日志（自动记录）
      <span class="qp-sample-badge">示例数据</span>
    </div>

    <el-table :data="trades" size="small" class="tj__table">
      <el-table-column prop="id" label="序号" width="60" />
      <el-table-column prop="symbol" label="品种" width="90" />
      <el-table-column label="类型" width="80">
        <template #default="{ row }">
          <span :class="row.type === '买入' ? 'qp-up' : 'qp-down'">{{ row.type }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="mode" label="模式" width="80" />
      <el-table-column label="净盈亏" width="110" align="right">
        <template #default="{ row }">
          <span :class="row.profit >= 0 ? 'qp-up' : 'qp-down'">
            {{ row.profit >= 0 ? '+' : '' }}${{ row.profit.toFixed(2) }}
          </span>
        </template>
      </el-table-column>
      <el-table-column prop="rr" label="盈亏比" width="80" align="right" />
      <el-table-column prop="open" label="开仓时间" width="120" />
      <el-table-column prop="close" label="平仓时间" width="120" />
      <el-table-column prop="dur" label="持仓时长" width="100" />
      <el-table-column prop="exit" label="平仓原因" min-width="100" />
    </el-table>
  </div>
</template>

<style scoped>
.tj__table { width: 100%; }
</style>
