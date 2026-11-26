<template>
  <section class="reprocess-board" data-testid="reprocess-board" aria-label="迟到事实跨模块重跑待办">
    <div class="board-head">
      <h3>迟到事实 → 重跑待办（同一份实际受理结论）</h3>
      <button class="btn ghost" type="button" data-testid="reprocess-refresh" @click="refresh">刷新待办</button>
    </div>

    <!-- 已受理落库：明确“可立即排期”，受理凭证与登记弹窗一致 -->
    <div v-if="todos.length" class="todo-list" data-testid="reprocess-todo-list">
      <article
        v-for="todo in todos"
        :key="String(todo.id)"
        class="todo-card"
        :data-todo-id="String(todo.id)"
        :data-request-key="todo.受理凭证 || ''"
        :data-business-date="todo.来源业务日 || ''"
        :data-partition-start="partitionStart(todo.来源业务日)"
        :data-partition-end="partitionEnd(todo.来源业务日)"
        data-testid="reprocess-todo-card"
      >
        <div class="todo-main">
          <div class="todo-title-row">
            <strong>{{ todo.重跑编号 }}</strong>
            <span v-if="isLinked(todo)" class="state-badge state-accepted">已受理</span>
            <span v-else class="state-badge state-generic">既有任务</span>
            <span
              v-if="isLinked(todo)"
              class="ready-tag"
              data-testid="schedule-readiness"
              :data-immediate="todo.可立即排期 === false ? 'false' : 'true'"
            >
              {{ todo.可立即排期 === false ? '需先办理移交后再排期' : '受理结论已落库 · 可立即排期' }}
            </span>
            <span v-if="todo.移交状态 === '已移交'" class="mini-tag handover-tag">
              已移交 → {{ todo.接班人 }}
            </span>
          </div>
          <dl class="todo-meta">
            <div><dt>触发原因</dt><dd>{{ todo.触发原因 }}</dd></div>
            <div><dt>影响事实表</dt><dd>{{ todo.影响事实表 }}</dd></div>
            <div><dt>重跑区间</dt><dd data-testid="todo-range">{{ todo.重跑区间 }}</dd></div>
            <div><dt>来源业务日</dt><dd class="todo-date" data-testid="todo-business-date">
              {{ todo.来源业务日 || '—' }}
              <span v-if="todo.来源业务日" class="todo-date-cn" data-testid="todo-business-date-cn">{{ formatCN(todo.来源业务日) }}</span>
            </dd></div>
            <div><dt>覆盖策略</dt><dd>{{ todo.覆盖策略 }}</dd></div>
            <div><dt>当前状态</dt><dd>{{ todo.status }}</dd></div>
            <div v-if="todo.受理凭证"><dt>同一受理凭证</dt><dd><code class="attempt-key">{{ todo.受理凭证 }}</code></dd></div>
          </dl>
          <p class="desk-hint">{{ todo.排期说明 || '受理结论跨模块写入，重跑岗可直接排期。' }}</p>
        </div>
        <div class="todo-side">
          <button
            class="btn primary"
            type="button"
            :data-testid="`try-schedule-${todo.id}`"
            data-role="try-schedule"
            :disabled="todo.status !== '待排期'"
            @click="openSchedule(todo)"
          >
            {{ scheduleBtnLabel(todo.status) }}
          </button>
        </div>
      </article>
    </div>
    <p v-else class="desk-hint" data-testid="reprocess-empty">暂无迟到事实派生的重跑待办。</p>

    <!-- 仍未送达/待核实的首笔：不能盲目排期，先核实、原键重投或办理移交 -->
    <div v-if="pendingAttempts.length" class="handover-zone" data-testid="reprocess-handover-zone">
      <h4>交班续做：受理结论尚未确认的提交</h4>
      <article
        v-for="a in pendingAttempts"
        :key="a.requestKey"
        class="todo-card pending-card"
        :data-state="a.state"
        :data-request-key="a.requestKey"
        :data-business-date="a.businessDate"
        data-testid="reprocess-pending-attempt"
      >
        <div class="todo-main">
          <div class="todo-title-row">
            <strong>{{ a.recordNo || '未编号' }}</strong>
            <span class="attempt-date">业务日 {{ a.businessDate || '—' }}</span>
            <span class="state-badge" :class="`state-${a.state}`">
              {{ a.state === 'failed' ? '失败（未受理）' : '迟到/未送达（待核实）' }}
            </span>
            <span v-if="a.handedOver" class="mini-tag handover-tag">已移交 → {{ a.successor }}</span>
          </div>
          <code class="attempt-key">{{ a.requestKey }}</code>
          <p class="attempt-message">{{ a.message }}</p>
          <p class="desk-hint">
            服务端还没有这份受理结论，<strong>不能立即排期</strong>；请先核实，若确认未送达就用同一请求重投，或由登记人办理移交。
          </p>
          <div class="attempt-actions">
            <button class="btn" type="button" data-testid="board-reconcile" @click="onReconcile(a.requestKey)">
              查询是否已受理
            </button>
            <button class="btn primary" type="button" data-testid="board-retry" :disabled="a.state === 'sending'" @click="onRetry(a.requestKey)">
              同一请求重投续做
            </button>
            <button class="btn ghost" type="button" data-testid="board-handover" @click="onHandover(a.requestKey)">
              {{ a.handedOver ? '重新登记移交' : '登记人办理移交' }}
            </button>
          </div>
          <div v-if="handoverKey === a.requestKey" class="handover-form" data-testid="board-handover-form">
            <label class="desk-field">
              <span>接班人</span>
              <input v-model="successor" type="text" data-testid="board-handover-to" />
            </label>
            <button class="btn primary" type="button" data-testid="board-confirm-handover" @click="confirmHandover(a.requestKey)">
              确认移交并由我接班续做
            </button>
          </div>
        </div>
      </article>
    </div>

    <!-- 试排期弹窗：具体日期可核对、可调整 -->
    <div v-if="scheduleVisible" class="modal-backdrop" data-testid="schedule-modal" @click.self="closeSchedule">
      <form class="modal schedule-modal" data-testid="schedule-form" @submit.prevent="confirmSchedule">
        <h3>试排期 · {{ scheduling?.重跑编号 }}</h3>
        <p class="desk-hint">
          来源业务日
          <strong data-testid="schedule-source-date">{{ scheduling?.来源业务日 }}</strong>
          <span v-if="scheduling?.来源业务日" data-testid="schedule-source-date-cn">
            （{{ formatCN(String(scheduling.来源业务日)) }}）
          </span>
          ，默认精确覆盖迟到当天，可按分区扩大区间。
        </p>
        <label class="create-field">
          <span>重跑开始日</span>
          <input v-model="start" type="date" data-testid="schedule-start" required />
        </label>
        <label class="create-field">
          <span>重跑结束日</span>
          <input v-model="end" type="date" data-testid="schedule-end" required />
        </label>
        <div class="range-shortcuts">
          <button type="button" class="btn ghost" data-testid="range-single-day" @click="setSingleDay">
            仅迟到当天（{{ scheduling?.来源业务日 }}）
          </button>
          <button type="button" class="btn ghost" data-testid="range-partition-month" @click="setPartitionMonth">
            按受影响分区整月重算
          </button>
        </div>
        <label class="create-field">
          <span>覆盖策略</span>
          <select v-model="strategy" data-testid="schedule-strategy">
            <option value="并存留痕">并存留痕</option>
            <option value="整段替换">整段替换</option>
          </select>
        </label>
        <p v-if="scheduleError" class="error-text" data-testid="schedule-error">{{ scheduleError }}</p>
        <div class="page-actions">
          <button class="btn" type="button" data-testid="schedule-cancel" @click="closeSchedule">取消</button>
          <button class="btn primary" type="submit" data-testid="schedule-confirm" :disabled="saving">
            {{ saving ? '排期中…' : '确认排期' }}
          </button>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { listReprocessTodos, scheduleReprocessTodo, type ReprocessTodo } from '@/api/backend-service'
