import { createRouter, createWebHashHistory, type RouteRecordRaw } from 'vue-router'

// hash 模式：网关只为 "/" 提供 SPA 入口，hash 路由的深链接（#/register、#/app）不会 404。
// PlanView 动态 import：开屏/注册阶段不加载规划页代码，配合注册页 2s 停留期预热。
export function preloadPlanView() {
  return import('../views/PlanView.vue')
}

const routes: RouteRecordRaw[] = [
  { path: '/', name: 'splash', component: () => import('../views/SplashView.vue') },
  { path: '/register', name: 'register', component: () => import('../views/AuthView.vue') },
  {
    path: '/app',
    name: 'app',
    component: preloadPlanView,
    meta: { requiresAuth: true }
  },
  { path: '/:pathMatch(.*)*', redirect: '/' }
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
  scrollBehavior: () => false
})

// 令牌守卫：无令牌访问 /app 弹回开屏（与旧版 boot() 无令牌弹登录等价）
router.beforeEach((to) => {
  if (to.meta.requiresAuth && !localStorage.getItem('tm_token')) {
    return { name: 'splash' }
  }
})

export default router
