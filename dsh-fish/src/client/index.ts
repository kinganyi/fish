/**
 * dsh-fish · 客户端半：把第一期界面搬进 DSH 的槽位
 *
 * ## 视觉一致方案的取舍
 *
 * 用户定的是「**做视觉一致的方案**」，所以这里不是把第一期界面重写成 DOM 组件，
 * 而是把它的 canvas 渲染核心原样搬进来，React 只做容器。
 * 代价与收益要说清楚：
 *   · 代价：这块面板和 DSH 的 DOM 体系是割裂的（它内部是画布，不是可被宿主样式化的元素），
 *     宿主的主题切换、字体缩放、无障碍树都影响不到画布内部。
 *   · 收益：**视觉与 fish 独立软件 100% 一致**——同一套海、同一套原子、同一套信息束。
 *     这正是"同构"要的东西：两个宿主里看到的是同一个世界，而不是"长得像的两个界面"。
 *
 * ## 为什么这一版只画海和原子
 *
 * 信息束、看板丝线、撞车电弧都依赖 live 读口的数据流（`/api/activity`）。
 * 在宿主里拿数据要经过 dsh-fish 宿主半转一道（浏览器侧不能直连 Python 读口），
 * 那条链路是下一步。这一版先把**渲染管线**接进槽位并验证它能跑、能停、能随容器尺寸变化——
 * 这是"同构可行"的最后一块技术前提。
 *
 * ## 与第一期的关系
 *
 * 算法与参数取自 `ui-第一期-20261027/js/06-sea.js` 与 `07-atom.js`，
 * 但这里是 TS + 自包含（不 import 第一期任何文件）——那一期是 classic script，
 * 在 ESM 闭包里加载不到。等链路稳定后，把两边收敛成一份共享视图模型才是正解；
 * 现在重复一次是为了先证明"能跑"，不是为了长期共存。
 */
import type { Context } from '@deepseek-ai/cordis'
import { createElement, useEffect, useRef } from 'react'

/** React 的三件在这里显式收拢：createElement 画结构，两个 hook 管生命周期。 */
type ReactModule = {
  createElement: typeof createElement
  useEffect: typeof useEffect
  useRef: typeof useRef
}
const React: ReactModule = { createElement, useEffect, useRef }

/** 与宿主半同名的插件 id。 */
export const name = 'dsh-fish'

/** 面板 id：三处槽位（内嵌/整页/侧栏）共用。 */
export const PANEL_ID = 'fish-scene'

export interface FishClientRuntime {
  readonly pluginId: string
  readonly viewModelVersion: string
}

declare module '@deepseek-ai/cordis' {
  interface Context {
    fishClient: FishClientRuntime
  }
}

/* ── 渲染核心（自包含，无 DOM 依赖之外的引用）──────────────────────── */

const TAU = Math.PI * 2
const clamp = (v: number, a: number, b: number) => (v < a ? a : v > b ? b : v)

/** 3D 透视海面的参数（与第一期同一组值） */
const SEA = {
  focal: 1180,
  camH: 118,
  rows: 26,
  horizonRatio: 0.62,
}

function waveH(x: number, z: number, t: number): number {
  const s1 = Math.sin(x * 0.0034 + z * 0.0021 + t * 0.55) * 17.0
  const s2 = Math.sin(x * 0.0086 - z * 0.0059 + t * 0.42) * 10.2
  const s3 = Math.sin(x * 0.0207 + z * 0.0138 + t * 0.98) * 4.6
  const s4 = Math.sin(x * 0.0475 - z * 0.0292 + t * 1.74) * 2.0
  const s5 = Math.sin(z * 0.0071 + t * 0.19) * 5.8
  return s1 + s2 + s3 + s4 + s5
}

type SceneAtom = { x01: number; y01: number; r: number; hue: number; phase: number }

/** 演示用的原子分布：真数据接进来之前，先用它证明管线 */
const ATOMS: SceneAtom[] = [
  { x01: 0.30, y01: 0.34, r: 46, hue: 174, phase: 0.0 },
  { x01: 0.52, y01: 0.26, r: 40, hue: 174, phase: 1.7 },
  { x01: 0.44, y01: 0.52, r: 38, hue: 174, phase: 3.1 },
  { x01: 0.68, y01: 0.40, r: 34, hue: 258, phase: 4.4 },
  { x01: 0.82, y01: 0.30, r: 30, hue: 34, phase: 5.6 },
]

