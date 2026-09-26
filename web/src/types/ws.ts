/** WS 契约 —— 《文件修改注意事项.md》§3.1 的 TypeScript 化。
 *  字段名与 gateway/app.py、status.py 逐项对应；拼错字段名在此编译不过。 */

/** 客户端 → 服务端 */
export type WsClientMsg =
  | { type: 'chat'; text: string }
  | { type: 'ping'; ts: number }
  | { type: 'stop' }
  | { type: 'template'; name: string }

export type StatusKind =
  | 'STATUS_PROGRESS'
  | 'STATUS_INFO'
  | 'STATUS_PHASE'
  | 'STATUS_CHECKPOINT'
  | 'STATUS_DRAFT'
  | 'STATUS_COLLECT'
  | 'STATUS_MCP'
  | 'STATUS_IMAGES'
  | 'STATUS_SUBAGENT'
  | 'STATUS_COMPLETED'
  | 'STATUS_CANCELLED'
  | 'STATUS_ERROR'
  | 'STATUS_FALLBACK'
  | 'AGENT_MESSAGE'

export interface WsOrder {
  type: string
  name: string
  amount: string | number
  selected?: boolean
  reference_only?: boolean
  reason?: string
  link?: string
}

export interface WsUsage {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  limit: number
}

export interface WsProfile {
  version?: number
  basic_info?: Record<string, unknown>
  detail_info?: Record<string, unknown>
}

/** 服务端 → 客户端（ discriminated union 之外的全部消息；按 type/kind 分支） */
export interface WsServerMsg {
  type: string
  // session
  sid?: string
  title?: string
  // chat
  role?: 'user' | 'chatter' | 'system'
  text?: string
  replay?: boolean
  // status / AGENT_MESSAGE
  kind?: StatusKind
  agent?: string
  seq?: number
  ts?: number
  phase?: string
  eta_min?: [number, number]
  elapsed_s?: number
  // sub_states / STATUS_SUBAGENT
  channel?: string
  state?: string
  states?: Record<string, string>
  // draft
  html?: string
  // final
  pdf_url?: string
  orders?: WsOrder[]
  total_price?: number | string
  // profile / usage
  profile?: WsProfile
  usage?: WsUsage
}

/** 令牌过期/未认证时前端去往注册页的消息键（AuthView 读取 query.err 展示） */
export const AUTH_EXPIRED_QUERY = 'expired'
