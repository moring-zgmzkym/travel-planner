<script setup lang="ts">
/**
 * PlanView —— 旅游规划主界面（旧 static/index.html 三栏 + static/app.js 628 行的整体平移）。
 * 连接层/会话层在 useSocket/useSession 单例中；本组件负责分发（handleMsg）与全部渲染。
 * 移植 fidelity 约定：
 *  - WS 字段名/心跳/退避/outbox/灯态逻辑逐行对应旧 app.js；
 *  - 仅两处 v-html：欢迎语常量（开发者字符串）与草稿卡（服务端 Jinja2 模板输出，与旧 innerHTML 同语义）；
 *  - esc() 手工转义由模板插值自动转义承接，safeLink() 白名单保留为方法。
 */
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import type { WsOrder, WsServerMsg } from '../types/ws'
import { authFetch, clearToken, getToken, setAuthLostHandler } from '../api/auth'
import {
  busy,
  connect,
  isWsOpen,
  notifyPong,
  onWsMessage,
  reconnectNow,
  sendMsg,
  sendRaw,
  setBusy,
  setSocketHooks,
  teardownSocket
} from '../composables/useSocket'
import {
  applyStyleSelection,
  createSession,
  currentSid,
  currentTemplate,
  loadStyleOptions,
  refreshSessions,
  selectStyle,
  selectedStyle,
  sessions,
  setSid,
  styleOptions,
  styleSyncTried
} from '../composables/useSession'

const router = useRouter()

const AGENT_NAMES: Record<string, string> = {
  Chatter: '聊天管家', InformationProcessor: '信息处理', Researcher: '信息收集',
  BookingButler: 'MCP 专项', Planner: '计划规划', TeamRunner: '调度器',
}
const AGENT_ICONS: Record<string, string> = {
  Chatter: '💬', InformationProcessor: '🗂', Researcher: '🔍',
  BookingButler: '🎫', Planner: '🗺', TeamRunner: '⚙',
}

/* ---------- 状态 ---------- */
interface Me { username: string; is_admin?: boolean }
const me = ref<Me | null>(null)
const connOn = ref(false)

type ChatItem =
  | { kind: 'msg'; id: number; role: 'user' | 'chatter' | 'sys'; text: string }
  | { kind: 'draft'; id: number; html: string }
  | { kind: 'final'; id: number; total: number | string; pdfHref: string; shareVisible: boolean; shareAbs: string; shareId: string }
const chatItems = ref<ChatItem[]>([])
let uid = 0

interface TlItem { id: number; time: string; agent: string; kind: string; text: string }
const timelineItems = ref<TlItem[]>([])

const chatScroll = ref<HTMLElement | null>(null)
const input = ref('')

/* 旅行画像 */
const profileVersion = ref(0)
interface ProfileRow { k?: string; v: string; warn?: boolean }
const profileRows = ref<ProfileRow[]>([])

/* 订单面板 */
const ordersVisible = ref(false)
const ordersTotal = ref('')
const ordersList = ref<WsOrder[]>([])

/* 偏好记忆 */
const prefs = ref<{ id: string; text: string }[]>([])
const trips = ref<{ destination?: string; dates?: string }[]>([])

/* Token 消耗 */
const usagePct = ref(0)
const usageText = ref('0 / 500K')

/* Agent 徽章 / 通道灯 */
const agentFlash = ref<Record<string, boolean>>({})
const chipTimers = new Map<string, number>()
const SUB_PARENT: Record<string, string> = {
  guides: 'Researcher', covers: 'Researcher', foods_img: 'Researcher', spots_img: 'Researcher',
  tickets: 'BookingButler', hotels: 'BookingButler', weather: 'BookingButler', route: 'BookingButler',
}
const SUB_CHANNELS = Object.keys(SUB_PARENT) // 通道单一来源：灯校验与父归属共用
const agentKeys = Object.keys(AGENT_NAMES)
const subStates = ref<Record<string, string>>({})
const agentDone = computed(() => {
  const out: Record<string, boolean> = {}
  for (const agent of ['Researcher', 'BookingButler']) {
    const sts = SUB_CHANNELS.filter((c) => SUB_PARENT[c] === agent)
      .map((c) => subStates.value[c]).filter(Boolean)
    out[agent] = sts.length > 0 && sts.every((s) => s === 'done')
  }
  return out
})