function renderScene(
  canvas: HTMLCanvasElement,
  t: number,
  size: { w: number; h: number; dpr: number },
): void {
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  const { w, h, dpr } = size
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.clearRect(0, 0, w, h)

  // 底色：与第一期同一套深空渐变
  const bg = ctx.createRadialGradient(w * 0.55, h * 0.34, 0, w * 0.55, h * 0.5, Math.max(w, h) * 0.95)
  bg.addColorStop(0, '#0b1a29')
  bg.addColorStop(0.38, '#071019')
  bg.addColorStop(0.72, '#040910')
  bg.addColorStop(1, '#020409')
  ctx.fillStyle = bg
  ctx.fillRect(0, 0, w, h)

  const horizon = h * SEA.horizonRatio
  const tide = 0.8 + 0.2 * Math.sin((t * TAU) / 26)

  // 海面：先定屏幕 y 的分布，再反解 z（第一期踩过的那个坑）
  ctx.fillStyle = 'rgba(96,222,232,1)'
  const top = horizon + 3
  const bottom = h * 1.04
  for (let i = 0; i < SEA.rows; i++) {
    const u = i / (SEA.rows - 1)
    const yy = top + Math.pow(u, 1.22) * (bottom - top)
    const dz = Math.max(3, yy - horizon)
    const z = (SEA.camH * SEA.focal) / dz
    const depth = u
    const scale = SEA.focal / z
    const stepPx = 16 - 9 * Math.pow(depth, 0.55)
    const rowVis = (0.17 + Math.pow(depth, 0.72) * 1.02) * tide
    const pxSize = Math.max(1.05, scale * 1.45)
    const halfW = w * 0.5
    for (let sx = 0; sx <= w; sx += stepPx) {
      const xw = (sx - halfW) / scale
      const hh = waveH(xw, z, t)
      const hScale = 0.55 + 0.45 * (1 - depth)
      const sy = yy - hh * scale * hScale
      const crest = clamp((hh + 36) / 72, 0, 1)
      const a = rowVis * (0.16 + crest * 1.55) * (0.86 + 0.52 * clamp((crest - 0.62) / 0.38, 0, 1))
      if (a < 0.012) continue
      ctx.globalAlpha = a > 1 ? 1 : a
      ctx.fillRect(sx, sy, pxSize, pxSize)
    }
  }
  ctx.globalAlpha = 1

  // 海平线
  const hl = ctx.createLinearGradient(0, 0, w, 0)
  hl.addColorStop(0, 'rgba(140,238,244,0)')
  hl.addColorStop(0.5, 'rgba(190,250,252,' + (0.5 * tide).toFixed(3) + ')')
  hl.addColorStop(1, 'rgba(140,238,244,0)')
  ctx.fillStyle = hl
  ctx.fillRect(0, horizon - 0.6, w, 1.2)

  // 原子：球体 + 内核 + 0/1 壳 + 电子
  for (const a of ATOMS) {
    const bobX = Math.sin(t * 0.61 + a.phase) * 4.6
    const bobY = Math.cos(t * 0.47 + a.phase * 0.8) * 3.8
    const x = a.x01 * w + bobX
    const y = a.y01 * h + bobY
    const r = a.r
    const hue = a.hue
    const pulse = 0.5 + 0.5 * Math.sin((t * TAU) / 4.2 + a.phase)

    // 外辉光
    const halo = ctx.createRadialGradient(x, y, r * 0.2, x, y, r * 2.4)
    halo.addColorStop(0, `hsla(${hue},72%,62%,0.22)`)
    halo.addColorStop(0.5, `hsla(${hue},70%,55%,0.08)`)
    halo.addColorStop(1, `hsla(${hue},70%,50%,0)`)
    ctx.fillStyle = halo
    ctx.beginPath()
    ctx.arc(x, y, r * 2.4, 0, TAU)
    ctx.fill()

    // 球体
    const body = ctx.createRadialGradient(x - r * 0.34, y - r * 0.36, r * 0.04, x, y, r)
    body.addColorStop(0, `hsla(${hue},66%,68%,0.52)`)
    body.addColorStop(0.26, `hsla(${hue},62%,52%,0.44)`)
    body.addColorStop(0.58, `hsla(${hue},58%,38%,0.34)`)
    body.addColorStop(1, `hsla(${hue},52%,22%,0)`)
    ctx.fillStyle = body
    ctx.beginPath()
    ctx.arc(x, y, r, 0, TAU)
    ctx.fill()

    // 内核
    const core = ctx.createRadialGradient(x, y, 0, x, y, r * 0.24)
    core.addColorStop(0, `rgba(255,255,255,${(0.9 * (0.6 + pulse * 0.4)).toFixed(3)})`)
    core.addColorStop(0.4, `hsla(${hue},90%,84%,0.32)`)
    core.addColorStop(1, `hsla(${hue},88%,72%,0)`)
    ctx.fillStyle = core
    ctx.beginPath()
    ctx.arc(x, y, r * 0.24, 0, TAU)
    ctx.fill()

    // 0/1 壳
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'
    const fs = clamp(r * 0.22, 7, 11)
    ctx.font = `${fs.toFixed(1)}px ui-monospace, Consolas, monospace`
    for (let k = 0; k < 14; k++) {
      const ang = (k / 14) * TAU + t * 0.22 + a.phase
      const rr = r * (0.94 + 0.24 * Math.sin(k * 2.3 + t * 2.2))
      const px = x + Math.cos(ang) * rr
      const py = y + Math.sin(ang) * rr * 0.96
      const life = 0.35 + 0.65 * Math.abs(Math.sin(k * 1.7 + t * 1.5 + a.phase))
      ctx.globalAlpha = life * 0.7
      ctx.lineWidth = 1.6
      ctx.strokeStyle = `rgba(2,5,10,${(life * 0.5).toFixed(3)})`
      const ch = k % 2 ? '1' : '0'
      ctx.strokeText(ch, px, py)
      ctx.fillStyle = `hsl(${hue},92%,88%)`
      ctx.fillText(ch, px, py)
    }
    ctx.globalAlpha = 1
    ctx.textAlign = 'start'

    // 电子
    for (let o = 0; o < 2; o++) {
      const tilt = (o - 0.5) * 0.7
      const sq = 0.28 + o * 0.12
      const orbR = r * (1.5 + o * 0.16)
      ctx.globalAlpha = 0.12
      ctx.strokeStyle = `hsl(${hue},60%,68%)`
      ctx.lineWidth = 0.75
      ctx.beginPath()
      ctx.ellipse(x, y, orbR, orbR * sq, tilt, 0, TAU)
      ctx.stroke()
      const ang = t * (0.9 + o * 0.3) + a.phase + o * Math.PI
      const ex0 = Math.cos(ang) * orbR
      const ey0 = Math.sin(ang) * orbR * sq
      const px = x + ex0 * Math.cos(tilt) - ey0 * Math.sin(tilt)
      const py = y + ex0 * Math.sin(tilt) + ey0 * Math.cos(tilt)
      const front = 0.45 + 0.55 * clamp((Math.sin(ang) + 1) / 2, 0, 1)
      ctx.globalAlpha = 0.85 * front
      ctx.fillStyle = 'rgba(255,255,255,0.95)'
      ctx.beginPath()
      ctx.arc(px, py, r * 0.045 * front, 0, TAU)
      ctx.fill()
    }
    ctx.globalAlpha = 1
  }
}

