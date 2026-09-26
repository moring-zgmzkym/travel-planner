<script setup lang="ts">
/**
 * AuthView —— 注册页：像素中国背景（舞台层）+ 玻璃拟态注册/登录卡。
 * 2 秒停留门：无论有无令牌，都在本页停留 ≥2s，期间预热 PlanView 代码块与
 * 会话/画像/记忆数据（ws-ticket 不预热，30s TTL，连接时才换）。
 * 放行条件 = 2s 到 && 预热完成；5s fail-open 硬上限，预热失败也放行。
 */
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { PLAN_ENTER_MS, sceneStage, mountScene, unmountScenes } from '../composables/useSceneStage'
import { preloadPlanView } from '../router'

const router = useRouter()
const route = useRoute()
const stage = sceneStage()

const DWELL_MS = 2000
const FAIL_OPEN_MS = 5000
const TOKEN_KEY = 'tm_token'
const enteredAt = performance.now()

type ViewState = 'visitor' | 'welcome' | 'gate'
const viewState = ref<ViewState>('visitor')
const mode = ref<'login' | 'register'>('register')
const username = ref('')
const password = ref('')
const error = ref('')
const submitting = ref(false)
const welcomeName = ref('')
const gating = ref(false) // 控制进度线动画播放

onMounted(async () => {
  mountScene('china') // 直接进入 #/register（书签/刷新）时也要挂载中国层
  if (route.query.expired) error.value = '登录已过期，请重新登录'
  const name = await checkToken()
  if (name) {
    welcomeName.value = name
    viewState.value = 'welcome'
    startGate()
  }
})

function setMode(m: 'login' | 'register') {
  mode.value = m
  error.value = ''
}

/** 令牌有效性预检：普通 fetch，不走带 401 副作用的封装；无效返回 null。 */
async function checkToken(): Promise<string | null> {
  const t = localStorage.getItem(TOKEN_KEY)
  if (!t) return null
  try {
    const r = await fetch('/api/me', { headers: { Authorization: `Bearer ${t}` } })
    if (!r.ok) return null
    return ((await r.json()).username as string) || null
  } catch {
    return null
  }
}

function preload() {
  return preloadPlanView()
}

/** 预热规划页数据：失败静默（各面板进页后自行重拉）。 */
function warmData() {
  const t = localStorage.getItem(TOKEN_KEY)
  if (!t) return Promise.resolve()
  const h = { headers: { Authorization: `Bearer ${t}` } }
  return Promise.allSettled([
    fetch('/api/sessions', h),
    fetch('/api/profile', h),
    fetch('/api/memory', h)
  ]).then(() => undefined)
}

function sleep(ms: number) {
  return new Promise<void>((r) => setTimeout(r, ms))
}

async function runGate() {
  const rest = Math.max(0, DWELL_MS - (performance.now() - enteredAt))
  await Promise.race([
    Promise.all([sleep(rest), preload(), warmData()]),
    sleep(FAIL_OPEN_MS) // fail-open：预热再慢也不把用户关在注册页
  ])
  const still = DWELL_MS - (performance.now() - enteredAt)
  if (still > 0) await sleep(still)
}

/**
 * 放行：先挂载规划页（不透明三栏立即覆盖视口，plan-enter 淡入叠在像素中国上，
 * 面板缝隙透出中国背景形成溶解），PLAN_ENTER_MS 后卸载场景。全程无黑屏。
 * 路由守卫：万一 push 失败（如令牌恰被 401 清掉），不得把随后挂载的开屏地球层误卸。
 */
async function startGate() {
  gating.value = true
  await runGate()
  await router.push({ name: 'app' })
  await sleep(PLAN_ENTER_MS) // 与 PlanView.vue 的 .plan-enter 0.45s 手工对应
  if (router.currentRoute.value.name === 'app') unmountScenes()
}