import {
  getAttempt,
  handoverAttempt,
  ledger,
  reconcileAttempt,
  sendAttempt,
} from '@/domain/lateFactLedger'

const emit = defineEmits<{ (e: 'changed'): void }>()

const todos = ref<ReprocessTodo[]>([])
const scheduleVisible = ref(false)
const scheduling = ref<ReprocessTodo | null>(null)
const start = ref('')
const end = ref('')
const strategy = ref('并存留痕')
const scheduleError = ref('')
const saving = ref(false)

const handoverKey = ref('')
const successor = ref('夜班重跑岗')

const pendingAttempts = computed(() =>
  [...ledger.attempts]
    .reverse()
    .filter((a) => a.state !== 'accepted'),
)

/** 2026-06-03 -> 2026年6月3日，供按中文日期核对。 */
function formatCN(iso: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || '').trim())
  if (!m) return ''
  return `${m[1]}年${Number(m[2])}月${Number(m[3])}日`
}

/** 是否为本链路（迟到事实受理结论）派生的待办。 */
function isLinked(todo: ReprocessTodo): boolean {
  return Boolean(todo.受理凭证 || todo.来源模块 === 'late_fact' || todo.来源记录)
}

function scheduleBtnLabel(status?: string): string {
  if (status === '待排期' || !status) return '试排期'
  if (status === '重跑中') return '重跑中…'
  return status // 已完成 / 重跑失败 本身已含结果语义
}