/* ── React 容器 ─────────────────────────────────────────────────────
 * 直接 `import ... from 'react'`：tsdown 因为 `neverBundle` 里有 'react'
 * 会把它**外置**成浏览器侧的 `require('react')`，而那个 require 正是
 * `__ModuleLoader__` 工厂的参数——模块表里由 shell 预先塞好（platform seed）。
 * 所以这条 import 在构建期不解析、运行期有值，正是我们要的。
 *
 * 用 createElement 而不是 JSX：本包 tsconfig 没开 `jsx`，为一个组件改全局编译选项不划算。
 * 等面板长到十几个组件时再统一切 JSX。
 */
function makePanel(React: ReactModule) {
  const { createElement: h, useEffect, useRef } = React
  return function FishPanel() {
    const ref = useRef<HTMLCanvasElement | null>(null)
    useEffect(() => {
      const cv = ref.current
      if (!cv) return
      let raf = 0
      let alive = true
      const t0 = performance.now()
      const size = { w: 0, h: 0, dpr: 1 }

      const resize = () => {
        const dpr = Math.min(window.devicePixelRatio || 1, 2)
        const rect = cv.getBoundingClientRect()
        size.w = Math.max(1, Math.floor(rect.width))
        size.h = Math.max(1, Math.floor(rect.height))
        size.dpr = dpr
        cv.width = Math.floor(size.w * dpr)
        cv.height = Math.floor(size.h * dpr)
      }
      resize()

      const loop = (now: number) => {
        if (!alive) return
        renderScene(cv, (now - t0) / 1000, size)
        raf = requestAnimationFrame(loop)
      }
      raf = requestAnimationFrame(loop)

      let ro: ResizeObserver | null = null
      if (typeof ResizeObserver !== 'undefined') {
        ro = new ResizeObserver(() => resize())
        ro.observe(cv)
      } else {
        window.addEventListener('resize', resize)
      }
      // 必须清理：槽位随时可能被宿主卸载，留着 rAF 就是一条泄漏的渲染循环
      return () => {
        alive = false
        cancelAnimationFrame(raf)
        if (ro) ro.disconnect()
        else window.removeEventListener('resize', resize)
      }
    }, [])

    /* 布局契约（这是踩过的坑，写死在代码里）：
     *
     * 官方把本组件挂在 `ui-conversation` 的 `.viewArea` 里，那个容器的规则是
     *   `.viewArea { display: flex; flex: 1; flex-direction: column; min-height: 0 }`
     * 并且——输入框产生 overlay 时——它会变成
     *   `.scrollBody:has([data-conversation-composer-overlay]) > … > .viewArea
     *    { flex: 1 1 0; min-height: 0; overflow: hidden }`
     *
     * 所以两件事是硬约束：
     *   ① **不能写 minHeight**。写死 320px 之后，输入框一升高容器被压到 320 以下，
     *      我们的 canvas 就顶出去被 `overflow:hidden` 裁掉（用户实测："显示不全"）。
     *   ② **不能只靠 `height:100%`**。父级是 flex 容器、高度来自 flex 计算，
     *      百分比高度会退化成 auto，canvas 于是塌成 0 或被内容撑开。
     *
     * 正解：外层当 flex 子项（`flex:1 1 0; minHeight:0`）+ `position:relative`，
     * canvas 用 absolute 铺满它。这样尺寸完全跟随宿主，永不超过也不塌陷。
     */
    return h(
      'div',
      { style: { flex: '1 1 0', minHeight: 0, position: 'relative', overflow: 'hidden' } },
      h('canvas', {
        ref,
        style: {
          position: 'absolute',
          top: 0,
          left: 0,
          right: 0,
          bottom: 0,
          width: '100%',
          height: '100%',
          display: 'block',
          borderRadius: '3px',
          background: '#020409',
        },
      }),
    )
  }
}

