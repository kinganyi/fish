#!/usr/bin/env node
/**
 * 构建产物的冒烟检查 —— `cd ui; npm run smoke`
 *
 * 为什么要有它：`package.json` 原来的 `smoke` 指向 `ui/scripts/smoke.mjs`，而那个文件
 * **不存在**（评审 R-02 原文）——一条指向空气的脚本等于没有验收。这里把它建成真的检查，
 * 而且每条都能红：
 *
 *   S1 构建存在且不旧于源码 —— dist 里任一文件比 `src/**` 最晚修改还旧 → 红（防"改完没构建"）
 *   S2 入口引用完整     —— dist/index.html 引的 js/css 必须真的在 dist 里 → 缺一个就红
 *   S3 空态四句在包里   —— codes.json 的 windowEmpty / filteredEmpty / emptyDb / unreachable
 *                          必须都能在 dist 的 js 里搜到 → 少一句就红（A-10 的构建期证据）
 *   S4 没有内部口径     —— dist 的 js/css/html 里不得出现 `**` / TODO / FIXME / 一致率 / 不采用
 *                          （评审原文：UI 里混进了开发者的字面 Markdown 与内部口径）
 *   S5 状态值都有构造点 —— `AvailState` / `LoadState` 的每个成员都要在 `src/**` 里出现
 *                          （F-7：不允许存在"声明了但永远不会被构造"的状态）
 *   S6 截图晚于构建     —— screenshots/*.png 必须比 dist 新（评审原文：三张截图早于 dist 5–14 分钟）
 *
 * 用法：`node scripts/smoke.mjs`（在 ui 目录）或 `node ui/scripts/smoke.mjs`（在仓根）。
 * 退出码：全绿 0，有红 1。只读，不构建、不改文件。
 */
import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const UI = resolve(HERE, '..')
const DIST = join(UI, 'dist')
const SRC = join(UI, 'src')

