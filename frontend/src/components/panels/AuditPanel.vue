<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'
import type { AuditLogRow } from '../../api'

const store = useDashboardStore()

/** 操作类型（与后端 audit 埋点一一对应；新增埋点时同步维护） */
const AUDIT_ACTIONS = [
  'CONFIG_UPDATE',
  'INSTANCE_STATUS_UPDATE',
  'INSTANCE_CONFIG_UPDATE',
  'COMMAND_CREATE',
  'BACKTEST_CREATE',
  'AI_ANALYZE',
]

const scope = ref<'current' | 'all'>('current')
const actionFilter = ref('')
const limit = ref(100)

// “当前账户”但尚未选择账户时退化为全量（全局审计端点）
const effectiveAccountId = computed(() => {
  if (scope.value !== 'current') return undefined
  return store.selectedAccountId || undefined
})

async function load() {
  await store.refreshAuditLogs(effectiveAccountId.value, actionFilter.value || undefined, limit.value)
}

onMounted(load)
watch([scope, actionFilter, limit], load)
watch(
  () => store.selectedAccountId,
  () => {
    if (scope.value === 'current') load()
  },
)

function accountLabel(row: AuditLogRow) {
  const a = store.accounts.find((x) => x.account_id === row.account_id)
  return a ? `${a.login_ref} · ${a.server}` : row.account_id.slice(0, 8)
}

function fmtTime(iso: string) {
  return iso ? new Date(iso).toLocaleString() : '—'
}

function shortId(id: string | null) {
  if (!id) return ''
  return id.length > 14 ? `${id.slice(0, 14)}…` : id
}

function payloadText(payload: Record<string, unknown> | null) {
  return payload ? JSON.stringify(payload) : ''
}

function payloadPreview(payload: Record<string, unknown> | null) {
  const text = payloadText(payload)
  if (!text) return '—'
  return text.length > 72 ? `${text.slice(0, 72)}…` : text
}
</script>

<template>
  <section class="audit">
    <div class="audit__toolbar">
      <el-select v-model="scope" size="small" style="width: 140px">
        <el-option value="current" label="当前账户" />
        <el-option value="all" label="全部账户" />
      </el-select>
      <el-select
        v-model="actionFilter"
        size="small"
        clearable
        placeholder="全部操作"
        style="width: 220px"
      >
        <el-option v-for="a in AUDIT_ACTIONS" :key="a" :value="a" :label="a" />
      </el-select>
      <el-select v-model="limit" size="small" style="width: 130px">
        <el-option :value="50" label="最近 50 条" />
        <el-option :value="100" label="最近 100 条" />
        <el-option :value="200" label="最近 200 条" />
        <el-option :value="500" label="最近 500 条" />
      </el-select>
      <el-button size="small" :loading="store.auditLoading" @click="load">刷新</el-button>
      <span class="audit__hint">
        审计为 best-effort 写入：记录失败不影响业务操作，仅记服务日志
      </span>
    </div>

    <el-table
      :data="store.auditLogs"
      size="small"
      :loading="store.auditLoading"
      empty-text="暂无审计记录（执行一次配置保存 / 实例启停 / 回测 / AI 分析后产生）"
    >
      <el-table-column label="时间" width="170">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="账户" min-width="150">
        <template #default="{ row }">{{ accountLabel(row) }}</template>
      </el-table-column>
      <el-table-column prop="action" label="操作" width="190" />
      <el-table-column label="对象" min-width="170">
        <template #default="{ row }">
          <span class="audit__obj">
            {{ row.object_type }}<template v-if="row.object_id"> · {{ shortId(row.object_id) }}</template>
          </span>
        </template>
      </el-table-column>
      <el-table-column prop="actor" label="操作者" width="100" />
      <el-table-column label="载荷" min-width="280">
        <template #default="{ row }">
          <code class="audit__payload" :title="payloadText(row.payload)">
            {{ payloadPreview(row.payload) }}
          </code>
        </template>
      </el-table-column>
    </el-table>
  </section>
</template>

<style scoped>
.audit {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.audit__toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.audit__hint {
  font-size: 11px;
  color: var(--qp-text-dim, #64748b);
}

.audit__obj {
  font-family: inherit;
  font-size: 12px;
}

.audit__payload {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px;
  color: var(--qp-text-dim, #94a3b8);
  word-break: break-all;
}
</style>