/**
 * 客户端半的装配入口。
 *
 * `inject` 是**服务边**：`slots` 由官方 `dsh-client-ui-renderer` 提供。
 * 它不在 → 本插件不激活 → 面板不注册（而不是崩掉）。
 * 注意这与 package.json 的 `dsh.client.inject` 是**两个不同层**的东西：
 * 那边是模块表行边（包名，决定加载序），这边是 cordis 服务依赖。
 */
export const inject = ['slots']

export function apply(ctx: Context): void {
  ctx.provide('fishClient', { pluginId: name, viewModelVersion: 'v1' })

  /* slots 服务的类型来自官方包，这里用结构化断言而不是 import 类型：
   * 那个包的 `.d.ts` 在 0.2.0-rc.2 这一代还没有稳定导出 `SlotsService` 类型名，
   * 硬 import 会让 typecheck 挂在"模块有但不导出这个名字"上。
   * 等它稳定后换成真类型——届时这条断言应当消失，而不是留着当挡箭牌。 */
  const slots = (ctx as unknown as {
    slots: {
      inject: (name: string, factory: () => unknown) => void
      register: (options: Record<string, unknown>, component: unknown) => unknown
    }
  }).slots

  slots.inject('conversation.view', () => {
    const Panel = makePanel(React)
    return slots.register(
      {
        name: 'conversation.view',
        id: PANEL_ID,
        order: 96,
        label: () => 'fish',
      },
      Panel,
    )
  })
}