const results = []
function check(name, ok, detail) {
  results.push({ name, ok, detail })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}  ${detail}`)
}

function walk(dir) {
  const out = []
  if (!existsSync(dir)) return out
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) out.push(...walk(full))
    else out.push(full)
  }
  return out
}

const distFiles = walk(DIST)
if (!distFiles.length) {
  console.log('FAIL  S1 构建存在  dist/ 不存在或为空——先 `npm run build`')
  process.exit(1)
}

// ── S1 构建不旧于源码 ────────────────────────────────────────────────
const srcMtime = Math.max(...walk(SRC).map((f) => statSync(f).mtimeMs))
const distMtime = Math.max(...distFiles.map((f) => statSync(f).mtimeMs))
check('S1 构建不旧于源码', distMtime >= srcMtime,
  `src 最晚 ${new Date(srcMtime).toISOString()} · dist 最晚 ${new Date(distMtime).toISOString()}`)

// ── S2 入口引用完整 ──────────────────────────────────────────────────
const htmlPath = join(DIST, 'index.html')
const html = existsSync(htmlPath) ? readFileSync(htmlPath, 'utf8') : ''
const refs = [...html.matchAll(/(?:src|href)="([^"]+)"/g)]
  .map((m) => m[1])
  .filter((ref) => !ref.startsWith('http') && !ref.startsWith('data:'))
const missing = refs.filter((ref) => !existsSync(join(DIST, ref.replace(/^\//, ''))))
check('S2 入口引用的资源都在包里', refs.length > 0 && missing.length === 0,
  `引用 ${refs.length} 个 · 缺 ${JSON.stringify(missing)}`)

// ── S3 空态四句在包里 ────────────────────────────────────────────────
const codes = JSON.parse(readFileSync(join(UI, 'codes.json'), 'utf8'))
const bundleText = distFiles
  .filter((f) => /\.(js|css)$/.test(f))
  .map((f) => readFileSync(f, 'utf8'))
  .join('\n')
const emptyKeys = ['emptyDb', 'filteredEmpty', 'windowEmpty', 'unreachable']
const absent = emptyKeys.filter((key) => !bundleText.includes(codes[key]))
check('S3 空态四句都在构建产物里', absent.length === 0,
  `缺 ${JSON.stringify(absent)}`)

// ── S4 没有内部口径 / 字面 Markdown / 旧页签名 ──────────────────────────
// 先把 `/* … */` 注释块去掉再扫：第三方（React）的 `/** @license … */` 横幅会命中 `**`，
// 而那不是界面上给用户看的文字。真正的风险是 JSX/字符串里的字面 Markdown（如 `**两段分列**`），
// 它在打包后是字符串字面量、在注释块之外，下面这步一定扫得到。
//
// `工作台` 是**旧页签名**（用户 2026-11-23 改叫「现场」）：产物里出现一次就红；
// `现场` 是**新页签名**：产物里必须有，否则说明改名只改了源码注释、没改到界面上。
const stripComments = (text) => text.replace(/\/\*[\s\S]*?\*\//g, '')
const BANNED = ['**', 'TODO', 'FIXME', '一致率', '不采用', '工作台']
const REQUIRED = ['现场']
const scanFiles = distFiles.filter((f) => /\.(js|css|html)$/.test(f))
const hits = []
let scanned = ''
for (const file of scanFiles) {
  const text = stripComments(readFileSync(file, 'utf8'))
  scanned += text
  for (const token of BANNED) {
    if (text.includes(token)) hits.push(`${file.replace(UI, 'ui')}: ${token}`)
  }
}
const absentRequired = REQUIRED.filter((token) => !scanned.includes(token))
check('S4 产物里没有内部口径与旧页签名（且新页签名在包里）',
  hits.length === 0 && absentRequired.length === 0,
  JSON.stringify({ hits, absentRequired }))

// ── S5 状态值都有构造点 ──────────────────────────────────────────────
// `AvailState` 在 api/types.ts，`LoadState` 在 store/workbench.ts——两处都要读，
// 否则会漏掉一半成员（漏读比读失败更糟：它让检查"看起来有判别力"）。
const typeText = readFileSync(join(SRC, 'api', 'types.ts'), 'utf8')
const storePath = join(SRC, 'store', 'workbench.ts')
const storeText = readFileSync(storePath, 'utf8')
const srcText = walk(SRC).map((f) => readFileSync(f, 'utf8')).join('\n')
function members(source, typeName) {
  // 分隔符要含 `\ntype ` —— `LoadState` 后面紧跟着 `type FetchOutcome`（中间只空一行没有），
  // 少这一个分支就会把下一个类型的前两个成员也吃进来，读数就不精确了。
  const match = new RegExp(
    `export type ${typeName}\\s*=([\\s\\S]*?)(?:\\n\\n|\\ntype |\\nexport )`).exec(source)
  if (!match) return []
  return [...new Set([...match[1].matchAll(/'([a-z]+)'/g)].map((m) => m[1]))]
}
const declared = [...members(typeText, 'AvailState'), ...members(storeText, 'LoadState')]
const unconstructed = declared.filter((member) => {
  // 构造点：任一 src 文件里出现该状态值的字符串字面量（store 赋值 / 视图判断 / 类型声明都算）
  return !new RegExp(`['"]${member}['"]`).test(srcText + storeText)
})
check('S5 声明过的状态值都有构造点', declared.length >= 8 && unconstructed.length === 0,
  `声明 ${declared.length} 个（${declared.join('/')}） · 无构造点 ${JSON.stringify(unconstructed)}`)

// ── S6 截图晚于构建 ──────────────────────────────────────────────────
const shotsDir = join(UI, 'screenshots')
const shots = existsSync(shotsDir)
  ? readdirSync(shotsDir).filter((name) => name.endsWith('.png')).map((name) => join(shotsDir, name))
  : []
const stale = shots.filter((file) => statSync(file).mtimeMs < distMtime)
check('S6 截图晚于 dist 构建', shots.length > 0 && stale.length === 0,
  `截图 ${shots.length} 张 · 比 dist 旧 ${JSON.stringify(stale.map((f) => f.replace(UI, 'ui')))}`)

const passed = results.filter((r) => r.ok).length
console.log(`\n# 汇总 ${passed}/${results.length} PASS`)
process.exit(passed === results.length ? 0 : 1)
