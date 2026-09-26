/** useSocket —— WS 连接层（旧 app.js connect()/心跳/退避/outbox 的 1:1 平移）。
 *  模块级单例：路由切换不丢连接状态。渲染通过 onWsMessage 监听器交给 PlanView。 */
import { reactive } from 'vue'
import type { WsClientMsg, WsServerMsg } from '../types/ws'
import { authFetch, clearToken } from '../api/auth'
import { currentSid, setSid, refreshSessions } from './useSession'

export interface SocketViewHooks {
  onConnChange?: (on: boolean) => void
  onReplayReset?: () => void // onopen：清空聊天/时间线，等服务端补播
  onReconnected?: () => void // 断线重连恢复提示
  onOfflineSend?: () => void // 离线发送进 outbox 的时间线提示
  onTimeline?: (agent: string, kind: string, text: string) => void // 连接层自有提示（半开检测等）
  onOutboxFlush?: (texts: string[]) => void // 补发：回显重画 + 置忙
  onNeedLogin?: (reason: string) => void // 令牌失效 → 注册页
}

let ws: WebSocket | null = null
let pingTimer = 0
let reconnectTimer = 0
let reconnectDelay = 2000 // 意外断线重连退避：2s 起 ×2 递增，30s 封顶（连接成功后重置）
let manualClose = false // 主动断开（切换会话）：不走退避，立即重连
let missedPongs = 0
let reconnected = false
let outbox: { sid: string; text: string }[] = [] // 离线待发消息 [{sid, text}]
const listeners = new Set<(m: WsServerMsg) => void>()
let hooks: SocketViewHooks = {}

export const busy = reactive({ on: false, seconds: 0 })
let busyTimer = 0
let busyStart = 0

export function setSocketHooks(h: SocketViewHooks) {
  hooks = h
}

