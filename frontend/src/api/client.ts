/** 统一请求封装：拼后端地址、抛带分类的错误、给页脚留一句可读的说明。
 *
 * 迟到事实场景要求把一次提交的结局分成三类：
 * - accepted（已受理）：服务端明确回了成功；
 * - failed（失败）：4xx / 业务 ok:false，服务端明确未受理，可改后重试；
 * - unconfirmed（迟到/未送达·待核实）：断线、超时、5xx，受理与否未知，
 *   必须用同一 request_key 查询或重投，不能直接重填新单。
 */
const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export type ErrorKind = 'network' | 'timeout' | 'http-4xx' | 'http-5xx' | 'business'

export class RequestError extends Error {
  kind: ErrorKind
  status?: number
  data?: unknown

  constructor(kind: ErrorKind, message: string, status?: number, data?: unknown) {
    super(message)
    this.name = 'RequestError'
    this.kind = kind
    this.status = status
    this.data = data
  }
}

export function request(path: string, init?: RequestInit): Promise<Response> {
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`
  // 注意：init.headers 必须与默认头合并，不能让 ...init 整体覆盖，
  // 否则带 Idempotency-Key 的调用会丢掉 Content-Type，被 fetch 退成 text/plain。
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...((init?.headers as Record<string, string> | undefined) ?? {}),
  }
  return fetch(url, {
    ...init,
    headers,
  }).catch((error: unknown) => {
    if (error instanceof RequestError) throw error
    const detail = error instanceof Error ? error.message : '请求未送达'
    // 网络层失败：请求可能根本没出门，也可能出门后回包丢失 —— 按“迟到/未送达”处理。
    throw new RequestError(
      'network',
      `接口请求失败：${detail}。断线导致响应迟到或请求仍未送达，是否已受理待核实，请勿另填新单`,
    )
  })
}

export async function fetchJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) {
    throw new RequestError(
      response.status >= 500 ? 'http-5xx' : 'http-4xx',
      `接口返回 ${response.status}，数据未更新`,
      response.status,
    )
  }
  return (await response.json()) as T
}

/** 从任意异常中归纳出三态结论，供登记弹窗与交班台账共用。 */
export function classifySubmissionError(error: unknown): {
  state: 'failed' | 'unconfirmed'
  kind: ErrorKind | 'unknown'
  message: string
} {
  if (error instanceof RequestError) {
    if (error.kind === 'network' || error.kind === 'timeout' || error.kind === 'http-5xx') {
      return { state: 'unconfirmed', kind: error.kind, message: error.message }
    }
    return { state: 'failed', kind: error.kind, message: error.message }
  }
  const message = error instanceof Error ? error.message : '登记失败'
  return { state: 'failed', kind: 'unknown', message }
}
