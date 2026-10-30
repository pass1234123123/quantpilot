<script setup lang="ts">
import { computed } from 'vue'
import { useDashboardStore } from '../stores/dashboard'
import { num } from '../api'

const store = useDashboardStore()

const navItems = [
  '仪表盘',
  '绩效',
  '风险管理',
  '过滤设置',
  'EA 设置',
  '交易日志',
  '新闻过滤',
  '行情监控',
  '告警',
  '设置',
  '帮助与支持',
]

const active = '仪表盘'

const systemHealth = computed(() => (store.apiHealthy ? 100 : 0))
const dataFeed = computed(() => (store.apiHealthy ? '正常' : '中断'))
const brokerConnection = computed(() =>
  store.snapshot?.connected ? '已建立' : '未知',
)
const eaStatus = computed(() => (store.snapshot?.expert_allowed ? '运行中' : '停止'))
const lastUpdate = computed(() => store.snapshot?.captured_at ?? store.lastRefreshAt)
const openPositions = computed(() => store.snapshot?.open_positions ?? 0)
const freeMargin = computed(() => num(store.snapshot?.free_margin))
</script>

<template>
  <aside class="sidebar">
    <div class="sidebar__brand">
      <div class="sidebar__logo">Q</div>
      <div>
        <div class="sidebar__brand-name">QUANTPILOT</div>
        <div class="sidebar__brand-sub">AI 智能交易系统</div>
      </div>
    </div>

    <nav class="sidebar__nav">
      <div
        v-for="item in navItems"
        :key="item"
        class="sidebar__item"
        :class="{ 'is-active': item === active }"
      >
        <span class="sidebar__item-dot" />
        {{ item }}
      </div>
    </nav>

    <div class="sidebar__status">
      <div class="sidebar__status-title">系统状态</div>
      <div class="sidebar__health">
        <div class="sidebar__ring" :class="{ 'is-ok': systemHealth === 100 }">
          {{ systemHealth }}%
        </div>
        <div class="sidebar__health-label">系统健康度</div>
      </div>
      <div class="sidebar__status-row"><span>数据源</span><b>{{ dataFeed }}</b></div>
      <div class="sidebar__status-row">
        <span>券商连接</span><b>{{ brokerConnection }}</b>
      </div>
      <div class="sidebar__status-row"><span>EA 状态</span><b>{{ eaStatus }}</b></div>
      <div class="sidebar__status-row"><span>持仓数量</span><b>{{ openPositions }}</b></div>
      <div class="sidebar__status-row"><span>可用保证金</span><b>{{ freeMargin ?? '—' }}</b></div>
      <div class="sidebar__status-row"><span>最后更新</span><b>{{ lastUpdate || '—' }}</b></div>
    </div>
  </aside>
</template>

<style scoped>
.sidebar {
  width: 216px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: linear-gradient(180deg, #0d1826 0%, #0b1420 100%);
  border-right: 1px solid var(--qp-border);
  padding: 14px 12px;
  overflow-y: auto;
}

.sidebar__brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 6px 14px;
  border-bottom: 1px solid var(--qp-border-soft);
  margin-bottom: 12px;
}

.sidebar__logo {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  display: grid;
  place-items: center;
  font-weight: 800;
  font-size: 16px;
  color: #031015;
  background: linear-gradient(135deg, #00e0f0, #007a8a);
}

.sidebar__brand-name { font-weight: 700; letter-spacing: 1px; font-size: 13px; }
.sidebar__brand-sub { font-size: 9px; color: var(--qp-text-dim); letter-spacing: 1.2px; }

.sidebar__nav { display: flex; flex-direction: column; gap: 2px; }

.sidebar__item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border-radius: 6px;
  color: var(--qp-text-dim);
  font-size: 12.5px;
  cursor: default;
  position: relative;
}

.sidebar__item-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: rgba(148, 163, 184, 0.4);
}

.sidebar__item.is-active {
  color: var(--qp-accent);
  background: var(--qp-accent-soft);
  font-weight: 600;
}
.sidebar__item.is-active::before {
  content: '';
  position: absolute;
  left: -12px;
  top: 6px;
  bottom: 6px;
  width: 3px;
  border-radius: 2px;
  background: var(--qp-accent);
}
.sidebar__item.is-active .sidebar__item-dot { background: var(--qp-accent); }

.sidebar__status {
  margin-top: auto;
  border: 1px solid var(--qp-border);
  border-radius: 10px;
  padding: 12px;
  background: rgba(0, 176, 192, 0.04);
}

.sidebar__status-title {
  font-size: 10px;
  letter-spacing: 1.2px;
  color: var(--qp-text-dim);
  margin-bottom: 10px;
}

.sidebar__health {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  margin-bottom: 10px;
}

.sidebar__ring {
  width: 56px;
  height: 56px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  font-weight: 700;
  font-size: 14px;
  border: 3px solid rgba(239, 68, 68, 0.6);
  color: var(--qp-down);
}
.sidebar__ring.is-ok {
  border-color: rgba(34, 197, 94, 0.7);
  color: var(--qp-up);
  box-shadow: 0 0 12px rgba(34, 197, 94, 0.15);
}

.sidebar__health-label { font-size: 10px; color: var(--qp-text-dim); }

.sidebar__status-row {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 10.5px;
  padding: 3px 0;
  border-top: 1px solid rgba(148, 163, 184, 0.08);
}
.sidebar__status-row span { color: var(--qp-text-dim); }
.sidebar__status-row b { font-weight: 600; text-align: right; }
</style>