async function submit() {
  const u = username.value.trim()
  const p = password.value
  if (!u || !p) { error.value = '请输入用户名和密码'; return }
  submitting.value = true
  try {
    const r = await fetch(`/api/${mode.value}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: u, password: p })
    })
    const data = await r.json().catch(() => ({}))
    if (r.status !== 200) { error.value = data.detail || '操作失败，请重试'; return }
    localStorage.setItem(TOKEN_KEY, data.token)
    welcomeName.value = data.username
    viewState.value = 'gate'
    startGate()
  } catch {
    error.value = '网络异常，请重试'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="auth-screen">
    <div class="auth-wrap">
      <div class="auth-card">
        <div class="auth-logo">✈️</div>
        <div class="auth-title">TripMate</div>

        <template v-if="viewState === 'visitor'">
          <div class="auth-sub">登录后开始规划你的旅程</div>
          <div class="auth-tabs">
            <button id="tab-login" :class="{ on: mode === 'login' }" @click="setMode('login')">登录</button>
            <button id="tab-register" :class="{ on: mode === 'register' }" @click="setMode('register')">注册</button>
          </div>
          <input
            id="auth-username"
            v-model="username"
            placeholder="用户名（2-24 位中英文/数字/下划线）"
            autocomplete="username"
            @keydown.enter="submit"
          />
          <input
            id="auth-password"
            v-model="password"
            type="password"
            placeholder="密码（至少 6 位）"
            autocomplete="current-password"
            @keydown.enter="submit"
          />
          <button id="auth-submit" :disabled="submitting" @click="submit">
            {{ mode === 'login' ? '登录' : '注册' }}
          </button>
          <div class="auth-error" id="auth-error">{{ error }}</div>
        </template>

        <template v-else>
          <div class="auth-sub">欢迎回来，{{ welcomeName }}</div>
          <div class="auth-gate-hint">正在准备行程引擎…</div>
          <div class="auth-progress" :class="{ run: gating }"><i></i></div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.auth-screen {
  position: fixed; inset: 0; z-index: 10;
  display: flex; align-items: center; justify-content: center;
}
.auth-wrap {
  width: min(360px, 90vw);
  /* 卡片子元素错峰上移淡入：logo→标题→表单 */
  animation: cardIn 0.5s cubic-bezier(0.16, 1, 0.3, 1) both;
}
.auth-card { animation: none; }
.auth-card > * { animation: itemIn 0.45s cubic-bezier(0.16, 1, 0.3, 1) both; }
.auth-card > *:nth-child(1) { animation-delay: 0.05s; }
.auth-card > *:nth-child(2) { animation-delay: 0.11s; }
.auth-card > *:nth-child(3) { animation-delay: 0.17s; }
.auth-card > *:nth-child(4) { animation-delay: 0.23s; }
.auth-card > *:nth-child(5) { animation-delay: 0.29s; }
.auth-card > *:nth-child(6) { animation-delay: 0.35s; }
.auth-card > *:nth-child(7) { animation-delay: 0.41s; }

/* 玻璃拟态卡片：适配彩色像素中国背景（覆盖 main.css 的不透明白） */
.auth-card {
  background: rgba(255, 255, 255, 0.86);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
}

.auth-gate-hint { font-size: 12.5px; color: var(--sub); margin: 2px 0 8px; }
.auth-progress {
  height: 4px; border-radius: 2px; background: var(--brand-soft); overflow: hidden;
}
.auth-progress i {
  display: block; height: 100%; width: 0%;
  background: linear-gradient(90deg, var(--brand), #FBBF24);
}
.auth-progress.run i { animation: gateFill 2s linear forwards; }
@keyframes gateFill { from { width: 0%; } to { width: 100%; } }

@keyframes cardIn { from { opacity: 0; } to { opacity: 1; } }
@keyframes itemIn { from { opacity: 0; transform: translateY(12px); } to { opacity: 1; transform: none; } }
</style>
