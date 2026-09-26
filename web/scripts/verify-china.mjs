/**
 * 像素中国重建验收：天空装饰（体素云/双编队鸟/偶现飞机）。
 * 用法：node scripts/verify-china.mjs [像素中国.html 路径]
 *
 * 背景：相机俯角 32°，视锥顶边仅低于水平线约 7°——画面上半部是远景海面/地形的平坦带，
 * 装饰必须位于相机(38)下方 11-16 单位、相机前方 ≲100 单位才会入画（剪影于平坦远背景）。
 *
 * 性能注意（踩过的坑）：__china.advance(N) 会在主线程同步跑 N×60 帧；无头 Edge 是软件
 * WebGL（SwiftShader），每帧数十毫秒——累计推进上千万 = CPU 打满。因此本脚本：
 * ① 视口缩到 800×450（软件光栅化成本约 1/4）；② 只做小步长（+20s）相对推进；
 * ③ 模拟总时长封顶约 100s；④ 只对“有装饰入画”的时点截图。
 *
 * 检查：① 多时点统计画面内(NC)的云/鸟/飞机数量（出场率）；② holeScan()==0；
 *       ③ 有装饰入画的时点截图供目检。
 */
import { chromium } from 'playwright-core'

const BUILT = process.argv[2] || 'D:/unimportant-files/桌面小程序/像素中国.html'
const TARGETS = [10, 30, 60, 90, 120, 160, 190, 220] // 小步长推进的目标时刻（秒；窗口覆盖 ≥3 次飞机横穿）

const t0 = Date.now()
const browser = await chromium.launch({ channel: 'msedge' })
const page = await browser.newPage({ viewport: { width: 800, height: 450 } })
await page.goto('file:///' + BUILT.replace(/\\/g, '/'), { waitUntil: 'load' })
await page.waitForTimeout(2500)

const rows = []
let last = 0
for (const target of TARGETS) {
  const r = await page.evaluate((delta) => {
    const c = window.__china
    c.advance(delta) // 相对推进（步长 ≤20s，勿传累计值）
    const scene = c.terrain.parent
    const decor = scene.children.find((o) => o !== c.terrain && o.type === 'Group')
    const clouds = decor.children.filter((o) => o.userData && o.userData.speed !== undefined)
    const birds = decor.children.filter((o) => o.userData && o.userData.wl !== undefined)
    const plane = decor.children.find((o) => !o.userData || (!o.userData.speed && !o.userData.wl))
    const onScreen = (v) => {
      const p = v.clone().project(c.camera)
      return p.z < 1 && Math.abs(p.x) < 1 && Math.abs(p.y) < 1
    }
    return {
      t: Math.round(c.info.time),
      clouds: clouds.filter((cl) => onScreen(cl.position)).length,
      birds: birds.filter((b) => onScreen(b.position)).length,
      plane: plane.visible && onScreen(plane.position) ? 1 : 0,
      holes: c.holeScan().holes,
      tris: c.info.tris
    }
  }, target - last)
  last = target
  rows.push(r)
  if (r.clouds + r.birds + r.plane > 0) {
    await page.screenshot({ path: `docs/screens/china-built-t${r.t}.png` })
  }
}

console.log('重建版采样：', JSON.stringify(rows))
const trisOk = rows.every((r) => r.tris > 100000 && r.tris < 125000) // 基线 ~89.7k + 装饰 ~22k
const holesOk = rows.every((r) => r.holes === 0)
const cloudRate = rows.filter((r) => r.clouds >= 1).length / rows.length
const birdRate = rows.filter((r) => r.birds >= 1).length / rows.length
const planeSeen = rows.some((r) => r.plane === 1)
console.log(`holeScan 全 0：${holesOk}；三角数稳定：${trisOk}；云出场率 ${(cloudRate * 100).toFixed(0)}%；鸟出场率 ${(birdRate * 100).toFixed(0)}%；飞机出现过：${planeSeen}；耗时 ${((Date.now() - t0) / 1000).toFixed(0)}s`)
const ok = holesOk && trisOk && cloudRate >= 0.7 && birdRate >= 0.25 && planeSeen
console.log(ok ? 'PASS: 三种装饰均有稳定出场、无空洞、渲染量稳定' : 'FAIL: 见上方指标')
await browser.close()
process.exit(ok ? 0 : 1)
