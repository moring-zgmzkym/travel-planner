/**
 * 过场与舞台验收（2026-09-25 第四轮：精确时序化——1.5s 补间自转到中国 + 0.5s 停展 +
 * 1s 渐变模糊放大 + 0.6s 交叉淡入，总 ~3.15s）：
 *  A. 门控断言——中国层 iframe 首帧就绪前：中国层 opacity 恒 0 且地球层 opacity 恒 1；
 *     且 fading 开始前中国层 opacity 恒 0（china-on 漏 spinning 会让中国层盖住地球）；
 *  B. 时序断言——旋转相位（spinning）~2s；停展窗 t∈[1600,1950] spin 漂移 <0.03rad；
 *     点击→注册页总时长 ∈[2.8, 4.5]s（页内相位计时）；
 *  C. 旋转断言——补间落点 landErr≤0.1rad；累计旋转/最大 boost 阈值随初始角距缩放
 *     （补间固定 1.5s，小角距天然小，固定值会误报）；|delta|<0.05 豁免；
 *  D. 旋转中 FPS ≥25；视觉证据三帧（旋转中/停展/模糊中）；
 *  E. 注册页→规划页：中国层 opacity 恒 1 直到规划页 .layout 挂载；规划页舞台透明无 has-scene。
 * 用法：先启动后端，再 node scripts/verify-warp.mjs
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
await page.waitForSelector('#btn-start.on', { timeout: 15000 })
// 等地球层入场动画（layerIn 0.9s）爬到 opacity=1 再开始，否则会把入场爬坡误判为"地球先撤"
await page.waitForFunction(
  () => {
    const e = document.querySelector('.layer-earth')
    return e && getComputedStyle(e).opacity === '1'
  },
  { timeout: 5000 }
)

// 初始旋转计划（小角距豁免用）
const initial = await page.evaluate(() => {
  const ef = document.querySelector('.layer-earth iframe')
  const e = ef && ef.contentWindow && ef.contentWindow.__earth
  if (!e || typeof e.faceLongitude !== 'function') return { hasProbe: false, delta: 9 }
  return { hasProbe: true, delta: e.faceLongitude(105).delta }
})

// A/B/C：点击与采样放在同一个页内任务里——若用 page.click() 再接采样，
// Playwright 每步往返 + 截图耗时 ~1s，会漏掉过场开头窗口（china-on 事故的盲区）；
// 采样自身也只做 rAF 轮询，不再夹带截图
const warp = await page.evaluate(async () => {
  document.querySelector('#btn-start').click()
  const out = []
  const s0 = performance.now()
  while (performance.now() - s0 < 8000) {
    const earth = document.querySelector('.layer-earth')
    const china = document.querySelector('.layer-china')
    const ef = document.querySelector('.layer-earth iframe')
    const cf = document.querySelector('.layer-china iframe')
    const probe = ef && ef.contentWindow && ef.contentWindow.__earth
    out.push({
      t: Math.round(performance.now() - s0),
      hash: location.hash,
      spinning: !!document.querySelector('.stage.spinning'),
      warping: !!document.querySelector('.stage.warping'),
      fading: !!document.querySelector('.stage.fading'),
      earthOpacity: earth ? getComputedStyle(earth).opacity : null,
      chinaOpacity: china ? getComputedStyle(china).opacity : null,
      chinaReady: !!(cf && cf.dataset.loaded === '1'),
      earthExists: !!earth,
      btnOp: (() => { const b = document.querySelector('#btn-start'); return b ? +getComputedStyle(b).opacity : null })(),
      spin: probe ? probe.info.spin : null,
      spinBoost: probe ? probe.info.spinBoost : null,
      targetSpin: probe && typeof probe.faceLongitude === 'function' ? probe.faceLongitude(105).targetSpin : null,
      fps: probe ? probe.info.fps : null
    })
    await new Promise((r) => requestAnimationFrame(r))
  }
  return out
})
await page.screenshot({ path: `${SHOTS}/warp-sampled.png` })

// A：中国层未就绪 → 不得淡入；就绪前地球不得淡出
const badChinaFade = warp.filter((s) => !s.chinaReady && Number(s.chinaOpacity) > 0)
const badEarthFade = warp.filter((s) => !s.chinaReady && s.earthExists && Number(s.earthOpacity) < 1)
// A2（china-on 相位事故守护）：从点击到 fading 开始之前，中国层 opacity 必须为 0
// （曾经的 bug：china-on 条件漏了 spinning，旋转+停展全程中国层 opacity=1 盖住地球）
// 注意口径：fading 开始后允许淡入；过场结束（fading 复位、china-on 接棒）到路由跳转的
// 几帧也是合法终态——所以作用域是 [0, fading 首帧)，不是整个采样窗
const fadeStartIdx = warp.findIndex((s) => s.fading)
const badChinaEarly = warp.slice(0, fadeStartIdx === -1 ? warp.length : fadeStartIdx)
  .filter((s) => Number(s.chinaOpacity) > 0)
// A3（spinning 相位事故守护）：点击后旋转期地球层必须持续在场、boost 必须真实生效
const earthGoneEarly = warp.filter((s) => s.t < 4000 && !s.earthExists).length
const boostSeen = warp.some((s) => s.spinBoost > 0.5)
// A4（按钮复活事故守护）：点击 1.2s 后（淡出早已完成）按钮 opacity 必须恒 0 直至路由跳转
// ——曾经的 bug：3s 兜底计时器没清，过场中按钮又浮现（2026-09-25 第四轮实测）
const badBtnRevive = warp.filter((s) => s.t > 1200 && !s.hash.includes('#/register') && (s.btnOp || 0) > 0.05)
// 精确时序断言：① 旋转相位（spinning 在场）应覆盖 ~2s（1.5s 补间 + 0.5s 停展）；
// ② 停展窗 t∈[1600,1950] 内地球必须静止（spin 漂移 <0.03rad）
const spinSamples = warp.filter((s) => s.spinning)
const spinPhaseMs = spinSamples.length > 1 ? spinSamples[spinSamples.length - 1].t - spinSamples[0].t : 0
const pauseSamples = warp.filter((s) => s.t >= 1600 && s.t <= 1950 && s.spin != null)
const pauseDrift = pauseSamples.length > 1
  ? Math.max(...pauseSamples.map((s) => s.spin)) - Math.min(...pauseSamples.map((s) => s.spin)) : 99
// B：旋转量与到达精度（阈值随初始角距缩放：补间固定 1.5s，小角距的 boost/旋转量天然小，
//    用固定值会误报——2026-09-25 第四轮详审修正；|delta|<0.05 的"恰好已朝中国"豁免）
const spins = warp.filter((s) => s.spin != null).map((s) => s.spin)
const cumSpin = spins.length > 1 ? Math.abs(spins[spins.length - 1] - spins[0]) : 0
const maxBoost = Math.max(0, ...warp.map((s) => s.spinBoost || 0))
const finalWithProbe = warp.filter((s) => s.spin != null).pop()
const landErr = finalWithProbe && finalWithProbe.targetSpin != null
  ? Math.abs(finalWithProbe.spin - finalWithProbe.targetSpin) : 99
const absDelta = Math.abs(initial.delta)
const exempt = absDelta < 0.05
const spinOk = exempt ? true : (cumSpin >= absDelta * 0.85 && maxBoost >= Math.min(5, absDelta * 1.8) && landErr <= 0.1)
// C：FPS 与总时长
const fpsSamples = warp.filter((s) => s.fps != null && s.spinBoost > 0.5).map((s) => s.fps)
const fpsMedian = fpsSamples.length ? fpsSamples.sort((a, b) => a - b)[Math.floor(fpsSamples.length / 2)] : 0
console.log(`A 未就绪淡入违规 ${badChinaFade.length} 帧 / 地球先撤违规 ${badEarthFade.length} 帧 / 淡入前中国层可见违规 ${badChinaEarly.length} 帧 / 按钮复活违规 ${badBtnRevive.length} 帧`)
console.log(`A2 旋转期地球缺席 ${earthGoneEarly} 帧 / boost 生效 ${boostSeen}`)
console.log(`A2 旋转相位持续 ${spinPhaseMs}ms（应 ~2000）；停展窗 spin 漂移 ${pauseDrift.toFixed(4)}rad（应 <0.03）`)
console.log(`B 初始角距 ${initial.hasProbe ? initial.delta.toFixed(2) + 'rad' : '无探针'}；累计旋转 ${cumSpin.toFixed(2)}rad；最大 boost ${maxBoost}；落点误差 ${landErr.toFixed(3)}rad`)
console.log(`C 旋转中 FPS 中位数 ${fpsMedian}`)

// 等过场结束进注册页；总时长用页内相位计时（首个 boost>0.5 帧 → #/register 首帧），
// 不能用采样器墙钟——采样器固定跑 8s，URL 早已切换也会被拖满（2026-09-25 度量坑）
await page.waitForURL(/#\/register/, { timeout: 15000 })
const firstSpin = warp.find((s) => (s.spinBoost || 0) > 0.5) || warp[0]
const entered = warp.find((s) => s.hash.includes('#/register'))
const warpMs = firstSpin && entered ? entered.t - firstSpin.t : -1
await page.screenshot({ path: `${SHOTS}/warp-register.png` })
const regStage = await page.evaluate(() => {
  const s = document.querySelector('.stage')
  const china = document.querySelector('.layer-china')
  return { hasScene: s.classList.contains('has-scene'), chinaOpacity: china ? getComputedStyle(china).opacity : null }
})
console.log(`过场总时长（页内相位计时）${warpMs}ms；注册页舞台：${JSON.stringify(regStage)}`)
const durationOk = warpMs >= 2800 && warpMs <= 4500
const timingOk = spinPhaseMs >= 1900 && spinPhaseMs <= 2300 && pauseDrift < 0.03
const aliveOk = earthGoneEarly === 0 && (boostSeen || exempt)

// 登录 → 2s 门 → 规划页；采样放行瞬间
const gateSampler = page.evaluate(async () => {
  const out = []
  const t0 = performance.now()
  while (performance.now() - t0 < 6000) {
    const china = document.querySelector('.layer-china')
    const layout = document.querySelector('.layout')
    out.push({
      t: Math.round(performance.now() - t0),
      chinaOpacity: china ? getComputedStyle(china).opacity : null,
      layoutExists: !!layout
    })
    await new Promise((r) => requestAnimationFrame(r))
  }
  return out
})
await page.click('#tab-login')
await page.fill('#auth-username', 'uitest')
await page.fill('#auth-password', 'uitest123')
await page.click('#auth-submit')
const gate = await gateSampler
// C：规划页挂载前中国层不得先淡出（opacity 不存在于 (0,1) 开区间）
const badPlanFade = gate.filter((s) => !s.layoutExists && s.chinaOpacity != null && Number(s.chinaOpacity) < 1)
console.log(`C 规划页挂载前中国层先撤违规 ${badPlanFade.length} 帧`)

await page.waitForSelector('.layout', { timeout: 8000 })
await page.waitForTimeout(800)
// D：规划页常态舞台必须透明、无 has-scene
const planStage = await page.evaluate(() => {
  const s = document.querySelector('.stage')
  return { hasScene: s.classList.contains('has-scene'), bg: getComputedStyle(s).backgroundColor, chinaLayer: !!document.querySelector('.layer-china') }
})
console.log('规划页舞台：', JSON.stringify(planStage))
await page.screenshot({ path: `${SHOTS}/warp-plan-gaps.png` })

// 视觉证据（新开一页，状态条件触发抓帧——条件用状态而非时刻，容忍截图耗时；
// 每帧拍完后立即回读状态作为该帧的真实相位标注，截图延迟导致的相位偏移不误导）
const frames = []
const page2 = await browser.newPage({ viewport: { width: 1440, height: 810 } })
await page2.goto('about:blank')
await page2.goto(BASE, { waitUntil: 'networkidle' })
await page2.evaluate(() => localStorage.clear())
await page2.waitForSelector('#btn-start.on', { timeout: 15000 })
await page2.waitForFunction(() => {
  const e = document.querySelector('.layer-earth')
  return e && getComputedStyle(e).opacity === '1'
}, { timeout: 5000 })
await page2.evaluate(() => document.querySelector('#btn-start').click())
const readState = () => page2.evaluate(() => {
  const stage = document.querySelector('.stage')
  return {
    spinning: !!stage.classList.contains('spinning'),
    warping: !!stage.classList.contains('warping'),
    fading: !!stage.classList.contains('fading'),
    chinaOp: document.querySelector('.layer-china') ? +getComputedStyle(document.querySelector('.layer-china')).opacity : null,
    earthBlur: (() => { const e = document.querySelector('.layer-earth'); const m = e && getComputedStyle(e).filter.match(/blur\(([\d.]+)px\)/); return m ? +m[1] : 0 })()
  }
})
const want = ['spin', 'pause', 'blur', 'xfade']
async function grab(name) {
  await page2.screenshot({ path: `${SHOTS}/warp-frame-${name}.png` })
  frames.push(`${name}@${JSON.stringify(await readState())}`)
  want.splice(want.indexOf(name), 1)
}
for (let i = 0; i < 300 && want.length; i++) {
  const st = await page2.evaluate(() => {
    const stage = document.querySelector('.stage')
    const ef = document.querySelector('.layer-earth iframe')
    const p = ef && ef.contentWindow && ef.contentWindow.__earth
    return {
      spinning: !!stage.classList.contains('spinning'),
      warping: !!stage.classList.contains('warping'),
      fading: !!stage.classList.contains('fading'),
      tweening: !!(p && p.info.spinning),
      spinBoost: p ? p.info.spinBoost : 0
    }
  })
  if (want.includes('spin') && st.spinning && st.tweening && st.spinBoost > 20) await grab('spin')
  else if (want.includes('pause') && st.spinning && !st.tweening) await grab('pause')
  else if (want.includes('blur') && st.warping && !st.fading) await grab('blur')
  else if (want.includes('xfade') && st.fading) await grab('xfade')
  await new Promise((r) => setTimeout(r, 40))
}
console.log('视觉证据抓帧（帧名@拍摄时刻状态）：')
for (const f of frames) console.log('  ' + f)
await page2.close()

console.log('页面错误：', errors.length ? errors : 'none')
const ok = errors.length === 0
  && badChinaFade.length === 0
  && badEarthFade.length === 0
  && badChinaEarly.length === 0
  && badBtnRevive.length === 0
  && badPlanFade.length === 0
  && aliveOk
  && timingOk
  && spinOk
  && durationOk
  && fpsMedian >= 25
  && regStage.hasScene && Number(regStage.chinaOpacity) === 1
  && !planStage.hasScene && planStage.bg === 'rgba(0, 0, 0, 0)'
console.log(ok ? 'PASS: 旋转到中国(1.5s)/停展(0.5s)/渐变模糊(1s)/无缝衔接/门控/无暗帧/规划页无黑底' : 'FAIL: 见上方指标')
await browser.close()
process.exit(ok ? 0 : 1)
