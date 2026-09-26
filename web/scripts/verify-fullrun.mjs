/**
 * 全流程验收（真实 LLM 跑通企划书 §10.1 验收用例）：
 * 开屏 → 过场 → 登录 → 规划页 → 发送验收用例 → 观察四 Agent 全链路（徽章/通道灯/时间线/草稿卡）
 * → 回复"确认" → 成品卡 + PDF + 订单面板 + 偏好记忆 → 截图存证。
 * 用法：先启动后端，再 node scripts/verify-fullrun.mjs
 */
import { chromium } from 'playwright-core'

const BASE = 'http://127.0.0.1:8000/'
const SHOTS = 'docs/screens'
const CASE = '帮我规划十一成都 3 天游，10 月 1 号从上海出发，高铁往返，两个人，预算 6000 最多 7000，想休闲一点顺便吃吃喝喝，酒店想住春熙路附近 300 到 500 一晚的，必去大熊猫基地。'

const browser = await chromium.launch({ channel: 'msedge' })
const page = await browser.newPage({ viewport: { width: 1440, height: 810 } })
const errors = []
page.on('pageerror', (e) => errors.push(String(e)))
page.on('console', (m) => { if (m.type() === 'error') errors.push('[console] ' + m.text()) })

const log = (...a) => console.log(new Date().toTimeString().slice(0, 8), ...a)

await page.goto('about:blank')
await page.goto(BASE, { waitUntil: 'networkidle' })
await page.evaluate(() => localStorage.clear())

// ① 开屏 → 过场 → 注册页 → 登录
await page.waitForSelector('#btn-start.on', { timeout: 15000 })
await page.screenshot({ path: `${SHOTS}/run-1-splash.png` })
log('① 开屏')
await page.click('#btn-start')
await page.waitForURL(/#\/register/, { timeout: 8000 })
await page.screenshot({ path: `${SHOTS}/run-2-register.png` })
log('② 注册页')
await page.click('#tab-login')
await page.fill('#auth-username', 'uitest')
await page.fill('#auth-password', 'uitest123')
await page.click('#auth-submit')
await page.waitForSelector('.layout', { timeout: 15000 })
await page.waitForTimeout(1500)
await page.screenshot({ path: `${SHOTS}/run-3-plan.png` })
log('③ 规划页')

// ④ 发送验收用例
await page.fill('#input', CASE)
await page.click('#send')
log('④ 已发送验收用例，等待草稿卡（最长 12 分钟）…')
await page.waitForSelector('.draft-card', { timeout: 720000 }).catch(() => log('④ 警告：12 分钟内未出现草稿卡'))
await page.screenshot({ path: `${SHOTS}/run-4-draft.png` })
const draftInfo = await page.evaluate(() => ({
  hasDraft: !!document.querySelector('.draft-card'),
  draftTables: document.querySelectorAll('.draft-card table').length,
  draftRows: document.querySelectorAll('.draft-card tr').length,
  timeline: document.querySelectorAll('.tl-item').length,
  litLights: document.querySelectorAll('.sub-light.st-done, .sub-light.st-running').length,
  profileRows: document.querySelectorAll('#profile-body > div').length,
}))
log('④ 草稿卡：', JSON.stringify(draftInfo))

// ⑤ 回复"确认" → 成品卡 + PDF + 订单 + 记忆（偏好记忆由 LLM 提炼，约 15-30s 后经 WS memory 消息刷新，需等待）
await page.fill('#input', '确认')
await page.click('#send')
log('⑤ 已回复"确认"，等待成品卡（最长 12 分钟）…')
await page.waitForSelector('.final-card', { timeout: 720000 }).catch(() => log('⑤ 警告：12 分钟内未出现成品卡'))
await page.waitForTimeout(3000)
await page.screenshot({ path: `${SHOTS}/run-5-final.png` })
const finalInfo = await page.evaluate(() => ({
  hasFinal: !!document.querySelector('.final-card'),
  pdfHref: document.querySelector('.final-card a.btn-pdf')?.getAttribute('href')?.slice(0, 80) || '',
  ordersPanel: !!document.querySelector('#orders-panel'),
  orderItems: document.querySelectorAll('.order-item').length,
  ordersTotal: document.querySelector('#orders-total')?.textContent || '',
  usageText: document.querySelector('#usage-text')?.textContent || '',
  timeline: document.querySelectorAll('.tl-item').length,
  sessionTitle: document.querySelector('#session-select')?.selectedOptions[0]?.textContent || '',
}))
log('⑤ 成品卡：', JSON.stringify(finalInfo))

// ⑤b 等待偏好记忆沉淀（WS memory 消息触发刷新；提炼约 15-30s）
await page.waitForFunction(() => document.querySelectorAll('.memory-item').length > 0, { timeout: 90000 })
  .catch(() => log('⑤b 警告：90s 内记忆面板未刷新'))
const memoryInfo = await page.evaluate(() => ({
  memoryItems: document.querySelectorAll('.memory-item').length,
  memoryTrips: document.querySelectorAll('.memory-trip').length,
}))
log('⑤b 偏好记忆：', JSON.stringify(memoryInfo))

// ⑥ PDF 链接可达性（<a> 导航即 GET；/api/pdf 只接受 GET，HEAD 会 405）
const pdfOk = finalInfo.pdfHref
  ? await page.evaluate(async (href) => {
      const r = await fetch(href)
      return r.status
    }, finalInfo.pdfHref)
  : 'no-href'
log('⑥ PDF GET 状态：', pdfOk)

log('页面错误：', errors.length ? errors : 'none')
await browser.close()
const ok = draftInfo.hasDraft && finalInfo.hasFinal && finalInfo.orderItems > 0
  && pdfOk === 200 && memoryInfo.memoryItems > 0
log(ok ? 'PASS: 全流程（含真实 LLM 规划链路）跑通' : 'FAIL: 见上方指标')
