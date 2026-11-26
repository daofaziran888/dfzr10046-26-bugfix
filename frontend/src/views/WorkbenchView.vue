<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <!-- 迟到事实：落库独立核查 + 在途凭证/交班续做台账 -->
    <LateFactDesk v-if="meta.key === 'late_fact'" @changed="reload" />

    <!-- 重跑任务：跨模块迟到待办，明确能否立即排期/移交 -->
    <ReprocessBoard
      v-else-if="meta.key === 'reprocess_task'"
      ref="reprocessBoardRef"
      @changed="reload"
    />

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal" @click.self="closeCreate">
      <form class="modal" data-testid="create-form" @submit.prevent="submitCreate">
        <div class="modal-head">
          <h3>登记{{ meta.entity }}</h3>
          <button
            class="modal-close"
            type="button"
            aria-label="关窗"
            data-testid="close-create"
            @click="closeCreate"
          >
            ×
          </button>
        </div>

        <!-- 本窗提交结论：只绑定当前 requestKey，旧回包不会写到这里 -->
        <p
          v-if="currentAttempt"
          class="submit-feedback"
          data-testid="submit-feedback"
          :data-state="currentAttempt.state"
        >
          <span class="state-badge" :class="`state-${currentAttempt.state}`">
            {{ attemptStateLabel(currentAttempt.state) }}
          </span>
          <span class="feedback-msg">{{ currentAttempt.message }}</span>
        </p>
        <p v-if="formError" class="error-text form-error" data-testid="create-error">{{ formError }}</p>

        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input
            v-model="createValues[field]"
            :data-testid="`create-${field}`"
            :required="meta.key !== 'late_fact'"
            @input="syncDerived(field)"
          />
        </label>

        <!-- 迟到事实：失败/待核实时沿用同一请求键续做，不另开新单 -->
        <div v-if="meta.key === 'late_fact' && currentAttempt && currentAttempt.state !== 'accepted' && currentAttempt.state !== 'sending'" class="retry-row">
          <button class="btn" type="button" data-testid="retry-current" @click="submitLateFact">
            用同一请求重投续做
          </button>
          <code class="attempt-key">{{ currentRequestKey }}</code>
        </div>

        <div class="page-actions">
          <button class="btn" type="button" data-testid="cancel-create" aria-label="取消并关窗" @click="closeCreate">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text" data-testid="page-error">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import LateFactDesk from '@/components/LateFactDesk.vue'
