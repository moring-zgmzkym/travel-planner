<script setup lang="ts">
/**
 * SplashView —— 开屏：地球全景（iframe 背景）+ 「let's make some plans」按钮。
 * 点击 → warpToChina()：加速自转到中国正面 → 停展 → 模糊放大 → 交叉淡入到注册页
 * （全程编排在 useSceneStage，跨路由无接缝）。
 */
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { sceneStage, mountScene, warpToChina } from '../composables/useSceneStage'

const router = useRouter()
const stage = sceneStage()
const btnVisible = ref(false)
const leaving = ref(false)
let fallbackTimer = 0

onMounted(() => {
  mountScene('earth')
  // 兜底：iframe load 事件万一不来，3s 后也放行按钮
  fallbackTimer = window.setTimeout(() => { if (!leaving.value) btnVisible.value = true }, 3000)
})
onUnmounted(() => window.clearTimeout(fallbackTimer))

// 地球首帧就绪 → 按钮弹入（leaving 后不再放行：过场中按钮保持隐藏）
watch(() => stage.earthLoaded, (v) => { if (v && !leaving.value) btnVisible.value = true })

async function start() {
  if (leaving.value) return
  leaving.value = true
  window.clearTimeout(fallbackTimer) // 必须清掉：否则 3s 兜底计时器会在过场中把按钮又放回来
  btnVisible.value = false
  await warpToChina() // 内部依次完成：加速自转到中国 → 停展 → 模糊放大 → 交叉淡入
  router.push({ name: 'register' })
}
</script>

<template>
  <div class="splash-ui">
    <div class="splash-brand">
      <span class="splash-logo">✈️</span>
      <span class="splash-brand-text">TripMate<small>多 Agent 协同旅游规划</small></span>
    </div>
    <button
      id="btn-start"
      class="start-btn"
      :class="{ on: btnVisible }"
      :disabled="!btnVisible"
      @click="start"
    >
      let's make some plans
    </button>
  </div>
</template>

<style scoped>
.splash-ui {
  position: fixed; inset: 0; z-index: 10;
  pointer-events: none; /* 只让按钮可点，其余交互透给地球视差 */
}
.splash-brand {
  position: absolute; top: 28px; left: 32px;
  display: flex; align-items: center; gap: 10px;
  color: #fff; text-shadow: 0 2px 12px rgba(0, 0, 0, .45);
  animation: fadeDown 0.8s cubic-bezier(0.16, 1, 0.3, 1) 0.3s both;
}
.splash-logo { font-size: 26px; }
.splash-brand-text { font-family: var(--serif); font-size: 19px; font-weight: 700; letter-spacing: 0.5px; display: flex; flex-direction: column; }
.splash-brand-text small { font-size: 10.5px; font-weight: 400; opacity: 0.75; letter-spacing: 1px; }

/* 按钮：屏幕中心偏下（top≈68%） */
.start-btn {
  position: absolute; left: 50%; top: 68%;
  transform: translate(-50%, -50%) scale(0.92);
  pointer-events: auto;
  padding: 14px 38px;
  font-size: 15.5px; font-weight: 600; letter-spacing: 0.06em;
  color: #fff;
  background: rgba(10, 15, 30, 0.42);
  border: 1px solid rgba(255, 255, 255, 0.28);
  border-radius: 999px;
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  cursor: pointer;
  opacity: 0;
  transition:
    opacity 0.6s ease,
    transform 0.6s cubic-bezier(0.16, 1, 0.3, 1),
    box-shadow 0.3s ease,
    background 0.3s ease;
}
.start-btn.on {
  opacity: 1;
  transform: translate(-50%, -50%) scale(1);
}
.start-btn.on:hover {
  background: rgba(249, 115, 22, 0.32);
  box-shadow: 0 0 28px rgba(249, 115, 22, 0.45);
  transform: translate(-50%, -50%) scale(1.04);
}
.start-btn.on:active { transform: translate(-50%, -50%) scale(0.97); }
.start-btn:disabled { cursor: default; }

@keyframes fadeDown { from { opacity: 0; transform: translateY(-10px); } to { opacity: 1; transform: none; } }

@media (max-width: 700px) {
  .start-btn { top: 72%; padding: 12px 28px; font-size: 14px; }
  .splash-brand { top: 20px; left: 20px; }
}
</style>
