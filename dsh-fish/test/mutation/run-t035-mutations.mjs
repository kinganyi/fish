#!/usr/bin/env node
/**
 * 变异测试 · T03.5 批次：**证明"工具契约不写死"的两条新判据与生成器的标注校验有牙齿**。
 *
 * 三条变异（对应任务书「变异 ≥2 条」）：
 *   · M01 `corpus` 的 `writeClass` 写成不存在于取值域的 `readwrite`
 *         → `gen:spec` 必红，**且 spec 一个字节都没动**（标注先校验、后落盘）；
 *   · M02 spec 回退成"18 工具且彼此自洽"的旧形态（删 corpus + declaredToolCount=18 + 同步摘要）
 *         → `check-spec` 必红，且唯一红的是 `spec.tools_match_production`；
 *           这一条同时证明"只靠 declaredToolCount == tools.length 是抓不到落后的"
 *           （内部自洽判据此时应当是绿的——那就是 T03 当时那份 spec 的形态）；
 *   · M03 spec 里 `corpus` 的 `writeClass` 改成 `readwrite`（+ 同步摘要）
 *         → `check-spec` 必红，且唯一红的是 `spec.tools_write_class_domain`。
 *
 * 每条都走完整闭环：**改坏 → 判据必红 → 还原 → 判据复绿**，还原写在 `finally` 里；
 * 收尾再逐字节核对 spec 已回到变异前的状态——中断也不会留下被改坏的仓库。
 *
 * 用法：FISH_HOME=<生产树> node test/mutation/run-t035-mutations.mjs
 */

import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const packageRoot = path.resolve(here, '..', '..')
const specDir = path.join(packageRoot, 'spec')
const genSpecScript = path.join(packageRoot, 'scripts', 'gen-spec.mjs')
const checkSpecScript = path.join(packageRoot, 'scripts', 'check-spec.mjs')
const toolsSpec = path.join(specDir, 'tools.json')
const contractSpec = path.join(specDir, 'contract.json')

/** 变异会碰到的文件：统一在收尾做逐字节还原核对。 */
const MUTABLE_FILES = [genSpecScript, toolsSpec, contractSpec]

if ((process.env.FISH_HOME ?? '').trim() === '') {
  console.error('缺少 FISH_HOME：gen:spec / check-spec 都要拿它定位生产树（只读）')
  process.exit(1)
}
for (const file of [genSpecScript, checkSpecScript, toolsSpec, contractSpec]) {
  if (!existsSync(file)) {
    console.error(`找不到 ${file}：本脚本要求在仓库根下运行`)
    process.exit(1)
  }
}

/** 跑一条命令，返回退出码与输出（不抛错，由调用方判定）。 */
function run(command, args) {
  const child = spawnSync(command, args, {
    cwd: packageRoot,
    encoding: 'utf8',
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
    maxBuffer: 64 * 1024 * 1024,
  })
  return { code: child.status ?? 1, output: `${child.stdout ?? ''}${child.stderr ?? ''}` }
}

function runGenSpec() {
  return run(process.execPath, [genSpecScript])
}

/** 跑 check-spec 并解析 JSON（判据读数逐条可比）。 */
function runCheckSpec() {
  const result = run(process.execPath, [checkSpecScript, '--json'])
  let parsed = null
  try {
    parsed = JSON.parse(result.output)
  } catch {
    parsed = null
  }
  const failed = (parsed?.checks ?? []).filter((check) => !check.ok).map((check) => check.id)
  const passed = (parsed?.checks ?? []).filter((check) => check.ok).map((check) => check.id)
  return { code: result.code, pass: parsed?.pass === true, failed, passed, output: result.output }
}

/** 生成器会写的 7 个产物：判"有没有被写过"用，只看字节、不解释语义。 */
const SPEC_ARTIFACTS = [
  'contract.json',
  'schema.sql',
  'schema.fingerprint.json',
  'events.json',
  'tools.json',
  'injection.json',
  path.join('layout', 'cut.json'),
]

