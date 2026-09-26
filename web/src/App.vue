<script setup lang="ts">
/**
 * 根组件：3D 场景背景舞台（地球/像素中国，跨路由共享）+ 路由视图。
 * 场景层 z-index 0、固定全屏；三屏内容（开屏 UI / 注册卡 / 规划三栏）各自压在上面。
 *
 * 关键：.stage 只在有场景时（has-scene）才铺深空底色——无场景时（规划页常态）完全透明，
 * body 的奶油渐变从三栏缝隙/内边距透出，与旧版一致（曾经这里常驻近黑底，形成一圈黑缝）。
 * 过场时序常量与 useSceneStage.ts 手工对应（ZOOM_MS=1000 / CROSSFADE_MS=600）。
 */
import { ref, watch } from 'vue'
import { sceneStage, SCENE_URLS, markLoaded, setEarthFrame } from './composables/useSceneStage'

const stage = sceneStage()
const earthSrc = ref('')
const chinaSrc = ref('')
let chinaTimer = 0 // 须声明在 watch(immediate) 之前：immediate 回调同步执行，晚于此行会 TDZ 报错

watch(earthSrc, (v) => { if (!v) setEarthFrame(null) })

watch(
  () => [stage.active, stage.warping, stage.spinning] as const,
  ([active, warping]) => {
    // 地球层：挂载时赋 src；过场结束（warping 与 spinning 都复位）才卸载，保证旋转+淡出完整播放
    if (active === 'earth') {
      stage.earthLoaded = false
      earthSrc.value = SCENE_URLS.earth
    } else if (!warping && !stage.spinning) {
      earthSrc.value = ''
    }
    // 中国层：门控过场下点击即赋值（提前加载，等首帧就绪才淡入——不给黑底可乘之机）
    window.clearTimeout(chinaTimer)
    if (active === 'china') {
      if (!chinaSrc.value) chinaSrc.value = SCENE_URLS.china
    } else {
      chinaSrc.value = ''
      stage.chinaLoaded = false
    }
  },
  { immediate: true }
)

function onEarthLoad(e: Event) {
  setEarthFrame(e.target as HTMLIFrameElement) // 供 setBoost 加速探针使用（同源可访问）
  markLoaded('earth')
}

function onChinaLoad(e: Event) {
  ;(e.target as HTMLIFrameElement).dataset.loaded = '1' // 门控信号 + 验收断言用
  markLoaded('china')
}
</script>

<template>
  <div
    class="stage"
    :class="{ warping: stage.warping, spinning: stage.spinning, fading: stage.fading, reduced: stage.reducedMotion, 'china-on': stage.active === 'china' && !stage.warping && !stage.spinning, 'has-scene': stage.active !== null }"
  >
    <div v-show="stage.active === 'earth' || stage.warping || stage.spinning" class="layer layer-earth">
      <iframe v-if="earthSrc" :src="earthSrc" title="地球全景" @load="onEarthLoad($event)" />
    </div>
    <div v-show="stage.active === 'china'" class="layer layer-china">
      <iframe v-if="chinaSrc" :src="chinaSrc" title="像素中国" @load="onChinaLoad($event)" />
    </div>
  </div>
  <router-view />
</template>

<style scoped>
.stage {
  position: fixed; inset: 0; z-index: 0; overflow: hidden;
  background: transparent; /* 无场景时透明：规划页三栏缝隙透出 body 奶油渐变（旧版观感） */
}
.stage.has-scene { background: #060a14; } /* 场景加载前的深空底色，避免闪白 */
.layer { position: absolute; inset: 0; will-change: transform, filter, opacity; }
.layer iframe { width: 100%; height: 100%; border: 0; display: block; }

/* 地球层：开屏期间接收鼠标视差；过场时放大+模糊（ZOOM_MS=1000，不动 opacity），
   fading 时才淡出（transition 列表保留 transform/filter，缩放不中断）。
   注意：ZOOM_MS 的 1.0s 在下面 base 与 fading 两条规则共 4 处属性（transform/filter ×2）
   必须同步——漏改会在 fading 起始点发生过渡速率跳档。 */
.layer-earth {
  pointer-events: auto;
  animation: layerIn 0.9s ease both;
  transition:
    transform 1.0s cubic-bezier(0.7, 0, 0.84, 0),
    filter 1.0s cubic-bezier(0.7, 0, 0.84, 0);
}
.stage.warping .layer-earth {
  transform: scale(1.5);
  filter: blur(18px) brightness(1.12);
}
.stage.fading .layer-earth {
  opacity: 0;
  transition:
    transform 1.0s cubic-bezier(0.7, 0, 0.84, 0),
    filter 1.0s cubic-bezier(0.7, 0, 0.84, 0),
    opacity 0.6s ease;
}

/* 中国层：默认放大+透明。注意 china-on 必须排除 spinning——多段过场中 spinning 期间
   warping 尚未置位，若 chin-on 早生效会把中国层以 opacity=1 压在旋转的地球之上，
   用户完全看不到"转向中国"（2026-09-25 实测事故）。fading 时才对冲淡入
   （scale 1.05→1，不带模糊降成本）；淡入时长与 CROSSFADE_MS=600 对齐；
   .china-on 保持终值（跨路由零跳变，也承担直接进 #/register 的淡入） */
.layer-china {
  pointer-events: none;
  transform: scale(1.05);
  opacity: 0;
  transition: opacity 0.6s ease, transform 0.6s ease;
}
.stage.fading .layer-china { opacity: 1; transform: none; }
.stage.china-on .layer-china {
  opacity: 1; transform: none;
  transition: opacity 0.6s ease, transform 0.6s ease;
}

@keyframes layerIn { from { opacity: 0; } to { opacity: 1; } }

/* 减弱动态：只保留 0.2s 淡入淡出，禁用位移/模糊/加速 */
.stage.reduced .layer-earth,
.stage.reduced .layer-china {
  transition: opacity 0.2s ease !important;
  transform: none !important;
  filter: none !important;
  animation: none !important;
}
</style>