import ReprocessBoard from '@/components/ReprocessBoard.vue'
import {
  createEntry,
  downloadEntries,
  listEntries,
  moduleMeta,
  newRequestKey,
  runAction as applyAction,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'
import { beginAttempt, getAttempt, sendAttempt } from '@/domain/lateFactLedger'

const props = defineProps<{ moduleKey: string }>()
const meta = computed(() => moduleMeta(props.moduleKey))

const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = computed(() => meta.value.fields.slice(0, 3))

const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const formError = ref('')
const currentRequestKey = ref('')
const reprocessBoardRef = ref<InstanceType<typeof ReprocessBoard> | null>(null)

/**
 * 弹窗会话序号：每次开窗 +1。
 * 旧请求回包到达时，只有会话仍是当前会话且 requestKey 匹配，
 * 才允许关窗/写本窗错误，从根上杜绝“上一张表的回包合上当前新表”。
 */
let formSession = 0

const stats = computed(() => [
  { label: meta.value.metrics[0], value: total.value },
  { label: meta.value.metrics[1], value: rows.value.filter((row) => row.status === meta.value.statuses[1]).length },
  {
    label: meta.value.metrics[2],
    value: rows.value.filter((row) => row.status === meta.value.statuses[meta.value.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.value.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

const currentAttempt = computed(() =>
  currentRequestKey.value ? getAttempt(currentRequestKey.value) : undefined,
)

function attemptStateLabel(state: string): string {
  if (state === 'accepted') return '已受理'
  if (state === 'failed') return '失败（服务端明确未受理）'
  if (state === 'unconfirmed') return '迟到/未送达（待核实）'
  return '提交中…'
}

function localISODate(d: Date): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

function lateFactDefaults(): Record<string, string> {
  const today = localISODate(new Date())
  const suffix = Math.random().toString(16).slice(2, 8).toUpperCase()
  return {
    记录编号: `LF-${suffix}`,
    所属事实表: '销售事实表',
    业务发生日: '',
    到达日: today,
    滞后天数: '',
    受影响分区: '',
    回补结论: '待回补',
    登记时间: today,
  }
}

function syncDerived(changedField?: string) {
  if (meta.value.key !== 'late_fact') return
  // 只在日期变化时自动推算，避免覆盖人工填写的滞后天数/分区
  if (changedField && changedField !== '业务发生日' && changedField !== '到达日') return
  const v = createValues.value
  const bd = String(v['业务发生日'] || '')
  const ad = String(v['到达日'] || '')
  if (/^\d{4}-\d{2}-\d{2}$/.test(bd) && /^\d{4}-\d{2}-\d{2}$/.test(ad)) {
    const lag = Math.round((new Date(ad).getTime() - new Date(bd).getTime()) / 86_400_000)
    if (!Number.isNaN(lag)) v['滞后天数'] = String(lag)
    if (!v['受影响分区']) v['受影响分区'] = `part-${bd.slice(0, 7).replace('-', '')}`
  }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.value.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  formSession += 1
  createValues.value = meta.value.key === 'late_fact'
    ? lateFactDefaults()
    : Object.fromEntries(meta.value.fields.map((field) => [field, '']))
  formError.value = ''
  currentRequestKey.value = ''
  creating.value = false
  errorMessage.value = ''
  showCreate.value = true
}

/** 关窗只改变可见性，不取消在途请求；其结论仍按 requestKey 归台账。 */
function closeCreate() {
  showCreate.value = false
  formError.value = ''
}

async function submitCreate() {
  if (meta.value.key === 'late_fact') {
    await submitLateFact()
    return
  }

  const session = formSession
  creating.value = true
  formError.value = ''
  try {
    const result = await createEntry(meta.value.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    if (session === formSession) {
      showCreate.value = false
      await reload()
    } else {
      // 旧会话回包：绝不关当前新窗，只静默刷新后台列表
      void reload()
    }
  } catch (error) {
    const message = error instanceof Error ? error.message : '登记失败'
    if (session === formSession) {
      formError.value = message
      errorMessage.value = message
    }
    // 会话已过期（用户已开新表）：错误不进新表，避免串位
  } finally {
    if (session === formSession) creating.value = false
  }
}

async function submitLateFact() {
  formError.value = ''
  syncDerived()
  const values = { ...createValues.value }
  if (!String(values['业务发生日'] || '').trim()) {
    formError.value = '请填写业务发生日（例如 2026-06-03）'
    return
  }

  // 同一弹窗内失败后重试沿用原键；新开的弹窗 openCreate 会清空该键
  if (!currentRequestKey.value) currentRequestKey.value = newRequestKey()
  const requestKey = currentRequestKey.value
  const session = formSession

  beginAttempt({ requestKey, values })
  creating.value = true
  const attempt = await sendAttempt(requestKey)

  if (attempt.state === 'accepted') {
    if (session === formSession && showCreate.value && currentRequestKey.value === requestKey) {
      // 本窗自己的成功回包：维持“成功即关窗”的既有交互
      showCreate.value = false
      currentRequestKey.value = ''
      creating.value = false
      await reload()
    } else {
      // 这是上一张表迟到的受理回包：只归位到台账/待办，
      // 绝不能关掉此刻正在填写的下一张表。
      if (session === formSession) creating.value = false
      void reload()
    }
    return
  }

  // failed / unconfirmed：结论写回本请求自己的台账记录
  if (session === formSession && currentRequestKey.value === requestKey) {
    formError.value = attempt.message
    creating.value = false
  }
  // 若会话已经是更新的一张表，错误只留在旧 requestKey 的台账卡片里，
  // 不触碰当前表单的 formError，实现并发错误隔离。
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.value.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
    if (meta.value.key === 'reprocess_task') void reprocessBoardRef.value?.refresh()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.value.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

watch(
  () => props.moduleKey,
  async () => {
    // 路由复用同一组件时，切换模块必须重置弹窗/筛选并重新加载
    showCreate.value = false
    creating.value = false
    formError.value = ''
    currentRequestKey.value = ''
    formSession += 1
    filters.value = {}
    await reload()
  },
)

onMounted(reload)
</script>
