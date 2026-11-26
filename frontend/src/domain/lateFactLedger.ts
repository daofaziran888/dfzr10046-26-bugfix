/**
 * 迟到事实提交台账：登记弹窗、交班台账、重跑工作台共享同一份提交结果。
 *
 * 关键约束：
 * - requestKey 是一次请求的幂等身份，断线重投/交班续做都沿用它；
 * - 响应串位时，各组件只按 requestKey 读写自己的那一条，绝不把旧错误
 *   写进当前新表单；
 * - 台账持久化到 localStorage，交班/刷新后仍可核实与续做。
 */
import { reactive } from 'vue'

import {
  checkPersistence,
  createEntry,
  fetchSubmission,
  handoverSubmission,
  newRequestKey,
} from '@/api/backend-service'
import { classifySubmissionError } from '@/api/client'
import type { AttemptRecord, Receipt } from '@/data/types'

const STORAGE_KEY = 'late_fact_attempt_ledger_v1'

function load(): AttemptRecord[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw) as AttemptRecord[]
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

function persist(attempts: AttemptRecord[]) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(attempts.slice(-50)))
  } catch {
    /* 隐私模式等场景下忽略持久化失败 */
  }
}

export const ledger = reactive({
  attempts: load() as AttemptRecord[],
})

function save() {
  persist(ledger.attempts)
}

export function getAttempt(requestKey: string): AttemptRecord | undefined {
  return ledger.attempts.find((a) => a.requestKey === requestKey)
}

export function latestAttemptForDate(businessDate: string): AttemptRecord | undefined {
  return [...ledger.attempts].reverse().find((a) => a.businessDate === businessDate)
}

/** 打开弹窗填写时先建一条 sending 台账（提交瞬间也可）。 */
export function beginAttempt(input: {
  requestKey?: string
  values: Record<string, string>
}): AttemptRecord {
  const requestKey = input.requestKey || newRequestKey()
  const values = { ...input.values }
  const now = Date.now()
  const existing = getAttempt(requestKey)
  if (existing) {
    existing.values = values
    existing.state = 'sending'
    existing.message = '提交中…'
    existing.updatedAt = now
    save()
    return existing
  }
  const attempt: AttemptRecord = {
    requestKey,
    recordNo: String(values['记录编号'] || ''),
    businessDate: String(values['业务发生日'] || ''),
    factTable: String(values['所属事实表'] || ''),
    values,
    state: 'sending',
    message: '提交中…',
    createdAt: now,
    updatedAt: now,
    handedOver: false,
  }
  ledger.attempts.push(attempt)
  save()
  return attempt
}

function applyAccepted(attempt: AttemptRecord, data: {
  message?: string
  receipt?: Receipt | null
  continued?: boolean | null
}) {
  attempt.state = 'accepted'
  attempt.message = data.message || '已受理并落库'
  attempt.receipt = data.receipt ?? null
  attempt.continued = Boolean(data.continued)
  attempt.todoRef = (data.receipt?.todo as AttemptRecord['todoRef']) ?? null
  if (data.receipt?.['记录编号']) attempt.recordNo = String(data.receipt['记录编号'])
  if (data.receipt?.['业务发生日']) attempt.businessDate = String(data.receipt['业务发生日'])
  attempt.updatedAt = Date.now()
}

/** 真正发请求；首次提交与按原键重投都走这里，保证只续原记录。 */
export async function sendAttempt(requestKey: string): Promise<AttemptRecord> {
  const attempt = getAttempt(requestKey)
  if (!attempt) throw new Error(`台账中没有请求 ${requestKey}`)
  attempt.state = 'sending'
  attempt.message = '提交中…'
  attempt.updatedAt = Date.now()
  save()
  try {
    const data = await createEntry('late_fact', attempt.values, { requestKey })
    applyAccepted(attempt, data)
  } catch (error) {
    const classified = classifySubmissionError(error)
    attempt.state = classified.state
    attempt.kind = classified.kind
    attempt.status = error instanceof Error && 'status' in error
      ? Number((error as { status?: number }).status) || undefined
      : undefined
    attempt.message = classified.message
    attempt.updatedAt = Date.now()
  }
  save()
  return attempt
}

/** 断线/异常后核实服务端到底受没受理；若已受理则把同一份结论补回台账。 */
export async function reconcileAttempt(requestKey: string): Promise<AttemptRecord> {
  const attempt = getAttempt(requestKey)
  if (!attempt) throw new Error(`台账中没有请求 ${requestKey}`)
  try {
    const info = await fetchSubmission(requestKey)
    if (info.persisted && info.receipt) {
      applyAccepted(attempt, {
        message: '经独立核实：该请求实际已受理落库（迟到回包已归位）',
        receipt: info.receipt,
        continued: true,
      })
    } else {
      // 再用业务日/记录编号查一次，兼容请求键未随首笔到达服务端的情况
      const report = await checkPersistence({
        businessDate: attempt.businessDate || undefined,
        recordNo: attempt.recordNo || undefined,
      })
      if (report.persisted && report.items[0]) {
        const row = report.items[0] as unknown as Receipt
        attempt.state = 'accepted'
        attempt.message = '按业务日/记录编号核查到历史登记，已沿用原记录'
        attempt.receipt = {
          ...row,
          request_key: String(row.request_key || requestKey),
        } as Receipt
        attempt.todoRef = null
      } else {
        attempt.state = 'unconfirmed'
        attempt.message = '核查后服务端仍无受理记录：首笔确认未送达，可用同一请求键重投续做'
      }
    }
  } catch (error) {
    const classified = classifySubmissionError(error)
    attempt.message = `核实请求本身失败：${classified.message}`
  }
  attempt.updatedAt = Date.now()
  save()
  return attempt
}

export async function handoverAttempt(
  requestKey: string,
  successor: string,
  note?: string,
): Promise<AttemptRecord> {
  const attempt = getAttempt(requestKey)
  if (!attempt) throw new Error(`台账中没有请求 ${requestKey}`)
  attempt.handedOver = true
  attempt.successor = successor
  attempt.handoverNote = note || ''
  attempt.updatedAt = Date.now()
  try {
    await handoverSubmission(requestKey, successor, note)
  } catch {
    // 服务端暂不可达时，移交先记在本机台账，接班人可据此继续核实/重投
  }
  save()
  return attempt
}

export function removeAttempt(requestKey: string) {
  const idx = ledger.attempts.findIndex((a) => a.requestKey === requestKey)
  if (idx >= 0) ledger.attempts.splice(idx, 1)
  save()
}
