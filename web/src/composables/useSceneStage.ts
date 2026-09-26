/**
 * useSceneStage —— 开屏地球 / 像素中国两个 3D 场景背景层的全局舞台（模块级单例）。
 *
 * 场景层挂在 App.vue（路由视图之下、所有屏幕共享），因此“地球自转到中国→过场→像素中国
 * 注册页”可以跨路由无缝交叉淡入，不需要在切换瞬间二次加载 iframe。
 *
 * 过场铁律（2026-09-25 踩坑总结）：① 任何一层不在其替代内容首帧就绪前撤离；② 地球层
 * 存活三相位（warping/spinning/active='earth'）与 china-on 排除 spinning，两处规则漏
 * spin相位各出过一次大事故（点击即卸载 / 中国层全程盖住地球）；③ 精确时序见
 * warpToChina 的六段注释（补间 1.5s + 停展 0.5s + 放大模糊 1.0s + 交叉淡入 0.6s）。
 *
 * 生命周期：同一时刻至多一个场景层在跑；离开即卸载 iframe（src 置空），
 * 避免两个 WebGL 上下文 + 8.6MB 常驻内存。
 */
import { reactive } from 'vue'

export type SceneName = 'earth' | 'china'

export const SCENE_URLS: Record<SceneName, string> = {
  earth: '/static/scenes/earth.html',
  china: '/static/scenes/pixel-china.html'
}

/** 过场时长（与 App.vue scoped CSS 的 transition 时长手工对应——改这里必须改 CSS） */
export const ZOOM_MS = 1000 // 地球放大+模糊总时长（CSS 过渡 1.0s）
export const CROSSFADE_MS = 600 // 交叉淡入（地球 opacity 淡出 / 中国 opacity 淡入，CSS 0.6s×3 处）
export const CHINA_READY_TIMEOUT_MS = 1500 // 等中国层首帧的 fail-open 上限（旋转期已预载 2s）
export const FADE_IN_OFFSET_MS = 550 // 淡入起点偏移（=ZOOM×0.55）：模糊爬升过半再淡入，衔接设计点
/** AuthView 放行后规划页淡入时长（与 PlanView.vue 的 .plan-enter 0.45s 手工对应） */
export const PLAN_ENTER_MS = 450

/* ---------- 旋转到中国（精确时序：1.5s 补间 + 0.5s 停展 = 2s） ---------- */
export const CHINA_LON = 105 // 中国中部经度（正对相机）
const SPIN_TO_CHINA_MS = 1500 // 补间时长（场景内 easeInOutCubic，起停柔和）
const PAUSE_AT_CHINA_MS = 500 // 转到后停住展示时长

interface SceneStageState {
  active: SceneName | null
  warping: boolean // 地球放大模糊中（同时也是地球层的存活条件之一：v-show 绑定）
  spinning: boolean // 加速自转到中国中（同样是地球层的存活条件：v-show 绑定）
  fading: boolean // 交叉淡入中（地球 opacity→0 / 中国 opacity→1）
  earthLoaded: boolean // 地球 iframe 首帧就绪（开屏按钮才弹入）
  chinaLoaded: boolean // 中国 iframe 首帧就绪（过场门控信号）
  reducedMotion: boolean
}

function detectReducedMotion(): boolean {
  return typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches
}

const state = reactive<SceneStageState>({
  active: null,
  warping: false,
  spinning: false,
  fading: false,
  earthLoaded: false,
  chinaLoaded: false,
  reducedMotion: detectReducedMotion()
})

let earthFrameEl: HTMLIFrameElement | null = null

export function sceneStage(): SceneStageState {
  return state
}

export function setEarthFrame(el: HTMLIFrameElement | null) {
  earthFrameEl = el
}

export function markLoaded(name: SceneName) {
  if (name === 'earth') state.earthLoaded = true
  else state.chinaLoaded = true
}

function sleep(ms: number) {
  return new Promise<void>((r) => setTimeout(r, ms))
}

/** 挂载场景层（幂等；已挂载则仅确保可见）。 */
export function mountScene(name: SceneName) {
  state.warping = false
  state.spinning = false
  state.fading = false
  state.active = name
}