/** 7 个产物的联合字节指纹。 */
function specDigest() {
  const hash = createHash('sha256')
  for (const relative of SPEC_ARTIFACTS) hash.update(readFileSync(path.join(specDir, relative)))
  return hash.digest('hex')
}

/** 把 contract.json 里 tools.json 的登记摘要改成当前实际值（避免"摘要先红"挡住被测判据）。 */
function syncToolsDigest(contractText) {
  const contract = JSON.parse(contractText)
  contract.specFiles['tools.json'] = createHash('sha256').update(readFileSync(toolsSpec)).digest('hex')
  return `${JSON.stringify(contract, null, 2)}\n`
}

/** 带备份的文本改写：返回还原函数（调用一次即恢复原字节）。 */
function patchFile(filePath, transform) {
  const original = readFileSync(filePath, 'utf8')
  const patched = transform(original)
  if (patched === original) throw new Error(`变异注入失败（没匹配到目标代码）：${path.basename(filePath)}`)
  writeFileSync(filePath, patched, 'utf8')
  return () => writeFileSync(filePath, original, 'utf8')
}

const originals = new Map(MUTABLE_FILES.map((file) => [file, readFileSync(file, 'utf8')]))
function restoreAll() {
  for (const [file, text] of originals) writeFileSync(file, text, 'utf8')
}

const results = []
let overallOk = true

// ---- M01：corpus 的 writeClass 不在取值域 → gen:spec 必红，且 spec 未被写过 ----
{
  const record = { id: 'M01', description: "corpus 的 writeClass 改成取值域之外的 'readwrite' → gen:spec 必红（且不落盘）" }
  const digestBefore = specDigest()
  let restore
  try {
    restore = patchFile(genSpecScript, (source) => {
      const before = "  corpus: ['audit', 'yes'],\n"
      if (!source.includes(before)) throw new Error('M01 目标代码没匹配到')
      return source.replace(before, "  corpus: ['readwrite', 'yes'],\n")
    })
    const red = runGenSpec()
    record.redExit = red.code
    record.redMentionsDomain = red.output.includes('不在取值域')
    record.redMentionsTool = red.output.includes('corpus')
    record.specUntouched = specDigest() === digestBefore
    record.redAsExpected = red.code !== 0 && record.redMentionsDomain && record.redMentionsTool && record.specUntouched
  } finally {
    restore?.()
  }
  const green = runGenSpec()
  record.greenExit = green.code
  record.greenAsExpected = green.code === 0
  const check = runCheckSpec()
  record.greenCheckPass = check.pass === true
  overallOk &&= record.redAsExpected && record.greenAsExpected && record.greenCheckPass
  results.push(record)
  console.log(
    `${record.redAsExpected && record.greenAsExpected && record.greenCheckPass ? 'PASS' : 'FAIL'}  ${record.id}  ` +
      `红：exit=${record.redExit} 取值域=${record.redMentionsDomain} spec未动=${record.specUntouched} → ` +
      `绿：gen exit=${record.greenExit} / check ${record.greenCheckPass ? '绿' : '红'}  ${record.description}`,
  )
}

