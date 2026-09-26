/**
 * 规划页验收：开屏 → 过场 → 登录 → 规划页三栏完整性 → 真实发送消息打通 WS →
 * 断言回执/时间线/连接灯 → 新对话切换 → 退出登录回开屏。
 * 用法：先启动后端（TRIPMATE_NO_WINDOW=1 python run.py），再 node scripts/verify-plan.mjs
 */
import { chromium } from 'playwright-core'

const BASE = 'http://127.0.0.1:8000/'
const SHOTS = 'docs/screens'

const browser = await chromium.launch({ channel: 'msedge' })
const page = await browser.newPage({ viewport: { width: 1440, height: 810 } })
const errors = []
page.on('pageerror', (e) => errors.push(String(e)))

await page.goto('about:blank')
await page.goto(BASE, { waitUntil: 'networkidle' })
await page.evaluate(() => localStorage.clear())

// ① 开屏 → 过场 → 注册页
await page.waitForSelector('#btn-start.on', { timeout: 15000 })
await page.click('#btn-start')
await page.waitForURL(/#\/register/, { timeout: 8000 })

// ② 登录（uitest 已在 verify-intro 中注册；幂等：不存在则先注册）
await page.click('#tab-login')
await page.fill('#auth-username', 'uitest')
await page.fill('#auth-password', 'uitest123')
await page.click('#auth-submit')
await page.waitForSelector('.layout, #auth-error:not(:empty)', { timeout: 15000 })
if (!(await page.isVisible('.layout').catch(() => false))) {
  await page.click('#tab-register')
  await page.click('#auth-submit')
  await page.waitForSelector('.layout', { timeout: 15000 })
}

// ③ 规划页完整性断言
await page.waitForSelector('.layout', { timeout: 12000 })
await page.waitForTimeout(1500)
const checks = await page.evaluate(() => ({
  userBar: !!document.querySelector('#user-bar') && document.querySelector('#user-bar').offsetParent !== null,
  userName: document.querySelector('#user-name')?.textContent || '',
  agentChips: document.querySelectorAll('.agent-chip[data-agent]').length,
  subLights: document.querySelectorAll('.sub-light[data-channel]').length,
  channels: [...document.querySelectorAll('.sub-light')].map((el) => el.dataset.channel).join(','),
  welcome: !!document.querySelector('.msg.sys'),
  connOn: !!document.querySelector('#conn-dot.on'),
  sessionOptions: document.querySelector('#session-select')?.options.length || 0,
  styleSelect: !!document.querySelector('#style-select'),
  memoryEmpty: document.querySelector('#memory-body')?.textContent || '',
  ordersHidden: !document.querySelector('#orders-panel'),
  timelineItems: document.querySelectorAll('.tl-item').length,
}))
console.log('③ 规划页检查：', JSON.stringify(checks, null, 0))
await page.screenshot({ path: `${SHOTS}/m4-plan-view.png` })

// ④ 真实发送一条消息，打通 WS 收发
await page.fill('#input', '你好，介绍一下你自己')
await page.click('#send')
await page.waitForSelector('.msg.user', { timeout: 5000 })
const tSend = Date.now()
await page.waitForFunction(
  () => document.querySelectorAll('.msg.chatter').length > 0,
  { timeout: 60000 }
).catch(() => console.log('④ 警告：60s 内未收到 chatter 回复（模型侧延迟）'))
const replyMs = Date.now() - tSend
const afterChat = await page.evaluate(() => ({
  chatterMsgs: document.querySelectorAll('.msg.chatter').length,
  timeline: document.querySelectorAll('.tl-item').length,
  busy: document.querySelector('#send')?.textContent || '',
  profileRows: document.querySelectorAll('#profile-body > div').length,
}))
console.log(`④ 发送"你好"→ 收到回复 ${replyMs}ms：`, JSON.stringify(afterChat))

// ⑤ 新对话切换：tm_sid 变为新的 s-* 会话（"已切换对话"系统消息会被重连补播清空——
//    与旧版行为一致，故以 sid 变更为功能判据）
const before = await page.evaluate(() => localStorage.getItem('tm_sid'))
await page.click('#new-session')
await page.waitForFunction(
  (prev) => {
    const v = localStorage.getItem('tm_sid')
    return v && v !== prev && /^s-/.test(v)
  },
  before,
  { timeout: 8000 }
).catch(() => console.log('⑤ 警告：tm_sid 未在 8s 内变为新会话'))
const after = await page.evaluate(() => ({
  sid: localStorage.getItem('tm_sid'),
  selValue: document.querySelector('#session-select')?.value || '',
  options: [...document.querySelectorAll('#session-select option')].map((o) => o.value).length,
}))
console.log('⑤ 新对话：', JSON.stringify({ before, ...after }))

// ⑥ 退出登录 → 回开屏
await page.click('#logout')
await page.waitForTimeout(2500)
const finalUrl = page.url()
const backToSplash = await page.evaluate(() => !!document.querySelector('#btn-start') || !!document.querySelector('.stub-screen'))
console.log('⑥ 退出后 URL：', finalUrl, '开屏元素：', backToSplash)

console.log('console/page errors:', errors.length ? errors : 'none')
const ok = errors.length === 0
  && checks.agentChips === 6
  && checks.subLights === 8
  && checks.channels === 'guides,covers,foods_img,spots_img,tickets,hotels,weather,route'
  && checks.connOn
  && checks.welcome
  && checks.userBar
  && afterChat.chatterMsgs > 0
  && !!after.sid && after.sid !== before && /^s-/.test(after.sid)
  && backToSplash
console.log(ok ? 'PASS: 规划页移植 + WS 闭环 + 会话切换 + 登出回开屏全部通过' : 'FAIL: 见上方指标')
await browser.close()
process.exit(ok ? 0 : 1)
