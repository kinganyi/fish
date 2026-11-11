#!/usr/bin/env node
/**
 * 判据 ③ 的判据器：**模拟一次插件启动**，看门禁是否按契约拒绝启用。
 *
 * 走的是 `prepareShadow()`——`apply()` 内部第一步调的就是它。所以"改坏 spec 后脚本
 * 仍然绿"这件事不可能发生：脚本验的就是生产那条路。
 *
 * 判据：
 *   · 契约一致 → exit 0，并打印门禁读数（供人核对）；
 *   · 契约不一致 → exit 1，打印 `CODE: message` + `下一步：…`（对齐宿主插件加载失败的响度）。
 *
 * 用法：node scripts/check-contract-startup.mjs [--json] [--strict-host]
 */

import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { existsSync } from 'node:fs'

const here = path.dirname(fileURLToPath(import.meta.url))
const packageRoot = path.resolve(here, '..')
const libPath = path.join(packageRoot, 'lib', 'index.js')

if (!existsSync(libPath)) {
  console.error(`找不到 ${libPath}：先跑 pnpm run build`)
  process.exit(1)
}

const lib = await import(pathToFileURL(libPath).href)

const config = {
  mode: 'shadow',
  strictHostVersion: process.argv.includes('--strict-host'),
}
const asJson = process.argv.includes('--json')

try {
  const runtime = lib.prepareShadow(config, null)
  const result = {
    started: true,
    mode: runtime.config.mode,
    specDir: runtime.paths.specDir,
    dbPath: runtime.paths.dbPath,
    gate: runtime.gate,
    tools: runtime.spec.tools.tools.length,
    events: runtime.spec.events.types.length,
    injectionSections: runtime.spec.injection.sections.length,
    cutVectors: runtime.spec.cut.vectors.length,
    modelSurface: lib.MODEL_SURFACE,
  }
  if (asJson) {
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`)
  } else {
    console.log('PASS  启动门禁通过（插件可启用）')
    console.log(`  契约版本：${runtime.gate.contractVersion}`)
    console.log(`  宿主版本：${runtime.gate.hostVersion ?? '未探测'}（最低 ${runtime.gate.minHostVersion}）`)
    console.log(`  指纹：${runtime.gate.fingerprint}（verified=${runtime.gate.fingerprintVerified}）`)
    console.log(`  spec 摘要：${runtime.gate.specDigestCount} 个文件 verified=${runtime.gate.specDigestsVerified}`)
    console.log(`  工具 ${result.tools} ｜ 事件 ${result.events} ｜ 注入段 ${result.injectionSections} ｜ 切词向量 ${result.cutVectors}`)
    console.log(`  模型面登记：tools=${lib.MODEL_SURFACE.toolRegistrations} injections=${lib.MODEL_SURFACE.promptInjections}`)
  }
  process.exitCode = 0
} catch (error) {
  const message = error instanceof Error ? `${error.name}: ${error.message}` : String(error)
  const hint = error && typeof error === 'object' && 'hint' in error ? error.hint : undefined
  const details = error && typeof error === 'object' && 'details' in error ? error.details : undefined
  if (asJson) {
    process.stdout.write(`${JSON.stringify({ started: false, error: message, hint, details }, null, 2)}\n`)
  } else {
    console.log('FAIL  启动门禁拒绝启用（这是正确行为）')
    console.log(`  ${message}`)
    if (hint) console.log(`  下一步：${hint}`)
    if (details !== undefined) console.log(`  证据：${JSON.stringify(details)}`)
  }
  process.exitCode = 1
}
