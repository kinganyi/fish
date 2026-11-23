import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

/** 开发代理目标：默认 8790；`FISH_WB_API` 可覆盖（不引 @types/node，用 globalThis 取）。 */
const apiTarget =
  (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env
    ?.FISH_WB_API || 'http://127.0.0.1:8790'

/**
 * 开发用 Vite proxy：`/api` → 只读读口（默认 127.0.0.1:8790）。
 *
 * 交付拓扑有**两种**（见 `ui/README.md`）：
 *   · 开发便利：`p0/fish_workbench_api.py --dist ui/dist` 同源发静态（读口挂了页面也就打不开）；
 *   · 验收/宿主托管：UI 由独立静态服务（如 `python -m http.server`）托管，API 独立进程，
 *     地址栏用 `?api=http://127.0.0.1:8790` 指基址——读口挂了页面仍能显示「读口没有起来」。
 * 所以前端**不写死同源**：基址解析在 `src/api/client.ts`（query → 构建期变量 → 同源）。
 */
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    target: 'es2022',
  },
})
