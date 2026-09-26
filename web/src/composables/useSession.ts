/** useSession —— 会话与路书风格（旧 app.js 会话管理 + 风格选择段的平移）。
 *  模块级单例；UI 重置（清聊天/时间线/灯态）由 PlanView 的 switchSession 编排。 */
import { ref } from 'vue'
import { authFetch } from '../api/auth'

export interface SessionItem {
  sid: string
  title: string
}

export const currentSid = ref(localStorage.getItem('tm_sid') || 'default') // 当前会话（每个账号独立命名空间）

export function setSid(sid: string) {
  currentSid.value = sid
  localStorage.setItem('tm_sid', sid)
}

export const sessions = ref<SessionItem[]>([])

/** 会话下拉刷新；当前 sid 不在列表 → 回落 default 并落盘（否则下次刷新又选不中） */
export async function refreshSessions() {
  try {
    const r = await authFetch('/api/sessions')
    if (!r.ok) return
    const list = await r.json()
    sessions.value = list
    if (!list.some((it: SessionItem) => it.sid === currentSid.value)) {
      setSid('default')
    }
  } catch (e) {
    /* 列表刷新失败不影响主流程；401 已由 authFetch 处理 */
  }
}

export async function createSession(): Promise<string | null> {
  try {
    const r = await authFetch('/api/sessions', { method: 'POST' })
    if (!r.ok) return null
    const it = await r.json()
    return it.sid as string
  } catch (e) {
    return null
  }
}

/* ---------- 路书风格（定稿 PDF 使用；列表来自 /api/templates 的 html 主题） ---------- */
export interface StyleItem {
  name: string
  display_name: string
  description?: string
}

export const currentTemplate = ref('lushu') // 最近一次 profile 快照回显的风格
export const styleSyncTried = ref(false) // 风格继承保险位：每会话画像只自动同步一次，杜绝消息循环
export const styleOptions = ref<StyleItem[]>([])

export async function loadStyleOptions() {
  try {
    const r = await fetch('/api/templates')
    if (!r.ok) throw new Error('HTTP ' + r.status)
    const data = await r.json()
    styleOptions.value = (data.templates || []).filter((t: StyleItem & { engine: string }) => t.engine === 'html')
    applyStyleSelection(currentTemplate.value) // 选项就绪后补应用（快照可能先于本请求到达）
  } catch (e) {
    styleOptions.value = [] // 列表不可达时隐藏控件，不影响其它功能
  }
}

export function applyStyleSelection(template: string) {
  if (!styleOptions.value.length) return
  const v = template || 'lushu'
  selectedStyle.value = styleOptions.value.some((o) => o.name === v) ? v : 'lushu' // 旧会话残留值（如 cartoon）不在列表 → 回显默认
}

export const selectedStyle = ref('lushu')

/** 用户手动切换风格：跨对话保持 + 通知服务端 */
export function selectStyle(name: string) {
  if (!name) return
  localStorage.setItem('tm_style', name)
  selectedStyle.value = name
  currentTemplate.value = name
  // ws 发送由 PlanView 调用（避免本模块反向依赖 useSocket）
}