/**
 * 开屏点击 → 注册页的门控过场（精确时序，总 ~3.15s）：
 *  ① `active='china'` 中国层立即开载 + `spinning=true`（两件必须同同步段，见下方注释）；
 *  ② `spinToLongitude(105, 1.5)`：场景内 easeInOutCubic 补间，逐渐加速自转到中国正面；
 *  ③ 停展 0.5s：地球完全静止，中国正面全细节展现；
 *  ④ `warping`：放大 + 渐变模糊（ZOOM_MS=1000，CSS 逐帧插值即"逐渐模糊"）；
 *  ⑤ `FADE_IN_OFFSET_MS`（模糊爬升 55%）→ `waitChinaReady()`（1.5s 兜底）→ `fading`
 *     交叉淡入（CROSSFADE_MS=600）：地球在模糊中化开成像素中国，模糊峰值与中国层
 *     全显错开约 150ms，避免同步齐切；
 *  ⑥ 复位 warping/fading：`.china-on` 稳态接棒（终值一致，零跳变），地球层随即卸载。
 * resolve = 过场结束（可安全切路由）。
 */
export function warpToChina(): Promise<void> {
  state.active = 'china'
  if (state.reducedMotion) {
    return (async () => {
      await waitChinaReady()
      state.fading = true
      await sleep(200)
      state.warping = false
      state.spinning = false
      state.fading = false
    })()
  }
  return (async () => {
    // spinning 必须与 active='china' 同段置位：地球层存活规则是
    // "warping || spinning || active==='earth'"，漏 spin相位地球层会在点击瞬间被卸载、
    // 补间打到死窗口（2026-09-25 实测事故）；china-on 也排除 spinning，否则中国层会以
    // opacity=1 盖住旋转中的地球（同日第二起事故）。
    state.spinning = true
    if (!spinToChina()) boostEarth(7) // 旧场景（无 spinToLongitude 探针）降级：单纯加速
    await sleep(SPIN_TO_CHINA_MS + PAUSE_AT_CHINA_MS) // 补间确定性推进，无需轮询
    state.spinning = false
    state.warping = true
    await sleep(FADE_IN_OFFSET_MS)
    await waitChinaReady()
    state.fading = true
    await sleep(CROSSFADE_MS)
    state.warping = false
    state.fading = false
  })()
}

/** 等中国层首帧就绪（iframe load → chinaLoaded），fail-open 见 CHINA_READY_TIMEOUT_MS。 */
function waitChinaReady(): Promise<void> {
  const deadline = Date.now() + CHINA_READY_TIMEOUT_MS
  return (async () => {
    while (!state.chinaLoaded && Date.now() < deadline) await sleep(50)
  })()
}

interface EarthProbe {
  info: { spin: number; spinBoost: number; spinning: boolean }
  setBoost(v: number): void
  faceLongitude(lon: number): { delta: number; targetSpin: number }
  spinToLongitude(lon: number, durationS: number): { delta: number; targetSpin: number }
}

function earthProbe(): EarthProbe | null {
  try {
    const w = earthFrameEl?.contentWindow as (Window & { __earth?: EarthProbe }) | null
    return w && w.__earth && typeof w.__earth.spinToLongitude === 'function' ? w.__earth : null
  } catch {
    return null
  }
}

/** ② 启动"SPIN_TO_CHINA_MS 补间自转到中国正面"。false = 无探针（旧场景，调用方降级）。 */
function spinToChina(): boolean {
  const earth = earthProbe()
  if (!earth) return false
  try {
    earth.spinToLongitude(CHINA_LON, SPIN_TO_CHINA_MS / 1000)
    return true
  } catch {
    return false
  }
}

/** 卸载全部场景层（无淡出——调用方需保证替代内容已不透明覆盖视口）。 */
export function unmountScenes() {
  state.active = null
  state.warping = false
  state.spinning = false
  state.fading = false
}

/** 地球自转加速（scene 内 __earth.setBoost 探针）。容错：缺失时 CSS 过场仍然成立。 */
export function boostEarth(scale = 7) {
  if (state.reducedMotion) return
  try {
    const w = earthFrameEl?.contentWindow as (Window & { __earth?: { setBoost?: (v: number) => void } }) | null
    if (w && typeof w.__earth?.setBoost === 'function') w.__earth.setBoost(scale)
  } catch {
    /* 跨域或未就绪：仅跳过加速，不影响过场 */
  }
}
