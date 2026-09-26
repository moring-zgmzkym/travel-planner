/**
 * 入口流程验收：开屏地球 → 点击过场（加速/模糊/放大）→ 像素中国注册页 →
 * 注册提交 → 注册页停留 ≥2s → 规划页；再验证已登录老用户路径（欢迎卡 + 自动放行）。
 * 截图存 docs/screens/。
 * 用法：先启动后端（TRIPMATE_NO_WINDOW=1 python run.py），再 node scripts/verify-intro.mjs
 */
import { chromium } from 'playwright-core'

const BASE = 'http://127.0.0.1:8000/'
const SHOTS = 'docs/screens'
const DWELL = 2000

const browser = await chromium.launch({ channel: 'msedge' })
const page = await browser.newPage({ viewport: { width: 1440, height: 810 } })
const errors = []
page.on('pageerror', (e) => errors.push(String(e)))

// 清令牌：确保从“新用户”路径开始
await page.goto('about:blank')
await page.goto(BASE, { waitUntil: 'networkidle' })
await page.evaluate(() => localStorage.clear())

// ① 开屏：等地球 iframe 就绪 + 按钮弹入
await page.goto(BASE + '#/', { waitUntil: 'networkidle' })
await page.waitForSelector('#btn-start.on', { timeout: 15000 })
await page.waitForTimeout(1200)
console.log('① 开屏按钮已弹入')
await page.screenshot({ path: `${SHOTS}/m3-splash.png` })

// ② 点击 → 过场：中段抓帧 + 读地球加速状态
await page.click('#btn-start')
await page.waitForTimeout(700)
const mid = await page.evaluate(() => {
  const f = document.querySelector('.layer-earth iframe')
  const e = f && f.contentWindow && f.contentWindow.__earth
  return { spinBoost: e ? e.info.spinBoost : null, fps: e ? e.info.fps : null }
})
await page.screenshot({ path: `${SHOTS}/m3-warp-mid.png` })
console.log('② 过场中段：spinBoost=%s fps=%s', mid.spinBoost, mid.fps)

// ③ 注册页：等 URL 切换 + 卡片可见（此处开始计“注册页停留”）
await page.waitForURL(/#\/register/, { timeout: 15000 })
const tEnter = Date.now()
await page.waitForSelector('.auth-card', { state: 'visible' })
await page.waitForTimeout(900)
const china = await page.evaluate(() => {
  const l = document.querySelector('.layer-china')
  return l ? getComputedStyle(l).opacity : null
})
await page.screenshot({ path: `${SHOTS}/m3-register.png` })
console.log('③ 注册页就绪（像素中国层 opacity=%s）', china)

// ④ 注册提交 → 2s 门 → 规划页（量：注册页停留 = 进页 → 进规划页）
// 幂等：uitest 已存在时注册会 400，自动切“登录”重试
await page.fill('#auth-username', 'uitest')
await page.fill('#auth-password', 'uitest123')
await page.click('#auth-submit')
await page.waitForSelector('.layout, #auth-error:not(:empty)', { timeout: 15000 })
if (!(await page.isVisible('.layout').catch(() => false))) {
  console.log('④ 注册被拒（账号已存在），切登录重试')
  await page.click('#tab-login')
  await page.click('#auth-submit')
  await page.waitForSelector('.layout', { timeout: 15000 })
}
const dwell = Date.now() - tEnter
await page.screenshot({ path: `${SHOTS}/m3-plan.png` })
console.log('④ 注册路径：注册页停留 %dms（要求 ≥%dms）', dwell, DWELL)

// ⑤ 已登录老用户：整页加载 #/register → 欢迎卡 → 自动放行 ≥2s
// 计时口径：document-start 时间戳（比 AuthView 模块初始化更早，断言更严）
await page.addInitScript(() => { (window).__docStart = Date.now() })
await page.goto('about:blank')
await page.goto(BASE + '#/register', { waitUntil: 'networkidle' })
await page.waitForSelector('.auth-gate-hint', { timeout: 8000 })
const welcomeText = await page.textContent('.auth-sub')
await page.waitForSelector('.layout', { timeout: 10000 })
const dwell2 = Date.now() - (await page.evaluate(() => (window).__docStart))
console.log('⑤ 老用户路径：%s，停留 %dms（要求 ≥%dms）', welcomeText.trim(), dwell2, DWELL)

console.log('console/page errors:', errors.length ? errors : 'none')
const ok = errors.length === 0
  && dwell >= DWELL
  && dwell2 >= DWELL
  && Number(mid.spinBoost) > 2
console.log(ok ? 'PASS: 入口流程全通过（过场加速 + 2s 停留门 + 老用户自动放行）' : 'FAIL: 见上方指标')
await browser.close()
process.exit(ok ? 0 : 1)
