<template>
  <section class="late-desk" data-testid="late-fact-desk" aria-label="迟到事实受理核查与交班续做">
    <div class="desk-grid">
      <!-- 六月三日是否已落库：独立核查入口，不依赖登记弹窗是否还开着 -->
      <article class="desk-card" data-testid="persistence-check">
        <h3>落库独立核查</h3>
        <p class="desk-hint">按业务发生日或受理请求键，直接查后端是否已落库，供交班核对。</p>
        <div class="desk-form-row">
          <label class="desk-field">
            <span>业务发生日</span>
            <input
              v-model="checkDate"
              type="date"
              data-testid="persistence-date"
              name="business_date"
              placeholder="2026-06-03"
            />
          </label>
          <label class="desk-field grow">
            <span>或受理请求键</span>
            <input
              v-model="checkKey"
              type="text"
              data-testid="persistence-key"
              name="request_key"
              placeholder="LF-REQ-..."
            />
          </label>
          <button class="btn primary" type="button" data-testid="check-persistence" @click="runCheck">
            查询是否落库
          </button>
        </div>
        <p
          v-if="checkResult"
          class="check-result"
          data-testid="persistence-result"
          :data-persisted="checkResult.persisted ? 'true' : 'false'"
          :class="checkResult.persisted ? 'ok-text' : 'warn-text'"
        >
          <template v-if="checkResult.persisted">
            已落库：{{ checkResult.count }} 条（查询日 {{ checkResult.business_date || checkDate || '按凭证' }}）
          </template>
          <template v-else>尚未落库：服务端没有匹配的迟到记录</template>
        </p>
        <p v-if="checkError" class="error-text" data-testid="persistence-error">{{ checkError }}</p>
      </article>

      <!-- 在途凭证 / 交班续做台账：每条请求的错误只显示在自己的卡片里 -->
      <article class="desk-card" data-testid="submission-ledger">
        <div class="ledger-head">
          <h3>在途凭证 · 交班续做台账</h3>
          <button class="btn ghost" type="button" data-testid="refresh-ledger" @click="refreshAll">
            全部核实受理结果
          </button>
        </div>
        <p class="desk-hint">
          断线或服务异常时区分
          <span class="chip chip-failed">失败</span>
          <span class="chip chip-late">迟到/未送达</span>
          <span class="chip chip-accepted">已受理</span>
          ；重投沿用同一请求，只续原记录。
        </p>

        <div v-if="!ledger.attempts.length" class="ledger-empty" data-testid="ledger-empty">
          本机暂无在途提交；登记一笔迟到事实后，这里会保留它的受理凭证。
        </div>

        <ul class="attempt-list">
          <li
            v-for="a in orderedAttempts"
            :key="a.requestKey"
            class="attempt-item"
            :data-state="a.state"
            :data-request-key="a.requestKey"
            :data-business-date="a.businessDate"
            data-testid="ledger-attempt"
          >
            <div class="attempt-title">
              <strong>{{ a.recordNo || '（未生成编号）' }}</strong>
              <span class="attempt-date">业务日 {{ a.businessDate || '—' }}</span>
              <span class="state-badge" :class="`state-${a.state}`" data-testid="attempt-state">
                {{ stateLabel(a.state) }}
              </span>
              <span v-if="a.continued" class="mini-tag">沿用原记录</span>
              <span v-if="a.handedOver" class="mini-tag handover-tag" :data-successor="a.successor || ''">
                已移交 → {{ a.successor }}
              </span>
            </div>
            <code class="attempt-key" data-testid="attempt-request-key">{{ a.requestKey }}</code>
            <p class="attempt-message" :class="{ 'error-text': a.state === 'failed' }">{{ a.message }}</p>

            <div class="attempt-actions">
              <button
                v-if="a.state === 'unconfirmed' || a.state === 'sending'"
                class="btn"
                type="button"
                data-testid="reconcile-attempt"
                @click="onReconcile(a.requestKey)"
              >
                查询是否已受理
              </button>
              <button
                v-if="a.state !== 'accepted'"
                class="btn primary"
                type="button"
                data-testid="retry-attempt"
                :disabled="a.state === 'sending'"
                @click="onRetry(a.requestKey)"
              >
                {{ a.state === 'sending' ? '提交中…' : '按原请求重投续做' }}
              </button>
              <button
                v-if="!a.handedOver"
                class="btn ghost"
                type="button"
                data-testid="handover-attempt"
                @click="toggleHandover(a.requestKey)"
              >
                办理移交
              </button>
              <span v-else class="handover-done">接班人：{{ a.successor }}</span>
            </div>

            <div v-if="handoverKey === a.requestKey" class="handover-form" data-testid="handover-form">
              <label class="desk-field">
                <span>接班人（重跑岗）</span>
                <input v-model="successor" type="text" data-testid="handover-to" placeholder="例如：夜班重跑岗" />
              </label>
              <label class="desk-field grow">
                <span>移交说明</span>
                <input v-model="handoverNote" type="text" data-testid="handover-note" placeholder="首笔受理结论待核实，请用原键续做" />
              </label>
              <button class="btn primary" type="button" data-testid="confirm-handover" @click="onHandover(a.requestKey)">
                确认移交
              </button>
            </div>
          </li>
        </ul>
      </article>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import { checkPersistence } from '@/api/backend-service'
import {
  getAttempt,
  handoverAttempt,
  ledger,
  reconcileAttempt,
  sendAttempt,
} from '@/domain/lateFactLedger'

const emit = defineEmits<{ (e: 'changed'): void }>()

const checkDate = ref('')
const checkKey = ref('')
const checkResult = ref<{ persisted: boolean; count: number; business_date?: string } | null>(null)
const checkError = ref('')

const handoverKey = ref('')
const successor = ref('夜班重跑岗')
const handoverNote = ref('')

const orderedAttempts = computed(() => [...ledger.attempts].sort((a, b) => b.updatedAt - a.updatedAt))

function stateLabel(state: string): string {
  if (state === 'accepted') return '已受理'
  if (state === 'failed') return '失败（服务端未受理）'
  if (state === 'unconfirmed') return '迟到/未送达（待核实）'
  return '提交中（在途）'
}

async function runCheck() {
  checkError.value = ''
  checkResult.value = null
  if (!checkDate.value && !checkKey.value) {
    checkError.value = '请填写业务发生日或受理请求键'
    return
  }
  try {
    checkResult.value = await checkPersistence({
      businessDate: checkDate.value || undefined,
      requestKey: checkKey.value.trim() || undefined,
    })
  } catch (error) {
    checkError.value = error instanceof Error ? error.message : '核查请求失败'
  }
}

async function onRetry(key: string) {
  await sendAttempt(key)
  const a = getAttempt(key)
  if (a?.state === 'accepted') emit('changed')
}

async function onReconcile(key: string) {
  const a = await reconcileAttempt(key)
  if (a.state === 'accepted') emit('changed')
}

function toggleHandover(key: string) {
  handoverKey.value = handoverKey.value === key ? '' : key
}

async function onHandover(key: string) {
  if (!successor.value.trim()) return
  await handoverAttempt(key, successor.value.trim(), handoverNote.value.trim())
  handoverKey.value = ''
}

async function refreshAll() {
  for (const a of ledger.attempts) {
    if (a.state === 'unconfirmed' || a.state === 'sending') {
      await reconcileAttempt(a.requestKey)
    }
  }
  emit('changed')
}
</script>