/* ETA chip */
const etaVisible = ref(false)
const etaText = ref('')
let etaRange: [number, number] | null = null

/* 欢迎语（开发者常量，旧 index.html 首条系统消息） */
const WELCOME_HTML = `你好！我是 TripMate 的聊天管家。告诉我<strong>出发地、目的地、天数</strong>，
以及预算、出行方式、酒店偏好、必去景点等（可选），我来为你规划行程。
<br>例如：<em>「帮我规划十一成都 3 天游，10 月 1 号从上海出发，高铁往返，两个人，预算 6000 最多 7000，
想休闲一点顺便吃吃喝喝，酒店想住春熙路附近 300 到 500 一晚的，必去大熊猫基地。」</em>`

/* ---------- 工具 ---------- */
function now() { return new Date().toTimeString().slice(0, 8) }

function safeLink(u?: string): string {
  const s = String(u || '')
  return /^https?:\/\//i.test(s) ? s : '' // 订单外链仅放行 http/https（防 javascript: 注入）
}

function scrollChat() {
  nextTick(() => {
    const el = chatScroll.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

/* ---------- 渲染（旧 app.js 渲染段） ---------- */
function addChat(role: 'user' | 'chatter' | 'sys', text: string) {
  chatItems.value.push({ kind: 'msg', id: ++uid, role, text })
  scrollChat()
}

function addTimeline(agent: string, kind: string, text: string) {
  timelineItems.value.unshift({ id: ++uid, time: now(), agent, kind, text })
  while (timelineItems.value.length > 120) timelineItems.value.pop()
}

function addDraft(html: string) {
  chatItems.value = chatItems.value.filter((i) => i.kind !== 'draft') // 旧逻辑：同时只保留一张草稿卡
  chatItems.value.push({ kind: 'draft', id: ++uid, html })
  scrollChat()
}

function addFinal(m: WsServerMsg) {
  chatItems.value = chatItems.value.filter((i) => i.kind !== 'final')
  const url = m.pdf_url || ''
  const pdfHref = url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(getToken()) // <a> 无法带 Authorization 头，/api/pdf 兼容 query 令牌
  chatItems.value.push({ kind: 'final', id: ++uid, total: m.total_price ?? '', pdfHref, shareVisible: false, shareAbs: '', shareId: '' })
  scrollChat()
  refreshMemory() // 定稿后偏好记忆可能已沉淀
  refreshSessions() // 会话标题由黑板状态派生：定稿后即时刷新为"已完成"
}

function renderProfile(p: WsServerMsg['profile']) {
  if (!p) return
  profileVersion.value = p.version || 0
  const b = (p.basic_info || {}) as Record<string, any>
  const d = (p.detail_info || {}) as Record<string, any>
  if (b.template) {
    currentTemplate.value = b.template as string
    styleSyncTried.value = false // 服务端已有真值：保险位复位
  } else {
    // 新对话未选过风格：继承浏览器偏好，仅当该值在当前选项里才同步服务端——
    // 陈旧主题名会落入 reportlab 注册表，不能喂给服务端
    const saved = localStorage.getItem('tm_style') || ''
    const known = !!saved && styleOptions.value.length && styleOptions.value.some((o) => o.name === saved)
    if (saved && known && !styleSyncTried.value) {
      styleSyncTried.value = true
      sendRaw({ type: 'template', name: saved })
    }
    currentTemplate.value = saved || 'lushu'
  }
  applyStyleSelection(currentTemplate.value) // 回显当前路书风格（旧值不在列表则显示默认）
  const rows: ProfileRow[] = []
  const kv = (k: string, v: unknown) => { if (v) rows.push({ k, v: String(v) }) }
  kv('出发地', b.origin); kv('目的地', b.destination); kv('天数', b.days)
  // 多天行程显示起止范围（"10-01 ~ 10-03"）：逐日用 " ~ " 串联会被误读成三段区间
  const dts = (b.travel_dates || []) as string[]
  kv('日期', dts.length >= 2 ? `${dts[0]} ~ ${dts[dts.length - 1]}` : (dts[0] || b.date_text))
  kv('方式', b.travel_mode); kv('风格', (b.style || []).join('/'))
  kv('人数', b.party_size); kv('预算', b.budget); kv('预算上限', b.budget_max)
  if (d.hotel && (d.hotel.location_pref || (d.hotel.price_range || []).length))
    rows.push({ k: '酒店', v: `${d.hotel.location_pref || ''} ${(d.hotel.price_range || []).join('-')}元/晚` })
  kv('必去', (d.must_visit || []).join('、'))
  kv('忌口', (d.food_restrictions || []).join('、'))
  kv('节奏', d.pace)
  if ((b.defaults_applied || []).length)
    rows.push({ v: `默认值：${(b.defaults_applied || []).join('；')}`, warn: true })
  profileRows.value = rows.length ? rows : []
}

function renderOrders(orders: WsOrder[] | undefined, total: number | string) {
  ordersVisible.value = true
  ordersTotal.value = `合计 ¥${total}`
  ordersList.value = orders || []
}

function renderUsage(u: WsServerMsg['usage']) {
  if (!u) return
  const pct = Math.min(100, (u.total_tokens / (u.limit || 500000)) * 100)
  usagePct.value = pct
  usageText.value = `${u.total_tokens.toLocaleString()} / ${(u.limit / 1000) + 'K'}（${pct.toFixed(1)}%）`
}

/* ---------- 偏好记忆 ---------- */
async function refreshMemory() {
  try {
    const r = await authFetch('/api/memory')
    if (!r.ok) return
    const data = await r.json()
    prefs.value = data.preferences || []
    trips.value = (data.trips || []).slice(0, 5)
  } catch (e) { /* 记忆面板失败不影响主流程；401 已弹注册页 */ }
}

async function deletePref(id: string) {
  try {
    await authFetch(`/api/memory/${id}`, { method: 'DELETE' })
    refreshMemory()
  } catch (e) { /* 401 已处理 */ }
}

async function clearMemory() {
  if (!confirm('确定清空全部偏好记忆？此操作不可恢复。')) return
  try {
    await authFetch('/api/memory', { method: 'DELETE' })
    refreshMemory()
  } catch (e) { /* 401 已处理 */ }
}

/* ---------- PDF 分享 ---------- */
async function makeShare(item: Extract<ChatItem, { kind: 'final' }>) {
  try {
    const r = await authFetch('/api/share', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sid: currentSid.value }),
    })
    const data = await r.json().catch(() => ({}))
    if (r.status !== 200) { addTimeline('System', 'STATUS_ERROR', data.detail || '分享生成失败'); return }
    item.shareAbs = location.origin + data.url
    item.shareId = data.share_id || ''
    item.shareVisible = true
  } catch (e) { /* 401 已弹注册页；其余静默 */ }
}

