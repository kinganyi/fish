/**
 * dsh-fish 构建配置：影子期只有 node 半边。
 *
 * ⚠️ 现在开始有第二个 block 了（client 半边的**管线验证**）。
 * 原文件头写的是"等 UI 与 fish 本体同构之后再补第二个产物"——那个时刻到了：
 * 第一期界面已经跑出来，现在要验证"同构能不能真做到"，第一步就是让这个包
 * 能产出官方 `__ModuleLoader__` 认的 CJS 工厂。
 *
 * ── 两条外置规则（照抄 dsh-hive 的实践，它那边已经跑通）─────────────
 *   1. 宿主半**绝不内联 `@deepseek-ai/*`**：内联 = 产物里带第二份实现，
 *      `ctx` 服务、单例状态、schema 身份都不再与宿主共享。
 *   2. 客户端半只外置「模块表答得出的 specifier」，其余一律内联——
 *      浏览器 require 表答不出的 specifier 一定在运行期抛错。
 *
 * ── 为什么这里不 import `defineConfig` ─────────────────────────────
 * 本文件**不许出现运行期 import**：`import { defineConfig } from 'tsdown'` 在
 * "插件不在 DSH 检出内"时必挂（这个目录没有能向上查到 tsdown 的祖先就会
 * `ERR_MODULE_NOT_FOUND`）。`defineConfig` 只是类型糖，用 `satisfies` 代替即可，
 * 类型校验一样不少，运行期零依赖。`import type` 在加载时被擦除，不参与解析。
 */
import type { UserConfig } from 'tsdown'

/** 包名：同时是模块表行 id 与 `__ModuleLoader__.load` 的 id。 */
const ID = 'dsh-fish'

/**
 * 浏览器静态共享模块（seed）：shell 引导时直接塞进冻结模块表。
 * 本批客户端半**一个都没用到**——但名单先写全，等真注册槽位时不用回头改配置。
 * 来源是 `packages/client/web/src/platform.ts` 的 `PLATFORM_MODULES`。
 */
const PLATFORM_MODULES = [
  'react',
  'react/jsx-runtime',
  'react-dom',
  'react-dom/client',
  '@deepseek-ai/cordis',
  '@deepseek-ai/dsh-client-store',
  '@deepseek-ai/dsh-client-ui-slots',
  '@deepseek-ai/dsh-client-ui-primitives',
  '@deepseek-ai/dsh-client-ui-dockkit',
]

/** 客户端插件行：为空，因为本批不注册任何槽位（与影子期纪律一致）。 */
const CLIENT_ROWS: string[] = []

const CLIENT_MODULE_TABLE = [...PLATFORM_MODULES, ...CLIENT_ROWS]

/** 宿主半：Loader 直接 import 的 Node 模块，宿主包全部留给宿主。 */
const host: UserConfig = {
  name: ID,
  entry: { index: 'src/index.ts' },
  outDir: 'lib',
  format: ['esm'],
  platform: 'node',
  target: 'es2023',
  clean: true,
  dts: false,
  sourcemap: true,
  // 产物名固定为 index.js：package.json 的 main/exports 与文档里的 `lib/index.js` 都按它写，
  // 让 tsdown 的默认扩展名（.mjs）决定文件名会把"包能装、但 import 不到"变成默认行为。
  outputOptions: { entryFileNames: 'index.js' },
  // 宿主提供的包一律外置：打进产物会得到"第二份 cordis"，服务注册表对不上。
  deps: { neverBundle: [/^@deepseek-ai\//] },
}

/**
 * 客户端半：浏览器闭包工厂产物，入口名固定为 client。
 *
 * 关键在 `outputOptions` 三件套——它们必须与官方 client bundle **逐字一致**：
 *   banner  把 factory 包成 `__ModuleLoader__.load({ id, factory: (require) => {`
 *   intro   在工厂体内造出 CommonJS 的 module/exports 两个绑定
 *   footer  `return module.exports; } });`
 * 少一行、多一个空格，加载器都认不出来。
 */
const client: UserConfig = {
  name: `${ID}/client`,
  entry: { client: 'src/client/index.ts' },
  outDir: 'lib',
  format: 'cjs',
  platform: 'browser',
  target: 'es2023',
  // 与宿主半共用 lib/，谁都不能清目录（clean 只有宿主那半开）
  clean: false,
  // 客户端产物由 __ModuleLoader__ 直接执行，不经 tsc/TS 消费者；声明文件这一面不稳定
  dts: false,
  sourcemap: true,
  deps: {
    neverBundle: [...CLIENT_MODULE_TABLE],
    alwaysBundle: (specifier: string) => !CLIENT_MODULE_TABLE.includes(specifier),
  },
  outputOptions: {
    entryFileNames: 'client.js',
    banner: `window.__ModuleLoader__.load({ id: ${JSON.stringify(ID)}, factory: (require) => {`,
    footer: 'return module.exports; } });',
    intro: 'var module = { exports: {} }; var exports = module.exports;',
  },
}

/** 两个 build block；`satisfies` 保留类型校验，运行期是普通数组（无 tsdown 依赖）。 */
export default [host, client] satisfies UserConfig[]
