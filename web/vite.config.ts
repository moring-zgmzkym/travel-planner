import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// base 必须为 /static/：网关只挂载 /static 一个目录（GET / 读 static/index.html），
// 产物引用的 assets 也必须在 /static/ 下才可达。
export default defineConfig({
  plugins: [vue()],
  base: '/static/',
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) }
  },
  server: {
    port: 5173,
    // 开发模式直连后端（生产由 FastAPI 同源托管）；注意 base='/static/'，
    // 开发时访问 http://127.0.0.1:5173/static/#/
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true }
    }
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 1200
  }
})