// ---- M02：spec 回退成"18 工具且彼此自洽"的旧形态 → 只有生产比对能抓住 ----
{
  const record = { id: 'M02', description: 'spec 回退到 18 工具（declaredToolCount 同步改成 18 + 摘要同步）→ spec.tools_match_production 必红' }
  const toolsBackup = readFileSync(toolsSpec, 'utf8')
  const contractBackup = readFileSync(contractSpec, 'utf8')
  try {
    const tools = JSON.parse(toolsBackup)
    tools.tools = tools.tools.filter((tool) => tool.name !== 'corpus')
    tools.declaredToolCount = tools.tools.length // 自洽的 18：内部判据抓不到
    writeFileSync(toolsSpec, `${JSON.stringify(tools, null, 2)}\n`, 'utf8')
    writeFileSync(contractSpec, syncToolsDigest(contractBackup), 'utf8')

    const red = runCheckSpec()
    record.redFailed = red.failed
    record.redExit = red.code
    record.countStillGreen = red.passed.includes('spec.tools_count')
    record.redAsExpected =
      red.code !== 0 &&
      red.failed.length === 1 &&
      red.failed[0] === 'spec.tools_match_production' &&
      record.countStillGreen === true
  } finally {
    writeFileSync(toolsSpec, toolsBackup, 'utf8')
    writeFileSync(contractSpec, contractBackup, 'utf8')
  }
  const green = runCheckSpec()
  record.greenExit = green.code
  record.greenPass = green.pass
  record.greenAsExpected = green.code === 0 && green.pass === true
  overallOk &&= record.redAsExpected && record.greenAsExpected
  results.push(record)
  console.log(
    `${record.redAsExpected && record.greenAsExpected ? 'PASS' : 'FAIL'}  ${record.id}  ` +
      `红：failed=[${(record.redFailed ?? []).join(',')}]（exit=${record.redExit}）内部自洽判据仍绿=${record.countStillGreen} → ` +
      `绿：pass=${record.greenPass}（exit=${record.greenExit}）  ${record.description}`,
  )
}

// ---- M03：spec 里 corpus 的 writeClass 越界 → 取值域判据必红 ----
{
  const record = { id: 'M03', description: "spec 里 corpus 的 writeClass 改成 'readwrite'（+ 同步摘要）→ spec.tools_write_class_domain 必红" }
  const toolsBackup = readFileSync(toolsSpec, 'utf8')
  const contractBackup = readFileSync(contractSpec, 'utf8')
  try {
    const tools = JSON.parse(toolsBackup)
    const corpus = tools.tools.find((tool) => tool.name === 'corpus')
    if (corpus === undefined) throw new Error('M03 目标没找到：spec 里没有 corpus')
    corpus.writeClass = 'readwrite'
    writeFileSync(toolsSpec, `${JSON.stringify(tools, null, 2)}\n`, 'utf8')
    writeFileSync(contractSpec, syncToolsDigest(contractBackup), 'utf8')

    const red = runCheckSpec()
    record.redFailed = red.failed
    record.redExit = red.code
    record.redAsExpected =
      red.code !== 0 && red.failed.length === 1 && red.failed[0] === 'spec.tools_write_class_domain'
  } finally {
    writeFileSync(toolsSpec, toolsBackup, 'utf8')
    writeFileSync(contractSpec, contractBackup, 'utf8')
  }
  const green = runCheckSpec()
  record.greenExit = green.code
  record.greenPass = green.pass
  record.greenAsExpected = green.code === 0 && green.pass === true
  overallOk &&= record.redAsExpected && record.greenAsExpected
  results.push(record)
  console.log(
    `${record.redAsExpected && record.greenAsExpected ? 'PASS' : 'FAIL'}  ${record.id}  ` +
      `红：failed=[${(record.redFailed ?? []).join(',')}]（exit=${record.redExit}）→ ` +
      `绿：pass=${record.greenPass}（exit=${record.greenExit}）  ${record.description}`,
  )
}

// ---- 收尾：逐字节核对"仓库已还原" ----
restoreAll()
const leftover = MUTABLE_FILES.filter((file) => readFileSync(file, 'utf8') !== originals.get(file))
if (leftover.length > 0) {
  overallOk = false
  console.error(`FAIL  收尾还原不完整：${leftover.map((file) => path.basename(file)).join(', ')}`)
} else {
  console.log(`# 还原核对：${MUTABLE_FILES.map((file) => path.basename(file)).join(' / ')} 与变异前逐字节一致`)
}

const bad = results.filter((record) => !(record.redAsExpected && record.greenAsExpected))
console.log(`# summary: ${results.length - bad.length}/${results.length} 符合预期（改坏必红、还原必绿）；整体 ${overallOk ? 'PASS' : 'FAIL'}`)
process.exitCode = bad.length === 0 && overallOk ? 0 : 1
