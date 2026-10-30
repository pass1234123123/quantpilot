<script setup lang="ts">
import { computed } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'

const store = useDashboardStore()
const account = computed(() => store.selectedAccount)
const snap = computed(() => store.snapshot)

// 真实数据：Permission/Trading Status ← snapshot 权限位；Account Info ← accounts
const allOperational = computed(
  () => !!snap.value?.connected && !!snap.value?.trade_allowed && !!snap.value?.expert_allowed,
)

const yesNo = (v: boolean | undefined) => (v ? '是' : '否')
</script>

<template>
  <div class="bc">
    <!-- 新闻过滤（示例数据） -->
    <div class="qp-panel bc__card">
      <div class="qp-panel__title">
        新闻过滤
        <span class="qp-sample-badge">示例数据</span>
      </div>
      <div class="bc__news">
        <div class="bc__news-row"><span>高影响新闻</span><b>已屏蔽</b></div>
        <div class="bc__news-row"><span>影响级别</span><b>高</b></div>
        <div class="bc__countdown">
          <div class="bc__countdown-label">下一条新闻</div>
          <div class="bc__countdown-time">03:59:16</div>
          <div class="bc__countdown-name">美国非农就业数据</div>
        </div>
      </div>
    </div>

    <!-- 点差与佣金（示例数据） -->
    <div class="qp-panel bc__card">
      <div class="qp-panel__title">
        点差与佣金
        <span class="qp-sample-badge">示例数据</span>
      </div>
      <div class="bc__rows">
        <div class="bc__row"><span>最大点差</span><b>30 点</b></div>
        <div class="bc__row"><span>佣金上限</span><b>$7.0/手</b></div>
        <div class="bc__row"><span>滑点保护</span><b class="qp-up">开启</b></div>
        <div class="bc__row"><span>库存费过滤</span><b class="qp-dim">关闭</b></div>
      </div>
    </div>

    <!-- 权限信号（真实数据） -->
    <div class="qp-panel bc__card bc__card--signal">
      <div class="qp-panel__title">
        权限信号
        <span class="qp-live-badge">真实数据</span>
      </div>
      <div class="bc__shield" :class="{ 'is-ok': allOperational }">
        <div class="bc__shield-icon">🛡</div>
        <div class="bc__shield-text">
          {{ allOperational ? '全系统正常运行' : '权限不完整' }}
        </div>
      </div>
      <div class="bc__rows">
        <div class="bc__row"><span>已连接</span><b :class="snap?.connected ? 'qp-up' : 'qp-down'">{{ yesNo(snap?.connected) }}</b></div>
        <div class="bc__row"><span>允许交易</span><b :class="snap?.trade_allowed ? 'qp-up' : 'qp-down'">{{ yesNo(snap?.trade_allowed) }}</b></div>
        <div class="bc__row"><span>允许 EA</span><b :class="snap?.expert_allowed ? 'qp-up' : 'qp-down'">{{ yesNo(snap?.expert_allowed) }}</b></div>
      </div>
    </div>

    <!-- 交易状态（真实数据） -->
    <div class="qp-panel bc__card">
      <div class="qp-panel__title">
        交易状态
        <span class="qp-live-badge">真实数据</span>
      </div>
      <div class="bc__rows">
        <div class="bc__row"><span>账户状态</span><b class="qp-warn">{{ account?.status || '—' }}</b></div>
        <div class="bc__row"><span>持仓数量</span><b>{{ snap?.open_positions ?? '—' }}</b></div>
        <div class="bc__row"><span>EA 版本</span><b>{{ snap?.ea_version || '—' }}</b></div>
        <div class="bc__row"><span>终端版本</span><b>{{ snap?.terminal_version || '—' }}</b></div>
      </div>
    </div>

    <!-- 账户信息（真实数据） -->
    <div class="qp-panel bc__card">
      <div class="qp-panel__title">
        账户信息
        <span class="qp-live-badge">真实数据</span>
      </div>
      <div class="bc__rows">
        <div class="bc__row"><span>账户模式</span><b>{{ account?.account_mode || '—' }}</b></div>
        <div class="bc__row"><span>杠杆</span><b>1:{{ account?.leverage ?? '—' }}</b></div>
        <div class="bc__row"><span>经纪商</span><b class="bc__ellip">{{ account?.company || account?.server || '—' }}</b></div>
        <div class="bc__row"><span>账号</span><b>{{ account?.login_ref || '—' }}</b></div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.bc {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 10px;
  margin-top: 12px;
}
.bc__card { gap: 0; }
.bc__rows { display: flex; flex-direction: column; gap: 6px; }
.bc__row {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 11.5px;
  padding: 3px 0;
  border-bottom: 1px solid rgba(148, 163, 184, 0.07);
}
.bc__row:last-child { border-bottom: none; }
.bc__row span { color: var(--qp-text-dim); white-space: nowrap; }
.bc__row b { font-weight: 600; text-align: right; }
.bc__ellip { max-width: 130px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.qp-warn { color: var(--qp-warn); }

.bc__news-row {
  display: flex;
  justify-content: space-between;
  font-size: 11.5px;
  padding: 3px 0;
}
.bc__news-row span { color: var(--qp-text-dim); }

.bc__countdown {
  margin-top: 8px;
  text-align: center;
  border: 1px solid var(--qp-border-soft);
  border-radius: 8px;
  padding: 8px;
  background: rgba(245, 158, 11, 0.05);
}
.bc__countdown-label { font-size: 9px; letter-spacing: 1px; color: var(--qp-text-dim); }
.bc__countdown-time { font-size: 20px; font-weight: 700; color: var(--qp-warn); font-variant-numeric: tabular-nums; }
.bc__countdown-name { font-size: 10px; color: var(--qp-text-dim); }

.bc__shield {
  text-align: center;
  padding: 10px 6px;
  border-radius: 8px;
  margin-bottom: 10px;
  border: 1px solid rgba(239, 68, 68, 0.4);
  background: rgba(239, 68, 68, 0.06);
}
.bc__shield.is-ok {
  border-color: rgba(34, 197, 94, 0.4);
  background: rgba(34, 197, 94, 0.06);
}
.bc__shield-icon { font-size: 26px; margin-bottom: 4px; }
.bc__shield-text { font-size: 10.5px; font-weight: 700; letter-spacing: 0.6px; color: var(--qp-text); }

@media (max-width: 1400px) {
  .bc { grid-template-columns: repeat(3, 1fr); }
}
</style>
