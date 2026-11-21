#!/usr/bin/env node
/**
 * 双跑器 · TS 侧。
 *
 * 契约（与 `run.py` 严格一致）：
 *   输入 = `fixtures/*.json`（每个文件一个 fixture）；
 *   输出 = `out/ts.json`，形如
 *     `{ "side": "ts", "node": "v24.x", "fixtures": [ {id, op, status, output?|reason?} ] }`
 *
 * `status` 的三种取值都**不是**"静默跳过"：
 *   · `ok`      —— 执行成功，`output` 参与对拍；
 *   · `pending` —— 本侧尚未实现（必须同时在 allowlist.json 里登记责任人 + 到期日）；
 *   · `error`   —— 执行失败，`reason` 写明原因（对拍时按失败处理）。
 *
 * T01 只实现 `cut`（纯函数、无 IO、可机器证明 0 diff）。注入 / 检索 / 身份三条属 T02，
 * 在这里显式 `pending`——**不写一个假实现来凑绿**：假实现会让"diff=0"失去意义，
 * 而 T01 的全部价值就是让后续每条 diff 都可信。
 *
 * 用法：node test/conformance/run.mjs [--out <file>]
 */

import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const packageRoot = path.resolve(here, '..', '..')
const fixturesDir = path.join(here, 'fixtures')
const outDir = path.join(here, 'out')
const libPath = path.join(packageRoot, 'lib', 'index.js')

const outIndex = process.argv.indexOf('--out')
const outPath = outIndex > 0 ? path.resolve(process.argv[outIndex + 1]) : path.join(outDir, 'ts.json')

if (!existsSync(libPath)) {
  console.error(`找不到 ${libPath}：先跑 pnpm run build`)
  process.exit(1)
}

const lib = await import(pathToFileURL(libPath).href)

/**
 * TS 侧执行器表：**只有实现了才有条目**。
 * 新增一个 op 的正确姿势：在这里加一个纯函数执行器，然后删掉 allowlist 里对应的
 * pending 条目——两件事必须同一次提交完成，否则 allowlist 会变成永久豁免。
 */
const EXECUTORS = {
  /**
   * 切词对拍：同一组输入串 → token 序列。
   * 用的是**生产代码** `lib.cutTokens`（能力块 #12），不是为测试另写的一份。
   */
  cut: (fixture) => {
    const inputs = Array.isArray(fixture.inputs) ? fixture.inputs : []
    if (inputs.length === 0) throw new Error('cut fixture 缺少 inputs[]')
    return { tokens: inputs.map((text) => lib.cutTokens(String(text))) }
  },
}

/** 读取全部 fixtures（按文件名排序，保证两次运行顺序一致）。 */
function loadFixtures() {
  if (!existsSync(fixturesDir)) throw new Error(`fixtures 目录不存在：${fixturesDir}`)
  const files = readdirSync(fixturesDir).filter((name) => name.endsWith('.json')).sort()
  if (files.length === 0) throw new Error(`fixtures 目录为空：${fixturesDir}`)
  return files.map((name) => {
    const filePath = path.join(fixturesDir, name)
    const parsed = JSON.parse(readFileSync(filePath, 'utf8'))
    if (typeof parsed.id !== 'string' || typeof parsed.op !== 'string') {
      throw new Error(`fixture 缺少 id/op：${name}`)
    }
    return { file: name, fixture: parsed }
  })
}

const results = []
for (const { file, fixture } of loadFixtures()) {
  const base = { id: fixture.id, op: fixture.op, file }
  const executor = EXECUTORS[fixture.op]
  if (executor === undefined) {
    results.push({
      ...base,
      status: 'pending',
      reason: `TS 侧未实现 op=${fixture.op}（T01 只交付 cut；其余见 allowlist.json 的 pending-impl 条目）`,
    })
    continue
  }
  try {
    results.push({ ...base, status: 'ok', output: executor(fixture) })
  } catch (error) {
    results.push({ ...base, status: 'error', reason: error instanceof Error ? error.message : String(error) })
  }
}

const payload = {
  side: 'ts',
  node: process.version,
  built: existsSync(libPath),
  fixtures: results,
}

mkdirSync(path.dirname(outPath), { recursive: true })
writeFileSync(outPath, `${JSON.stringify(payload, null, 2)}\n`, 'utf8')

const ok = results.filter((r) => r.status === 'ok').length
const pending = results.filter((r) => r.status === 'pending').length
const errors = results.filter((r) => r.status === 'error').length
console.log(`# run.mjs（TS 侧）→ ${outPath}`)
for (const r of results) console.log(`  ${r.status.padEnd(7)} ${r.id}${r.reason ? `  ${r.reason}` : ''}`)
console.log(`# ok=${ok} pending=${pending} error=${errors}`)
process.exitCode = errors === 0 ? 0 : 1