function copyShare(item: Extract<ChatItem, { kind: 'final' }>) {
  navigator.clipboard && navigator.clipboard.writeText(item.shareAbs)
}

async function revokeShare(item: Extract<ChatItem, { kind: 'final' }>) {
  if (!item.shareId) { item.shareVisible = false; return }
  try {
    await authFetch(`/api/share/${item.shareId}`, { method: 'DELETE' })
  } catch (e) { /* 401 已处理 */ }
  item.shareVisible = false
}

/* ---------- 徽章 / 通道灯 ---------- */
function flashAgent(name: string) {
  agentFlash.value = { ...agentFlash.value, [name]: true }
  const old = chipTimers.get(name)
  if (old) clearTimeout(old)
  // 35s 熄灭（> 服务端 30s 心跳）：团队运行期间心跳持续续亮，徽章不闪灭
  chipTimers.set(name, window.setTimeout(() => {
    agentFlash.value = { ...agentFlash.value, [name]: false }
  }, 35000))
}

/** Subagent 指示灯：黄=运行中 / 绿=完成 / 红=失败；驱动父徽章联动 */
function setSubLight(channel: string | undefined, state: string | undefined) {
  if (!channel || !SUB_CHANNELS.includes(channel)) {
    return // 未知通道忽略（前向兼容：服务端新增通道前端不炸）
  }
  subStates.value = { ...subStates.value, [channel]: state || '' }
  // 父 Agent 徽章联动：本父任一通道运行中 → 黄（点亮）；其全部已运行通道完成 → 绿（见 agentDone）
  const parent = SUB_PARENT[channel]
  if (parent && state === 'running') flashAgent(parent)
}