export function onWsMessage(fn: (m: WsServerMsg) => void) {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export function isWsOpen(): boolean {
  return !!ws && ws.readyState === 1
}

/** "用户名/sid" 复合键 → 裸 sid（无斜杠原样返回；用户名白名单无斜杠，切分无歧义） */
export function bareSid(sid: string | undefined): string {
  const s = sid || ''
  return s.includes('/') ? s.split('/').pop()! : s || 'default'
}

export function sendRaw(obj: WsClientMsg) {
  if (isWsOpen()) ws!.send(JSON.stringify(obj))
}

/** 收到 pong：半开连接检测视为链路健康 */
export function notifyPong() {
  missedPongs = 0
}

export function sendMsg(text: string) {
  if (isWsOpen()) {
    sendRaw({ type: 'chat', text })
  } else {
    outbox.push({ sid: currentSid.value, text }) // 离线不丢弃：重连后自动补发
    hooks.onOfflineSend?.()
  }
}

/** 思考中状态与计时（旧 setBusy：发送钮禁用 + “思考中… X秒/X分Y秒”） */
export function setBusy(v: boolean) {
  busy.on = v
  if (busyTimer) { clearInterval(busyTimer); busyTimer = 0 }
  if (v) {
    busyStart = Date.now()
    busy.seconds = 0
    busyTimer = setInterval(() => {
      busy.seconds = Math.round((Date.now() - busyStart) / 1000)
    }, 1000)
  } else {
    busy.seconds = 0
  }
}

/** 主动以当前 sid 重连（切换会话用）：不走退避 */
export function reconnectNow() {
  manualClose = true
  ws?.close()
}

async function flushOutbox() {
  if (!outbox.length) return
  const pending = outbox.filter((o) => o.sid === currentSid.value)
  outbox = outbox.filter((o) => o.sid !== currentSid.value) // 其他会话的离线消息保留，切回时再补发
  if (!pending.length) return
  hooks.onOutboxFlush?.(pending.map((o) => o.text))
  for (const o of pending) sendMsg(o.text) // 服务端 chatter_lock 串行处理，回复按序到达
  setBusy(true)
}

export async function connect() {
  let ticket = ''
  try {
    const r = await authFetch('/api/ws-ticket', { method: 'POST' }) // 30s 短时票据：长期令牌不进 URL
    if (r.status !== 200) return
    ticket = (await r.json()).ticket
  } catch (e) {
    return // 401 已由 authFetch 处理
  }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  ws = new WebSocket(`${proto}://${location.host}/ws?sid=${encodeURIComponent(currentSid.value)}&ticket=${encodeURIComponent(ticket)}`)
  ws.onopen = () => {
    hooks.onConnChange?.(true)
    hooks.onReplayReset?.() // 清面板防重连重复渲染，服务端将全量补播
    reconnectDelay = 2000 // 连接成功：重置退避
    missedPongs = 0
    if (pingTimer) clearInterval(pingTimer)
    pingTimer = setInterval(() => {
      // 心跳 25s；半开连接检测：空闲期连续 2 次（~50s）未收到 pong → 主动断开走重连。
      // busy（等待聊天回复）期间豁免：服务端接收循环被处理阻塞，pong 会积压到回合结束才回。
      if (!ws || ws.readyState !== 1) return
      if (!busy.on && missedPongs >= 2) {
        hooks.onTimeline?.('System', 'STATUS_ERROR', '连接长时间无响应，正在重新连接…')
        ws.close()
        return
      }
      missedPongs++
      sendRaw({ type: 'ping', ts: Date.now() })
    }, 25000)
    if (reconnected) {
      // 断线重连：解除"思考中"死锁（回复可能已随断线丢失）+ 恢复提示
      setBusy(false)
      hooks.onReconnected?.()
    }
    reconnected = true
    refreshSessions()
  }
  ws.onclose = (e) => {
    hooks.onConnChange?.(false)
    if (pingTimer) { clearInterval(pingTimer); pingTimer = 0 }
    if (e.code === 4401) {
      // 未认证：票据/令牌失效 → 停自动重连，去注册页
      clearToken()
      hooks.onNeedLogin?.('expired')
      return
    }
    const wasManual = manualClose
    manualClose = false
    const delay = wasManual ? 0 : reconnectDelay
    reconnectDelay = wasManual ? 2000 : Math.min(reconnectDelay * 2, 30000) // 指数退避（成功后重置）
    if (reconnectTimer) clearTimeout(reconnectTimer)
    reconnectTimer = setTimeout(connect, delay) // 自动重连 + 服务端补发
  }
  ws.onmessage = (e) => {
    let m: WsServerMsg
    try {
      m = JSON.parse(e.data)
    } catch {
      return // 单条非 JSON 帧丢弃，不断链
    }
    if (m.type === 'session') {
      // 补播完成标记（session 在聊天历史之后发送）：此刻补发离线消息，DOM 顺序正确。
      // 注意：服务端 _replay_snapshot 下发的 sid 是复合键 "用户名/sid"（_get_session 返回 key），
      // 而 WS URL / 下拉选项 / 分享接口 / 持久化需要的都是裸 sid——旧前端原样存放复合键，
      // 刷新或重连时 safe_sid 会把它判非法沦为 junk 会话（记录"丢失"）。此处统一归一为裸 sid。
      setSid(bareSid(m.sid))
      refreshSessions()
      flushOutbox()
    }
    listeners.forEach((fn) => fn(m))
  }
}

/** 组件卸载：停重连、关连接、清计时器（防止泄漏） */
export function teardownSocket() {
  manualClose = true
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = 0 }
  if (pingTimer) { clearInterval(pingTimer); pingTimer = 0 }
  if (busyTimer) { clearInterval(busyTimer); busyTimer = 0 }
  busy.on = false
  busy.seconds = 0
  try { ws?.close() } catch { /* 已断开 */ }
  ws = null
  reconnected = false
  outbox = []
}
