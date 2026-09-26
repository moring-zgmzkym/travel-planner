/** 认证封装（对应旧 app.js 的 authFetch / token 管理）。
 *  401 → 清理令牌并通知上层跳注册页（main.ts 注册处理器，避免本模块反向依赖 router）。 */
import { AUTH_EXPIRED_QUERY } from '../types/ws'

let token = localStorage.getItem('tm_token') || ''

/** 401/4401 时由 main.ts 注入：跳转注册页并提示 */
let onAuthLost: ((reason: string) => void) | null = null
export function setAuthLostHandler(fn: (reason: string) => void) {
  onAuthLost = fn
}

export function getToken(): string {
  return token
}

export function setToken(t: string) {
  token = t
  localStorage.setItem('tm_token', t)
}

export function clearToken() {
  token = ''
  localStorage.removeItem('tm_token')
}

export async function authFetch(url: string, opts: RequestInit = {}): Promise<Response> {
  opts.headers = Object.assign({}, opts.headers || {},
    token ? { Authorization: `Bearer ${token}` } : {})
  const r = await fetch(url, opts)
  if (r.status === 401) {
    clearToken()
    onAuthLost ? onAuthLost(AUTH_EXPIRED_QUERY) : (location.hash = '#/register')
    throw new Error('401')
  }
  return r
}