function resetSubLights() {
  // 新阶段/检查点重跑/终态：灯回灰 + 清父徽章完成绿（防止上一阶段的残留状态误导）
  subStates.value = {}
}

function subLightClass(ch: string): string {
  const s = subStates.value[ch]
  if (s === 'running') return 'st-running'
  if (s === 'done') return 'st-done'
  if (s === 'failed') return 'st-failed'
  return ''
}

/* ETA chip：STATUS_PHASE（含检查点重跑）锚定预计区间，STATUS_PROGRESS 刷新已进行，
   终态（完成/停止/错误）或成品卡片到达即隐藏。 */
function updateEtaChip(m: WsServerMsg) {
  if (m.kind === 'STATUS_COMPLETED' || m.kind === 'STATUS_CANCELLED' || m.kind === 'STATUS_ERROR') {
    etaRange = null
    etaVisible.value = false
    resetSubLights() // 终态：被取消/中断的通道收不到 done，灯全部回灰防"仍在查询"误导
    return
  }
  if (Array.isArray(m.eta_min)) etaRange = m.eta_min
  if (!etaRange) return
  const elapsedMin = typeof m.elapsed_s === 'number' ? Math.round(m.elapsed_s / 60) : null
  const eta = etaRange[0] === etaRange[1] ? `约 ${etaRange[0]} 分钟` : `${etaRange[0]}-${etaRange[1]} 分钟`
  etaText.value = `⏱ 预计 ${eta}` + (elapsedMin !== null ? ` · 已进行 ${elapsedMin} 分` : '')
  etaVisible.value = true
}

function hideEtaChip() {
  etaRange = null
  etaVisible.value = false
}

/* ---------- 消息分发（旧 handleMsg） ---------- */
function handleMsg(m: WsServerMsg) {
  if (m.type === 'session') return // sid/列表/离线补发已在 useSocket 内部处理
  if (m.type === 'status' && m.kind === 'STATUS_SUBAGENT') {
    // Subagent 指示灯专用事件：只驱动灯（黄=运行中/绿=完成/红=失败），不进时间线
    setSubLight(m.channel, m.state)
    return
  }
  if (m.type === 'sub_states') {
    // 刷新/重连快照：恢复指示灯真值——REPLAY 滚动窗口会被心跳挤出
    for (const [ch, st] of Object.entries(m.states || {})) setSubLight(ch, st)
    return
  }
  if (m.type === 'status' || m.type === 'AGENT_MESSAGE') {
    if (m.kind === 'STATUS_PHASE') resetSubLights() // 新阶段/检查点重跑：灯回灰，随查询重新点亮
    addTimeline(m.agent || '', m.kind || m.type, m.text || '')
    flashAgent(m.agent || '')
    updateEtaChip(m)
  } else if (m.type === 'chat') {
    if (m.role === 'user' && !m.replay) return // 实时回显跳过（乐观渲染已画）；补播的需重画（刷新后 DOM 已重置）
    addChat((m.role || 'chatter') as 'user' | 'chatter' | 'sys', m.text || '')
    setBusy(false)
  } else if (m.type === 'draft') {
    addDraft(m.html || '')
  } else if (m.type === 'final') {
    addFinal(m)
    renderOrders(m.orders, m.total_price ?? '')
    hideEtaChip() // 成品已到达，预计等待结束
  } else if (m.type === 'memory') {
    refreshMemory() // 服务端偏好沉淀完成后推送
  } else if (m.type === 'profile') {
    renderProfile(m.profile)
  } else if (m.type === 'usage') {
    renderUsage(m.usage)
  } else if (m.type === 'pong') {
    notifyPong() // 半开连接检测：收到 pong 即视为链路健康
  }
}