async function refresh() {
  try {
    todos.value = await listReprocessTodos()
  } catch {
    todos.value = []
  }
}

defineExpose({ refresh })

onMounted(refresh)

function parseRange(todo: ReprocessTodo): [string, string] {
  const range = String(todo.重跑区间 || '')
  if (range.includes('至')) {
    const [s, e] = range.split('至')
    return [s.trim(), e.trim()]
  }
  const d = String(todo.来源业务日 || '')
  return [d, d]
}

function openSchedule(todo: ReprocessTodo) {
  scheduling.value = todo
  const [s, e] = parseRange(todo)
  start.value = s
  end.value = e
  strategy.value = String(todo.覆盖策略 || '并存留痕')
  scheduleError.value = ''
  scheduleVisible.value = true
}

function closeSchedule() {
  if (saving.value) return
  scheduleVisible.value = false
  scheduling.value = null
}

function setSingleDay() {
  const d = String(scheduling.value?.来源业务日 || start.value || '')
  if (/^\d{4}-\d{2}-\d{2}$/.test(d)) {
    start.value = d
    end.value = d
  }
}

function setPartitionMonth() {
  const d = String(scheduling.value?.来源业务日 || start.value || '')
  const s = partitionStart(d)
  const e = partitionEnd(d)
  if (s && e) {
    start.value = s
    end.value = e
  }
}

function partitionStart(d?: string): string {
  const m = /^(\d{4})-(\d{2})-\d{2}$/.exec(String(d || ''))
  return m ? `${m[1]}-${m[2]}-01` : ''
}

function partitionEnd(d?: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(d || ''))
  if (!m) return ''
  const last = new Date(Number(m[1]), Number(m[2]), 0).getDate()
  return `${m[1]}-${m[2]}-${String(last).padStart(2, '0')}`
}

async function confirmSchedule() {
  if (!scheduling.value) return
  scheduleError.value = ''
  if (!start.value || !end.value) {
    scheduleError.value = '请填写完整的重跑起止日期'
    return
  }
  if (end.value < start.value) {
    scheduleError.value = `结束日 ${end.value} 不能早于开始日 ${start.value}`
    return
  }
  saving.value = true
  try {
    const ref = scheduling.value.受理凭证 || String(scheduling.value.id)
    const result = await scheduleReprocessTodo(ref, {
      start: start.value,
      end: end.value,
      strategy: strategy.value,
    })
    if (!result.ok) throw new Error(result.message)
    scheduleVisible.value = false
    scheduling.value = null
    await refresh()
    emit('changed')
  } catch (error) {
    scheduleError.value = error instanceof Error ? error.message : '排期失败'
  } finally {
    saving.value = false
  }
}

async function onReconcile(key: string) {
  const a = await reconcileAttempt(key)
  if (a.state === 'accepted') {
    await refresh()
    emit('changed')
  }
}

async function onRetry(key: string) {
  const a = await sendAttempt(key)
  if (a.state === 'accepted') {
    await refresh()
    emit('changed')
  }
}

function onHandover(key: string) {
  handoverKey.value = handoverKey.value === key ? '' : key
}

async function confirmHandover(key: string) {
  if (!successor.value.trim()) return
  await handoverAttempt(key, successor.value.trim())
  handoverKey.value = ''
  // 移交结论可能已在服务端补挂到待办
  await refresh()
}
</script>
