/**
 * 地球场景验收：原始版 vs 重建版渲染完好性 + setBoost 加速倍率。
 * 用法：node scripts/verify-earth.mjs [地球.html 路径]
 * 依赖：系统 Edge（channel: msedge），无需下载 playwright 浏览器。
 */
import { chromium } from 'playwright-core'

const ORIG = 'D:/unimportant-files/桌面小程序/地球.原始备份.html'
const BUILT = process.argv[2] || 'D:/unimportant-files/桌面小程序/地球.html'

const browser = await chromium.launch({ channel: 'msedge' })
const page = await browser.newPage({ viewport: { width: 1280, height: 720 } })

async function inspect(file, tag) {
  await page.goto('file:///' + file.replace(/\\/g, '/'), { waitUntil: 'load' })
  await page.waitForTimeout(2500)
  const probe = await page.evaluate(() => {
    const e = window.__earth
    if (!e) return { ok: false }
    return { ok: true, hasSetBoost: typeof e.setBoost === 'function', info: e.info }
  })
  await page.screenshot({ path: `docs/screens/earth-${tag}.png` })
  return probe
}

const orig = await inspect(ORIG, 'original')
const built = await inspect(BUILT, 'rebuilt')
console.log('original probe:', JSON.stringify(orig))
console.log('rebuilt  probe:', JSON.stringify(built))

await page.goto('file:///' + BUILT.replace(/\\/g, '/'), { waitUntil: 'load' })
await page.waitForTimeout(2000)
const rate = await page.evaluate(async () => {
  const e = window.__earth
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
  e.setBoost(1)
  await sleep(2500)
  const a0 = e.info.spin, t0 = e.info.time
  await sleep(1000)
  const a1 = e.info.spin, t1 = e.info.time
  const normal = (a1 - a0) / (t1 - t0)
  e.setBoost(7)
  await sleep(1200)
  const b0 = e.info.spin, u0 = e.info.time
  await sleep(1000)
  const b1 = e.info.spin, u1 = e.info.time
  const boosted = (b1 - b0) / (u1 - u0)
  return { normal: +normal.toFixed(4), boosted: +boosted.toFixed(4), ratio: +(boosted / normal).toFixed(2), fps: e.info.fps }
})
console.log('spin rate:', JSON.stringify(rate))

const ok = built.ok && built.hasSetBoost && orig.ok && rate.ratio >= 5.5 && rate.ratio <= 8
console.log(ok ? 'PASS: 场景完好 + 加速倍率约 7x' : 'FAIL: 场景或加速异常')
await browser.close()
process.exit(ok ? 0 : 1)
