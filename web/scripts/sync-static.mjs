/**
 * 把 web/dist 的构建产物同步进 TripMate/static/（FastAPI 直接托管该目录，GET / 读 static/index.html）。
 *
 * 守卫（安全设计，任何一条不满足都拒绝执行并退出码 1）：
 *  1) static/ 仍是旧前端三件套（app.js/style.css 还在）但没有 static-legacy/ 备份 → 拒绝；
 *  2) dist/ 缺少 index.html / manifest.webmanifest / scenes/*.html → 拒绝（public/ 未就位？）。
 * 执行：先列出 static/ 内将被清空的条目，清空后整目录拷贝 dist → static。
 */
import { readdirSync, existsSync, statSync, copyFileSync, mkdirSync, rmSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const webRoot = dirname(dirname(fileURLToPath(import.meta.url))) // TripMate/web
const tripRoot = dirname(webRoot) // TripMate
const distDir = join(webRoot, 'dist')
const staticDir = join(tripRoot, 'static')
const legacyDir = join(tripRoot, 'static-legacy')

function fail(msg) {
  console.error(`sync-static: ${msg}`)
  process.exit(1)
}

if (!existsSync(distDir)) fail('dist/ 不存在，请先运行 vite build')
for (const f of ['index.html', 'manifest.webmanifest', 'scenes/earth.html', 'scenes/pixel-china.html']) {
  if (!existsSync(join(distDir, f))) fail(`dist/ 缺少 ${f}（web/public/ 未就位？）`)
}

const hasLegacyFiles = ['app.js', 'style.css'].some((f) => existsSync(join(staticDir, f)))
if (hasLegacyFiles && !existsSync(legacyDir)) {
  fail('static/ 仍是旧前端且没有 static-legacy/ 备份，已停止。请先备份再同步。')
}

const victims = existsSync(staticDir) ? readdirSync(staticDir) : []
console.log('sync-static: 将清空 static/ 并拷贝 dist/，受影响的条目：')
for (const v of victims) console.log(`  - ${v}`)
for (const v of victims) rmSync(join(staticDir, v), { recursive: true, force: true })
mkdirSync(staticDir, { recursive: true })

function copyDir(from, to) {
  mkdirSync(to, { recursive: true })
  for (const name of readdirSync(from)) {
    const src = join(from, name)
    const dst = join(to, name)
    if (statSync(src).isDirectory()) copyDir(src, dst)
    else copyFileSync(src, dst)
  }
}
copyDir(distDir, staticDir)
console.log(`sync-static: 完成，static/ 已更新（替换 ${victims.length} 项）`)
