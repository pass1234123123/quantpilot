<script setup lang="ts">
import { computed } from 'vue'
import { useDashboardStore } from '../stores/dashboard'
import { num } from '../api'

defineProps<{ active: string }>()
const emit = defineEmits<{ (e: 'select', key: string): void }>()

const store = useDashboardStore()

interface NavItem {
  key: string
  label: string
}

const navItems: NavItem[] = [
  { key: 'dashboard', label: '仪表盘' },
  { key: 'performance', label: '绩效' },
  { key: 'risk', label: '风险管理' },
  { key: 'strategies', label: '策略引擎' },
  { key: 'signals', label: '交易信号' },
  { key: 'commands', label: '命令通道' },
  { key: 'backtests', label: '回测研究' },
  { key: 'ai', label: 'AI 分析' },
  { key: 'filtering', label: '过滤设置' },
  { key: 'expert', label: 'EA 设置' },
  { key: 'journal', label: '交易日志' },
  { key: 'news', label: '新闻过滤' },
  { key: 'market', label: '行情监控' },
  { key: 'alerts', label: '告警' },
  { key: 'settings', label: '设置' },
  { key: 'help', label: '帮助与支持' },
]

const systemHealth = computed(() => (store.apiHealthy ? 100 : 0))
const dataFeed = computed(() => (store.apiHealthy ? '正常' : '中断'))
const brokerConnection = computed(() =>
  store.snapshot?.connected ? '已建立' : '未知',
)
// EA 是否存活：最近 5 分钟内有 snapshot 上报即视为运行中（EA 挂掉会停止上报）
const eaAlive = computed(() => {
  const at = store.snapshot?.captured_at
  if (!at) return false
  return Date.now() - new Date(at).getTime() < 5 * 60 * 1000
})
const eaStatus = computed(() => (eaAlive.value ? '运行中' : '无心跳'))
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
        :key="item.key"
        class="sidebar__item"
        :class="{ 'is-active': item.key === active }"
        role="button"
        tabindex="0"
        @click="emit('select', item.key)"
        @keydown.enter="emit('select', item.key)"
      >
        <span class="sidebar__item-dot" />
        {{ item.label }}
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
  cursor: pointer;
  position: relative;
  user-select: none;
  transition: color 0.15s, background 0.15s;
}
.sidebar__item:hover,
.sidebar__item:focus-visible {
  color: var(--qp-text);
  background: rgba(148, 163, 184, 0.1);
  outline: none;
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
