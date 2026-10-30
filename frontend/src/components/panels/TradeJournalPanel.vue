<script setup lang="ts">
// 示例数据：交易日志需成交数据（orders/deals 表），本切片尚无交易执行
const trades = [
  { id: 1, symbol: 'XAUUSD', type: 'Buy', mode: 'AI', profit: 68.4, rr: 2.1, open: '05.20 08:12', close: '05.20 09:45', dur: '1h 33m', exit: 'TP Hit' },
  { id: 2, symbol: 'EURUSD', type: 'Sell', mode: 'Quant', profit: -21.5, rr: 1.0, open: '05.20 06:40', close: '05.20 07:55', dur: '1h 15m', exit: 'SL Hit' },
  { id: 3, symbol: 'GBPUSD', type: 'Buy', mode: 'AI', profit: 45.2, rr: 1.8, open: '05.19 14:22', close: '05.19 16:08', dur: '1h 46m', exit: 'Signal' },
  { id: 4, symbol: 'USDCAD', type: 'Sell', mode: 'Quant', profit: 12.8, rr: 1.5, open: '05.19 09:30', close: '05.19 11:02', dur: '1h 32m', exit: 'Signal' },
  { id: 5, symbol: 'USDJPY', type: 'Buy', mode: 'AI', profit: 89.6, rr: 2.4, open: '05.18 15:05', close: '05.18 18:44', dur: '3h 39m', exit: 'TP Hit' },
]
</script>

<template>
  <div class="qp-panel">
    <div class="qp-panel__title">
      Trade Journal (Auto-Journal)
      <span class="qp-sample-badge">示例数据</span>
    </div>

    <el-table :data="trades" size="small" class="tj__table">
      <el-table-column prop="id" label="#" width="44" />
      <el-table-column prop="symbol" label="Symbol" width="90" />
      <el-table-column label="Type" width="80">
        <template #default="{ row }">
          <span :class="row.type === 'Buy' ? 'qp-up' : 'qp-down'">{{ row.type }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="mode" label="Mode" width="80" />
      <el-table-column label="Net Profit" width="110" align="right">
        <template #default="{ row }">
          <span :class="row.profit >= 0 ? 'qp-up' : 'qp-down'">
            {{ row.profit >= 0 ? '+' : '' }}${{ row.profit.toFixed(2) }}
          </span>
        </template>
      </el-table-column>
      <el-table-column prop="rr" label="R:R" width="70" align="right" />
      <el-table-column prop="open" label="Open Time" width="120" />
      <el-table-column prop="close" label="Close Time" width="120" />
      <el-table-column prop="dur" label="Duration" width="90" />
      <el-table-column prop="exit" label="Exit Reason" min-width="100" />
    </el-table>
  </div>
</template>

<style scoped>
.tj__table { width: 100%; }
</style>