/* ---------- 会话切换 / 新建（旧 switchSession/createSession） ---------- */
function switchSession(nextSid: string) {
  if (nextSid === currentSid.value) return
  setSid(nextSid)
  chatItems.value = [] // 欢迎语是模板常驻头部，清空的只是历史（示例是使用指导：切换会话也要保留）
  addChat('sys', '已切换对话。该对话的规划进展与成果如下方所示（历史消息不跨对话保留）。')
  timelineItems.value = []
  hideEtaChip() // 新会话无运行中阶段
  resetSubLights() // 新会话灯回灰（防上一会话的状态串灯；重连补播会按序还原本会话灯态）
  styleSyncTried.value = false // 新会话风格继承保险位复位
  setBusy(false)
  reconnectNow() // 主动断开：立即以新 sid 重连（不走退避），服务端补播该会话状态
}

async function newSession() {
  const sid = await createSession()
  if (sid) {
    await refreshSessions()
    switchSession(sid)
  } else {
    addTimeline('System', 'STATUS_ERROR', '新对话创建失败，请重试。')
  }
}

/* ---------- 发送 / 停止 / 风格 ---------- */
function doSend() {
  const text = input.value.trim()
  if (!text || busy.on) return
  input.value = ''
  setBusy(true)
  sendMsg(text) // 离线时自动进 outbox（含时间线提示），重连后补发
  addChat('user', text) // 乐观渲染（服务端回显时去重）
}

function stopPlan() {
  sendRaw({ type: 'stop' })
}

function onSessionChange(e: Event) {
  switchSession((e.target as HTMLSelectElement).value)
}

function onStyleChange(e: Event) {
  const name = (e.target as HTMLSelectElement).value
  selectStyle(name)
  sendRaw({ type: 'template', name }) // 跨对话保持 + 通知服务端
}

function onInputKey(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); doSend() }
}

function logout() {
  clearToken()
  location.reload() // 重新加载 → hash 仍是 #/app → 路由守卫无令牌弹回开屏地球
}

/* ---------- 生命周期 ---------- */
let offWs: (() => void) | null = null

// 卸载摘除：监听器、徽章计时器、连接层（生命周期钩子必须在 setup 同步注册）
onUnmounted(() => {
  offWs?.()
  for (const t of chipTimers.values()) clearTimeout(t)
  chipTimers.clear()
  teardownSocket()
})

onMounted(async () => {
  // 连接层 → 视图 回调注册
  setSocketHooks({
    onConnChange: (on) => { connOn.value = on },
    onReplayReset: () => { chatItems.value = []; timelineItems.value = [] }, // 先清面板防重连重复渲染，服务端将全量补播
    onReconnected: () => { setBusy(false); addTimeline('System', 'INFO', '连接已恢复，若刚发送的消息没有响应，请重新发送一次。') },
    onOfflineSend: () => addTimeline('System', 'INFO', '当前离线，消息已保存，重连后自动发送。'),
    onOutboxFlush: (texts) => {
      addTimeline('System', 'INFO', `自动补发离线消息 ${texts.length} 条…`)
      for (const t of texts) { sendMsg(t); addChat('user', t) } // 重画回显：服务端收到的才不重发
      setBusy(true) // chatter_lock 串行处理，回复按序到达后逐条解锁
    },
    onNeedLogin: (reason) => router.push({ name: 'register', query: { [reason]: '1' } }),
  })
  setAuthLostHandler((reason) => router.push({ name: 'register', query: { [reason]: '1' } }))
  offWs = onWsMessage(handleMsg)

  loadStyleOptions()

  // boot：/api/me → 用户栏 → 记忆 → 连接
  try {
    const r = await authFetch('/api/me')
    if (r.status !== 200) { router.replace({ name: 'splash' }); return }
    me.value = await r.json()
    refreshMemory()
    connect()
  } catch (e) {
    return // 401 已由 authFetch 处理（跳注册页）
  }
})
</script>

