import { request, RequestError } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  ActionResult,
  AttemptRecord,
  EntryRow,
  OverviewResult,
  PageResult,
  Receipt,
} from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export interface CreateOptions {
  /** 幂等请求键：断线重投/交班续做时必须沿用同一个键。 */
  requestKey?: string
  registrar?: string
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
  options: CreateOptions = {},
): Promise<ActionResult> {
  const headers: Record<string, string> = {}
  if (options.requestKey) headers['Idempotency-Key'] = options.requestKey
  let response: Response
  try {
    response = await request(`/api/${key}`, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        values,
        request_key: options.requestKey,
        registrar: options.registrar,
      }),
    })
  } catch (error) {
    // client.ts 已把网络错误归类为 RequestError(network)，直接上抛给三态判定
    throw error
  }

  const raw = await response.text()
  let data: (ActionResult & Record<string, unknown>) | null = null
  if (raw) {
    try {
      data = JSON.parse(raw) as ActionResult & Record<string, unknown>
    } catch {
      data = null
    }
  }

  if (!response.ok) {
    const detail =
      (data && typeof data.message === 'string' && data.message) ||
      (data && typeof data.detail === 'string' && data.detail) ||
      `接口返回 ${response.status}`
    if (response.status >= 500) {
      throw new RequestError(
        'http-5xx',
        `服务异常（HTTP ${response.status}）：${detail}。首笔是否已受理无法确认，按「迟到/未送达·待核实」处理，请勿另填新单，用原请求键核实或重投`,
        response.status,
        data,
      )
    }
    if (response.status === 408 || response.status === 504) {
      throw new RequestError(
        'timeout',
        `请求超时（HTTP ${response.status}）：响应迟到，是否受理待核实，请用原请求键查询或重投`,
        response.status,
        data,
      )
    }
    throw new RequestError(
      'http-4xx',
      `登记失败（HTTP ${response.status}）：${detail}。服务端明确未受理，可修正内容后重试`,
      response.status,
      data,
    )
  }

  if (data && data.ok === false) {
    throw new RequestError('business', data.message || '登记失败，服务端明确未受理', response.status, data)
  }
  return (data ?? { ok: true, message: '已受理' }) as ActionResult
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

// ----------------------------------------------------------------------
// 迟到受理凭证 / 落库核查 / 移交 / 重跑待办
// ----------------------------------------------------------------------

export async function fetchSubmission(requestKey: string): Promise<{
  accepted: boolean
  persisted: boolean
  state: string
  receipt?: Receipt | null
  message?: string
}> {
  const response = await request(`/api/late_fact/submissions/${encodeURIComponent(requestKey)}`)
  if (!response.ok) throw new Error('受理凭证查询失败')
  return await response.json()
}

export async function checkPersistence(params: {
  businessDate?: string
  requestKey?: string
  recordNo?: string
}): Promise<{ persisted: boolean; count: number; items: EntryRow[]; message?: string }> {
  const query = new URLSearchParams()
  if (params.businessDate) query.set('business_date', params.businessDate)
  if (params.requestKey) query.set('request_key', params.requestKey)
  if (params.recordNo) query.set('record_no', params.recordNo)
  const response = await request(`/api/late_fact/persistence?${query.toString()}`)
  if (!response.ok) throw new Error('落库核查失败')
  return await response.json()
}

export async function handoverSubmission(
  requestKey: string,
  toPerson: string,
  note?: string,
): Promise<{ ok: boolean; handover?: unknown }> {
  const response = await request('/api/late_fact/handovers', {
    method: 'POST',
    body: JSON.stringify({ request_key: requestKey, to_person: toPerson, note }),
  })
  if (!response.ok) throw new Error('移交登记失败')
  return await response.json()
}

export interface ReprocessTodo {
  id?: number | string
  status?: string
  重跑编号: string
  重跑区间: string
  触发原因?: string
  影响事实表?: string
  覆盖策略?: string
  受理凭证?: string
  来源记录?: string
  来源业务日?: string
  可立即排期?: boolean
  排期说明?: string
  移交状态?: string
  接班人?: string
  [key: string]: unknown
}

export async function listReprocessTodos(): Promise<ReprocessTodo[]> {
  const response = await request('/api/reprocess_task/todos')
  if (!response.ok) throw new Error('重跑待办读取失败')
  const data = await response.json()
  return data.items ?? []
}

export async function scheduleReprocessTodo(
  ref: string | number,
  payload: { start?: string; end?: string; strategy?: string; range?: string } = {},
): Promise<ActionResult> {
  const response = await request(`/api/reprocess_task/todos/${encodeURIComponent(String(ref))}/schedule`, {
    method: 'POST',
    body: JSON.stringify({
      start: payload.start,
      end: payload.end,
      strategy: payload.strategy,
      '重跑区间': payload.range,
    }),
  })
  const data = await response.json().catch(() => null)
  if (!response.ok || (data && data.ok === false)) {
    throw new Error(data?.message || `排期失败（HTTP ${response.status}）`)
  }
  return data as ActionResult
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

/** 生成与后端风格一致的幂等请求键。 */
export function newRequestKey(): string {
  const rnd =
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID().replace(/-/g, '').slice(0, 16)
      : Math.random().toString(16).slice(2, 18).padEnd(16, '0')
  return `LF-REQ-${Date.now().toString(36)}-${rnd}`
}

/** 从台账记录构造提交参数（重试/重投保持同一份内容）。 */
export function attemptSummary(a: AttemptRecord): string {
  return `${a.recordNo || '未编号'} / 业务日 ${a.businessDate || '未填'} / 键 ${a.requestKey}`
}
