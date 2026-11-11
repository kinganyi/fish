/**
 * apply() 冒烟：用**假 ctx** 跑一次真实装配路径。
 *
 * 为什么不等真宿主：影子期不能启用插件（会与 MCP / hooks 并存），但"apply 到底做了
 * 什么"不能靠读代码——用假 ctx 把注册面抓下来，才能断言两件最要紧的事：
 *   1. **注册了 `/fish`**（人类命令）；
 *   2. **没有注册任何工具、没有碰 systemPrompt**（影子期三条硬边界）。
 *
 * 假 ctx 只实现用到的那两个面（`inject` / `logger`），其余一律不给——如果 `apply`
 * 偷偷用了别的服务，这里会直接抛错（fail loud，而不是"装到宿主上才发现"）。
 *
 * 用法：随 `pnpm test` 一起跑。
 */

import assert from 'node:assert/strict'
import path from 'node:path'
import { existsSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'
import test from 'node:test'

const here = path.dirname(fileURLToPath(import.meta.url))
const packageRoot = path.resolve(here, '..', '..')
const libPath = path.join(packageRoot, 'lib', 'index.js')
if (!existsSync(libPath)) throw new Error(`找不到 ${libPath}：先跑 pnpm run build`)
const lib = await import(pathToFileURL(libPath).href)

/**
 * 造一个最小 cordis ctx：只提供 `inject` 与 `logger`。
 * `inject(deps, cb)` 立刻用"只含 commands 的 scoped ctx"回调一次（模拟服务已就绪）。
 */
function makeFakeContext() {
  const registeredCommands = []
  const injectedDeps = []
  const touchedServices = []
  const scoped = {
    commands: {
      register(definition) {
        registeredCommands.push(definition)
        return () => undefined
      },
    },
    effect(effects, label) {
      touchedServices.push(`effect:${label}`)
      const disposer = effects()
      touchedServices.push(`effect-run:${typeof disposer}`)
      return undefined
    },
  }
  const ctx = {
    // 任何对未实现服务的访问都会在这里炸掉（对象是封闭的）。
    inject(deps, callback) {
      injectedDeps.push(deps)
      callback(scoped)
      return undefined
    },
  }
  return { ctx, registeredCommands, injectedDeps, touchedServices }
}

test('apply：只装配 /fish，不注册工具、不注入提示词', () => {
  const { ctx, registeredCommands, injectedDeps, touchedServices } = makeFakeContext()
  lib.apply(ctx, { mode: 'shadow', logLevel: 'error' })

  assert.deepEqual(injectedDeps, [['commands']], '唯一按需装配的服务应当是 commands')
  assert.equal(registeredCommands.length, 1, '应恰好注册 1 条命令')
  assert.equal(registeredCommands[0].name, lib.COMMAND_NAME)
  assert.equal(registeredCommands[0].name, 'fish')
  assert.equal(typeof registeredCommands[0].handler, 'function')
  assert.ok(touchedServices.some((entry) => entry.startsWith('effect-run:')), '命令 disposer 应由 ctx.effect 托管')

  // 影子期硬边界：模型面登记数为 0（常量与实现必须说同一句话）
  assert.equal(lib.MODEL_SURFACE.toolRegistrations, 0)
  assert.equal(lib.MODEL_SURFACE.promptInjections, 0)
})

test('apply：/fish 的 handler 能返回宿主认识的结果形状', async () => {
  const { ctx, registeredCommands } = makeFakeContext()
  lib.apply(ctx, { mode: 'shadow', logLevel: 'error' })
  const handler = registeredCommands[0].handler

  const help = await handler({ rawInput: '', attachments: [], signal: new AbortController().signal })
  assert.equal(help.kind, 'success')
  assert.match(help.text, /shadow status/)

  const bad = await handler({ rawInput: 'nope', attachments: [], signal: new AbortController().signal })
  assert.equal(bad.kind, 'error')
  assert.equal(typeof bad.text, 'string')
})

test('apply：mode=active 必须被拒绝（影子期没有写路径）', () => {
  const { ctx } = makeFakeContext()
  assert.throws(() => lib.apply(ctx, { mode: 'active' }), /影子期只支持 mode=shadow/)
})

test('apply：commandEnabled=false 时不注册命令（仍然完成门禁与初始化）', () => {
  const { ctx, registeredCommands, touchedServices } = makeFakeContext()
  lib.apply(ctx, { mode: 'shadow', commandEnabled: false, logLevel: 'error' })
  assert.equal(registeredCommands.length, 0)
  assert.equal(touchedServices.filter((entry) => entry.startsWith('effect:')).length, 0)
})
