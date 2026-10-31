<script setup lang="ts">
import { computed } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'

const store = useDashboardStore()

// 心跳新鲜度（真实）：最近一次 snapshot 上报距今多久
const snapshotAge = computed(() => {
  const at = store.snapshot?.captured_at
  if (!at) return null
  const sec = Math.floor((Date.now() - new Date(at).getTime()) / 1000)
  return sec < 60 ? `${sec} 秒前` : sec < 3600 ? `${Math.floor(sec / 60)} 分钟前` : `${Math.floor(sec / 86400)} 小时前`
})
</script>

<template>
  <div class="help">
    <div class="qp-panel">
      <div class="qp-panel__title">系统架构与数据链路</div>
      <div class="help__flow">
        <span class="help__node">MT5 终端<br /><small>EA (MQL5)</small></span>
        <span class="help__arrow">→</span>
        <span class="help__node">本地 Agent<br /><small>事件采集</small></span>
        <span class="help__arrow">→</span>
        <span class="help__node">NATS JetStream<br /><small>消息队列</small></span>
        <span class="help__arrow">→</span>
        <span class="help__node">云端 API<br /><small>入库 / 查询</small></span>
        <span class="help__arrow">→</span>
        <span class="help__node">PostgreSQL<br /><small>持久化</small></span>
        <span class="help__arrow">→</span>
        <span class="help__node">本仪表盘<br /><small>只读展示</small></span>
      </div>
      <div class="help__note">
        <p>EA 运行在你的 MT5 终端内，只读采集账户 / 持仓 / 成交 / 品种 / 日历数据并上报，<b>本系统不发送任何交易指令</b>。</p>
        <p v-if="snapshotAge">最近一次快照上报：<b>{{ snapshotAge }}</b>（「{{ store.snapshot?.ea_version ?? '—' }}」版本 EA）。</p>
      </div>
    </div>

    <div class="qp-panel">
      <div class="qp-panel__title">数据刷新频率（真实节奏）</div>
      <div class="help__rows">
        <div class="help__row"><span>账户快照（余额 / 权益 / 保证金）</span><b>约 30 秒</b></div>
        <div class="help__row"><span>持仓 / 挂单 / 成交</span><b>事件驱动，随变化上报</b></div>
        <div class="help__row"><span>品种规格与点差观测</span><b>每小时</b></div>
        <div class="help__row"><span>财经日历</span><b>每 10 分钟</b></div>
        <div class="help__row"><span>本页面前端轮询</span><b>30 秒</b></div>
      </div>
    </div>

    <div class="qp-panel">
      <div class="qp-panel__title">常见问题</div>
      <div class="help__faq">
        <p><b>Q：点差为 0 / 价格不动？</b><br />周末或休市时段经纪商不更新报价，属正常真实数据。</p>
        <p><b>Q：成交时间为什么是「服务器时间」？</b><br />MT5 不提供服务器时区接口，成交时间按经纪商服务器时钟存储（已知近似，已在 API 文档注明）。</p>
        <p><b>Q：提示 API 离线？</b><br />请确认云端 API 服务已启动；本页面所有数据均来自该 API，无任何本地模拟。</p>
        <p><b>Q：配置修改后何时生效？</b><br />保存后写入云端版本库，EA 通过本地 Agent 拉取后应用；本系统为观察切片，不对交易行为做强制执行。</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.help { display: flex; flex-direction: column; gap: 12px; }

.help__flow {
  display: flex;
  align-items: stretch;
  gap: 6px;
  flex-wrap: wrap;
  margin: 4px 0 10px;
}
.help__node {
  flex: 1;
  min-width: 92px;
  text-align: center;
  font-size: 11px;
  font-weight: 600;
  padding: 10px 6px;
  border: 1px solid var(--qp-border);
  border-radius: 8px;
  background: rgba(0, 176, 192, 0.05);
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 2px;
}
.help__node small { font-size: 9px; color: var(--qp-text-dim); font-weight: 400; }
.help__arrow { align-self: center; color: var(--qp-text-dim); font-size: 13px; }

.help__note p,
.help__faq p {
  margin: 0 0 8px;
  font-size: 11.5px;
  line-height: 1.7;
  color: var(--qp-text-dim);
}
.help__note p:last-child,
.help__faq p:last-child { margin-bottom: 0; }
.help__note b,
.help__faq b { color: var(--qp-text); font-weight: 600; }

.help__rows { display: flex; flex-direction: column; gap: 6px; }
.help__row {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 11.5px;
  padding: 3px 0;
  border-bottom: 1px solid rgba(148, 163, 184, 0.07);
}
.help__row:last-child { border-bottom: none; }
.help__row span { color: var(--qp-text-dim); }
.help__row b { font-weight: 600; }
</style>
