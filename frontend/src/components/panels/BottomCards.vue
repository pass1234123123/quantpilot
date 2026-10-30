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
</script>

<template>
  <div class="bc">
    <!-- News Filter（示例数据） -->
    <div class="qp-panel bc__card">
      <div class="qp-panel__title">
        News Filter
        <span class="qp-sample-badge">示例数据</span>
      </div>
      <div class="bc__news">
        <div class="bc__news-row"><span>High Impact News</span><b>Blocked</b></div>
        <div class="bc__news-row"><span>Impact Level</span><b>High</b></div>
        <div class="bc__countdown">
          <div class="bc__countdown-label">NEXT NEWS</div>
          <div class="bc__countdown-time">03:59:16</div>
          <div class="bc__countdown-name">US Non-Farm Payrolls</div>
        </div>
      </div>
    </div>

    <!-- Spread & Commission（示例数据） -->
    <div class="qp-panel bc__card">
      <div class="qp-panel__title">
        Spread &amp; Commission
        <span class="qp-sample-badge">示例数据</span>
      </div>
      <div class="bc__rows">
        <div class="bc__row"><span>Max Spread</span><b>30 pts</b></div>
        <div class="bc__row"><span>Commission Limit</span><b>$7.0/lot</b></div>
        <div class="bc__row"><span>Slippage Guard</span><b class="qp-up">On</b></div>
        <div class="bc__row"><span>Swap Filter</span><b class="qp-dim">Off</b></div>
      </div>
    </div>

    <!-- Permission Signal（真实数据） -->
    <div class="qp-panel bc__card bc__card--signal">
      <div class="qp-panel__title">
        Permission Signal
        <span class="qp-live-badge">真实数据</span>
      </div>
      <div class="bc__shield" :class="{ 'is-ok': allOperational }">
        <div class="bc__shield-icon">🛡</div>
        <div class="bc__shield-text">
          {{ allOperational ? 'ALL SYSTEMS OPERATIONAL' : 'PERMISSION INCOMPLETE' }}
        </div>
      </div>
      <div class="bc__rows">
        <div class="bc__row"><span>Connected</span><b :class="snap?.connected ? 'qp-up' : 'qp-down'">{{ snap?.connected ? 'Yes' : 'No' }}</b></div>
        <div class="bc__row"><span>Trade Allowed</span><b :class="snap?.trade_allowed ? 'qp-up' : 'qp-down'">{{ snap?.trade_allowed ? 'Yes' : 'No' }}</b></div>
        <div class="bc__row"><span>Expert Allowed</span><b :class="snap?.expert_allowed ? 'qp-up' : 'qp-down'">{{ snap?.expert_allowed ? 'Yes' : 'No' }}</b></div>
      </div>
    </div>

    <!-- Trading Status（真实数据） -->
    <div class="qp-panel bc__card">
      <div class="qp-panel__title">
        Trading Status
        <span class="qp-live-badge">真实数据</span>
      </div>
      <div class="bc__rows">
        <div class="bc__row"><span>Account Status</span><b class="qp-warn">{{ account?.status || '—' }}</b></div>
        <div class="bc__row"><span>Open Positions</span><b>{{ snap?.open_positions ?? '—' }}</b></div>
        <div class="bc__row"><span>EA Version</span><b>{{ snap?.ea_version || '—' }}</b></div>
        <div class="bc__row"><span>Terminal</span><b>{{ snap?.terminal_version || '—' }}</b></div>
      </div>
    </div>

    <!-- Account Info（真实数据） -->
    <div class="qp-panel bc__card">
      <div class="qp-panel__title">
        Account Info
        <span class="qp-live-badge">真实数据</span>
      </div>
      <div class="bc__rows">
        <div class="bc__row"><span>Account Mode</span><b>{{ account?.account_mode || '—' }}</b></div>
        <div class="bc__row"><span>Leverage</span><b>1:{{ account?.leverage ?? '—' }}</b></div>
        <div class="bc__row"><span>Broker</span><b class="bc__ellip">{{ account?.company || account?.server || '—' }}</b></div>
        <div class="bc__row"><span>Login</span><b>{{ account?.login_ref || '—' }}</b></div>
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
