#!/usr/bin/env node
/**
 * 变异测试 · T03 批次：**证明三段新能力（看板 / 候选 / ④C）与契约计数的判据有牙齿**。
 *
 * 三条变异对应任务书 E.1/E.2/E.3：
 *   · M01 看板段的"缺省语义"渲染成空白 → 对拍必红（留白会被读成"今天没人"，而不是"没签到的确没人"）；
 *   · M02 候选去掉 24h 去重 → 对拍必红（同一条候选每轮都重推，是"推送变成噪音"的第一步）；
 *   · M03 `budgetRule` 改成 tiktoken 口径但实现不动 → **spec 一致性判据**必红。
 *
 * 每条都走完整闭环：**改坏 → 重建 → 判据必红 → 还原 → 重建 → 复绿**，
 * 还原写在 `finally` 里——任何中断都不会留下被改坏的仓库。
 *
 * 用法：FISH_HOME=<生产树> node test/mutation/run-t03-mutations.mjs
 */

import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { copyFileSync, existsSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const packageRoot = path.resolve(here, '..', '..')
const tsdownBin = path.join(packageRoot, 'node_modules', 'tsdown', 'dist', 'run.mjs')
const conformanceDir = path.join(packageRoot, 'test', 'conformance')
const specDir = path.join(packageRoot, 'spec')

if (!existsSync(tsdownBin)) {
  console.error(`找不到 ${tsdownBin}：先 pnpm install`)
  process.exit(1)
}
if ((process.env.FISH_HOME ?? '').trim() === '') {
  console.error('缺少 FISH_HOME：对拍与 check-spec 都要拿它定位生产树（只读）')
  process.exit(1)
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

function rebuild() {
  const result = run(process.execPath, [tsdownBin])
  if (result.code !== 0) throw new Error(`重建失败：\n${result.output}`)
}

/** 跑一遍完整对拍：两侧 + diff。返回 `{code, diff, matched, output}`。 */
function runConformance() {
  const ts = run(process.execPath, [path.join(conformanceDir, 'run.mjs')])
  const py = run('python', [path.join(conformanceDir, 'run.py')])
  const diff = run(process.execPath, [path.join(conformanceDir, 'diff.mjs')])
  const summary = /diff=(\d+)\s+matched=(\d+)/.exec(diff.output)
  return {
    code: diff.code,
    diff: summary === null ? -1 : Number(summary[1]),
    matched: summary === null ? -1 : Number(summary[2]),
    output: [ts.output, py.output, diff.output].join('\n'),
  }
}

/** 跑 check-spec 并解析 JSON（判据读数逐条可比）。 */
function runCheckSpec() {
  const result = run(process.execPath, [path.join(packageRoot, 'scripts', 'check-spec.mjs'), '--json'])
  let parsed = null
  try {
    parsed = JSON.parse(result.output)
  } catch {
    parsed = null
  }
  const failed = (parsed?.checks ?? []).filter((check) => !check.ok).map((check) => check.id)
  return { code: result.code, pass: parsed?.pass === true, failed, output: result.output }
}

/** 带备份的源码改写：返回还原函数（调用一次即恢复原字节）。 */
function patchFile(filePath, transform) {
  const original = readFileSync(filePath, 'utf8')
  const patched = transform(original)
  if (patched === original) throw new Error(`变异注入失败（没匹配到目标代码）：${path.basename(filePath)}`)
  const backup = `${filePath}.mutation-bak`
  copyFileSync(filePath, backup)
  writeFileSync(filePath, patched, 'utf8')
  return () => {
    writeFileSync(filePath, original, 'utf8')
    if (existsSync(backup)) unlinkSync(backup)
  }
}

const boardTs = path.join(packageRoot, 'src', 'domain', 'board.ts')
const pushTs = path.join(packageRoot, 'src', 'domain', 'push.ts')
const injectionSpec = path.join(specDir, 'injection.json')
const contractSpec = path.join(specDir, 'contract.json')

const results = []
let overallOk = true

// ---- M01：看板段缺省语义渲染成空白（"今天还没有人签到"整句消失） ----
{
  const record = { id: 'M01', description: '看板段缺省语义渲染成空白（空板不再明写"今天还没有人签到"）' }
  let restore
  try {
    restore = patchFile(boardTs, (source) => {
      const before = '    return empty\n'
      if (!source.includes(before)) throw new Error('M01 目标代码没匹配到')
      return source.replace(before, '    void empty\n    return \'\'\n')
    })
    rebuild()
    const red = runConformance()
    record.redDiff = red.diff
    record.redExit = red.code
    record.redAsExpected = red.diff > 0 && red.code !== 0
  } finally {
    restore?.()
  }
  rebuild()
  const green = runConformance()
  record.greenDiff = green.diff
  record.greenMatched = green.matched
  record.greenExit = green.code
  record.greenAsExpected = green.diff === 0 && green.code === 0
  overallOk &&= record.redAsExpected && record.greenAsExpected
  results.push(record)
  console.log(
    `${record.redAsExpected && record.greenAsExpected ? 'PASS' : 'FAIL'}  ${record.id}  ` +
      `红：diff=${record.redDiff}（exit=${record.redExit}）→ 绿：diff=${record.greenDiff} matched=${record.greenMatched}（exit=${record.greenExit}）  ${record.description}`,
  )
}

// ---- M02：候选去掉 24h 去重（同一会话反复收到同一条） ----
{
  const record = { id: 'M02', description: '候选去掉 24h 去重（push_log 里推过的 activity 候选重新出现）' }
  let restore
  try {
    restore = patchFile(pushTs, (source) => {
      const before = '      if (pushed.has(code)) continue\n'
      if (!source.includes(before)) throw new Error('M02 目标代码没匹配到')
      return source.replace(before, '      // MUTATION: 24h 去重已移除\n')
    })
    rebuild()
    const red = runConformance()
    record.redDiff = red.diff
    record.redExit = red.code
    record.redAsExpected = red.diff > 0 && red.code !== 0
  } finally {
    restore?.()
  }
  rebuild()
  const green = runConformance()
  record.greenDiff = green.diff
  record.greenMatched = green.matched
  record.greenExit = green.code
  record.greenAsExpected = green.diff === 0 && green.code === 0
  overallOk &&= record.redAsExpected && record.greenAsExpected
  results.push(record)
  console.log(
    `${record.redAsExpected && record.greenAsExpected ? 'PASS' : 'FAIL'}  ${record.id}  ` +
      `红：diff=${record.redDiff}（exit=${record.redExit}）→ 绿：diff=${record.greenDiff} matched=${record.greenMatched}（exit=${record.greenExit}）  ${record.description}`,
  )
}

// ---- M03：budgetRule 改成 tiktoken 口径，实现不动 → spec 一致性判据必红 ----
//
// 为什么是"改生成后的 spec + 同步摘要"而不是"改生成器再重跑"：
//   · 手改 spec 不同步摘要 → 会先被 `gate.spec_digests` 拦下，红灯变成"摘要不一致"，
//     判据等于被摘要校验挡住（那就没证明 budgetRule 那条判据有牙齿）；
//   · 重跑生成器 → 依赖 `p0/` 这棵树**当刻**的形状。实测 2026-10-27 下午 p0 里新增了
//     一个 `corpus` 工具，`gen-spec` 因"新工具没有 writeClass 标注"而失败（已登记为
//     下一棒的待办，见 NOTES-T03 §6）——判据不该被别人的开发进度卡住。
// 所以这里直接把 spec 改成"口径与实现不同步"的样子，并把摘要改对：**唯一该红的就是预算口径**。
{
  const record = { id: 'M03', description: 'budgetRule 改成 tiktoken 口径（实现仍走 fallback）→ spec 一致性判据必红' }
  const injectionBackup = readFileSync(injectionSpec, 'utf8')
  const contractBackup = readFileSync(contractSpec, 'utf8')
  try {
    const mutated = JSON.parse(injectionBackup)
    mutated.budgetRule = { ...mutated.budgetRule, rule: 'tiktoken', formula: 'len(encode(text))' }
    const mutatedText = `${JSON.stringify(mutated, null, 2)}\n`
    writeFileSync(injectionSpec, mutatedText, 'utf8')
    // 同步摘要：让"摘要校验"保持绿，红灯只剩预算口径那两条（这是本条变异的设计）。
    const contract = JSON.parse(contractBackup)
    contract.specFiles['injection.json'] = createHash('sha256').update(readFileSync(injectionSpec)).digest('hex')
    writeFileSync(contractSpec, `${JSON.stringify(contract, null, 2)}\n`, 'utf8')

    const red = runCheckSpec()
    record.redFailed = red.failed
    record.redExit = red.code
    record.redAsExpected = red.code !== 0 && red.failed.includes('spec.injection_budget_rule')
    record.redNote =
      red.failed.includes('gate.spec_digests') || red.failed.includes('x.unexpected')
        ? '⚠ 除 budgetRule 外还有摘要/解析级失败（说明摘要没同步对，判据被别的检查挡住了）'
        : ''
  } finally {
    writeFileSync(injectionSpec, injectionBackup, 'utf8')
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
      `红：failed=[${(record.redFailed ?? []).join(',')}]（exit=${record.redExit}）→ 绿：pass=${record.greenPass}（exit=${record.greenExit}）  ${record.description}${record.redNote ? `  ${record.redNote}` : ''}`,
  )
}

const bad = results.filter((record) => !(record.redAsExpected && record.greenAsExpected))
console.log(`# summary: ${results.length - bad.length}/${results.length} 符合预期（改坏必红、还原必绿）`)
process.exitCode = bad.length === 0 ? 0 : 1