<template>
  <div class="layout plan-enter">
    <aside class="sidebar">
      <div class="brand">
        <div class="logo">✈️</div>
        <div>
          <div class="brand-name">TripMate</div>
          <div class="brand-sub">多 Agent 协同旅游规划系统</div>
        </div>
      </div>

      <div class="user-bar" id="user-bar" v-if="me">
        <span class="user-chip">👤 <span id="user-name">{{ me.username }}{{ me.is_admin ? '（管理员）' : '' }}</span></span>
        <button id="logout" title="退出登录" @click="logout">退出</button>
      </div>

      <div class="panel" id="profile-panel">
        <div class="panel-title">🧳 旅行画像 <span class="ver" id="profile-ver">v{{ profileVersion }}</span></div>
        <div id="profile-body" class="profile-body">
          <template v-if="profileRows.length">
            <div v-for="(r, i) in profileRows" :key="i" :style="r.warn ? 'color:var(--warn);font-size:11px' : undefined">
              <template v-if="r.k"><span class="k">{{ r.k }}</span> {{ r.v }}</template>
              <template v-else>{{ r.v }}</template>
            </div>
          </template>
          <template v-else>等待信息录入…</template>
        </div>
      </div>

      <div class="panel" id="orders-panel" v-if="ordersVisible">
        <div class="panel-title">🧾 推荐订单清单 <span class="chip chip-ok" id="orders-total">{{ ordersTotal }}</span></div>
        <div id="orders-body">
          <div class="order-item" v-for="(o, i) in ordersList" :key="i" :class="{ sel: o.selected }">
            <span class="o-type">{{ o.type }}</span><span v-if="o.selected"> <span class="chip chip-ok">✅ 已勾选</span></span>
            <span class="o-amount">¥{{ o.amount }}</span>
            <div class="o-name">{{ o.name }}</div>
            <div v-if="o.reference_only" class="ref-tag">⚠ 参考值（降级数据）</div>
            <div v-if="o.reason" class="o-reason">{{ o.reason }}</div>
            <a v-if="safeLink(o.link)" :href="safeLink(o.link)" target="_blank" rel="noopener">直达链接 ↗</a>
          </div>
        </div>
      </div>

      <div class="panel" id="memory-panel">
        <div class="panel-title">🧠 偏好记忆
          <button class="mini-btn" id="memory-clear" title="清空全部偏好记忆" @click="clearMemory">清空</button>
        </div>
        <div class="memory-body" v-if="prefs.length || trips.length">
          <div class="memory-item" v-for="p in prefs" :key="p.id">
            <span>{{ p.text }}</span>
            <button class="mini-btn mem-del" title="删除该条" @click="deletePref(p.id)">✕</button>
          </div>
          <div class="memory-trip" v-for="(t, i) in trips" :key="i">🧭 {{ t.destination || '行程' }} · {{ t.dates || '' }}</div>
        </div>
        <div v-else class="memory-body"><span class="memory-empty">暂无沉淀，完成一次规划后自动提炼</span></div>
      </div>

      <div class="panel">
        <div class="panel-title">📊 Token 消耗（本次规划）</div>
        <div class="usage-bar"><div class="usage-fill" id="usage-fill" :style="{ width: usagePct + '%' }"></div></div>
        <div class="usage-text" id="usage-text">{{ usageText }}</div>
      </div>
    </aside>

    <main class="chat-col">
      <header class="chat-head">
        <div class="session-bar">
          <select id="session-select" title="切换对话（每个对话独立规划）" :value="currentSid" @change="onSessionChange">
            <option v-for="s in sessions" :key="s.sid" :value="s.sid">{{ s.title }}</option>
          </select>
          <button id="new-session" title="开启一个新的规划对话" @click="newSession">＋ 新对话</button>
          <select
            v-if="styleOptions.length"
            id="style-select"
            title="路书风格（定稿 PDF 使用）"
            :value="selectedStyle"
            @change="onStyleChange"
          >
            <option v-for="s in styleOptions" :key="s.name" :value="s.name" :title="s.description || ''">🖨 {{ s.display_name }}</option>
          </select>
        </div>
        <div class="agents-row">
          <span class="agent-chip" v-for="a in agentKeys" :key="a"
            :data-agent="a" :class="{ on: agentFlash[a], done: agentDone[a] }">
            {{ AGENT_ICONS[a] }} {{ AGENT_NAMES[a] }}
          </span>
        </div>
        <div class="subs-row" title="Subagent 工作指示灯：灰=未运行 · 黄=查询中 · 绿=完成 · 红=失败（自动降级并附说明）">
          <span class="sub-light" v-for="(label, ch) in { guides: '🔍 攻略', covers: '🖼 封面', foods_img: '🍜 美食图', spots_img: '📸 景点图', tickets: '🚄 车票', hotels: '🏨 酒店', weather: '🌤 天气', route: '🗺 路线' }" :key="ch"
            :data-channel="ch" :class="subLightClass(ch)"><i></i>{{ label }}</span>
        </div>
        <div class="head-right">
          <span class="agent-chip" id="eta-chip" v-if="etaVisible">{{ etaText }}</span>
          <button id="stop-plan" title="停止当前规划任务（已收集数据保留）" @click="stopPlan">⏹ 停止规划</button>
          <div class="conn-dot" id="conn-dot" :class="{ on: connOn }"></div>
        </div>
      </header>

      <div class="chat-scroll" id="chat" ref="chatScroll">
        <div class="msg sys" v-html="WELCOME_HTML"></div>
        <template v-for="item in chatItems" :key="item.id">
          <div v-if="item.kind === 'msg'" class="msg" :class="item.role">{{ item.text }}</div>
          <div v-else-if="item.kind === 'draft'" class="draft-card" v-html="item.html"></div>
          <div v-else-if="item.kind === 'final'" class="final-card">
            <div class="final-title">🎉 行程计划已生成</div>
            <a class="btn-pdf" :href="item.pdfHref" target="_blank">📄 打开 PDF 行程计划</a>
            <button class="btn-share" @click="makeShare(item)">🔗 生成分享链接</button>
            <div class="share-row" v-if="item.shareVisible">
              <input class="share-input" readonly :value="item.shareAbs">
              <button class="mini-btn share-copy" @click="copyShare(item)">复制</button>
              <button class="mini-btn share-revoke" @click="revokeShare(item)">撤销</button>
            </div>
            <div class="final-note">已勾选订单合计约 {{ item.total }} 元，请在官方渠道逐项确认并支付。</div>
          </div>
        </template>
      </div>
      <div class="composer">
        <textarea id="input" v-model="input" rows="2" placeholder="输入消息，Enter 发送（Shift+Enter 换行）…" @keydown="onInputKey"></textarea>
        <button id="send" :disabled="busy.on" @click="doSend">{{ busy.on ? (busy.seconds >= 60 ? `思考中… ${Math.floor(busy.seconds / 60)}分${busy.seconds % 60}秒` : `思考中… ${busy.seconds}秒`) : '发送' }}</button>
      </div>
    </main>

    <aside class="timeline-col">
      <div class="panel-title">🛰 Agent 工作状态</div>
      <div class="timeline" id="timeline">
        <div class="tl-item" v-for="t in timelineItems" :key="t.id" :class="'k-' + t.kind">
          <span class="tl-time">{{ t.time }}</span>
          <div class="tl-body">
            <div class="tl-agent" :class="'a-' + t.agent">{{ AGENT_NAMES[t.agent] || t.agent }}</div>
            <div class="tl-text">{{ t.text }}</div>
          </div>
        </div>
      </div>
    </aside>
  </div>
</template>

<style scoped>
/* 规划页入场：叠在像素中国背景上淡入（0.45s 纯 opacity，无位移——位移会让边缘透出
   舞台底；时长与 AuthView 放行处的 PLAN_ENTER_MS 手工对应）。 */
.plan-enter { animation: planIn 0.45s ease both; }
@keyframes planIn { from { opacity: 0; } to { opacity: 1; } }
</style>
