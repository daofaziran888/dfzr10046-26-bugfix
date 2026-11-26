/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
  entry?: EntryRow | null
  receipt?: Receipt | null
  continued?: boolean | null
  state?: string | null
  persisted?: boolean | null
}

export type Receipt = {
  request_key: string
  receipt_no?: string
  记录编号?: string
  业务发生日?: string
  所属事实表?: string
  entry_id?: number
  state?: string
  accepted?: boolean
  persisted?: boolean
  continued?: boolean
  registrar?: string
  移交状态?: string
  接班人?: string
  accepted_at?: string
  todo?: {
    id?: number
    重跑编号?: string
    重跑区间?: string
    status?: string
    可立即排期?: boolean
  }
  [key: string]: unknown
}

/** 一次提交在前端台账里的结局：提交中 / 已受理 / 失败 / 迟到(待核实)。 */
export type AttemptState = 'sending' | 'accepted' | 'failed' | 'unconfirmed'

export interface AttemptRecord {
  requestKey: string
  recordNo: string
  businessDate: string
  factTable: string
  values: Record<string, string>
  state: AttemptState
  message: string
  status?: number
  kind?: string
  continued?: boolean
  receipt?: Receipt | null
  todoRef?: Receipt['todo'] | null
  createdAt: number
  updatedAt: number
  handedOver?: boolean
  successor?: string
  handoverNote?: string
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}
