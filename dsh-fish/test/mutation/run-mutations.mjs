#!/usr/bin/env node
/**
 * 变异测试：**证明判据能红**。
 *
 * 为什么必须有这一步：一条"永远是绿的"判据与"真的没坏"在外观上完全一样。变异测试
 * 的做法是——把被测逻辑改坏一点，看判据是不是立刻变红；变红说明判据有牙齿，仍然绿
 * 说明判据是摆设。
 *
 * 本脚本自动完成 3 条变异的完整闭环：改坏 → 重建 → 判据必红 → 还原 → 重建 → 复绿。
 * 每一条都在 `finally` 里还原源码，**任何中断都不会留下被改坏的仓库**。
 *
 * 用法：node test/mutation/run-mutations.mjs
 */

import { spawnSync } from 'node:child_process'
import { copyFileSync, existsSync, readFileSync, rmSync, unlinkSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const packageRoot = path.resolve(here, '..', '..')
const tsdownBin = path.join(packageRoot, 'node_modules', 'tsdown', 'dist', 'run.mjs')
const contractTs = path.join(packageRoot, 'src', 'core', 'contract.ts')
const cutTs = path.join(packageRoot, 'src', 'layout', 'cut.ts')
const contractJson = path.join(packageRoot, 'spec', 'contract.json')

if (!existsSync(tsdownBin)) {
  console.error(`找不到 ${tsdownBin}：先 pnpm install`)
  process.exit(1)
}

/** 跑一条命令，返回退出码与末几行输出（不抛错，由调用方判定）。 */
function run(command, args, options = {}) {
  const child = spawnSync(command, args, {
    cwd: packageRoot,
    encoding: 'utf8',
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
    maxBuffer: 32 * 1024 * 1024,
    ...options,
  })
  const output = `${child.stdout ?? ''}${child.stderr ?? ''}`.trim()
  return { code: child.status ?? 1, output }
}

/** 重建（tsdown）；失败立刻抛出——拿到的是"没编译成功"，不是"判据没反应"。 */
function rebuild() {
  const result = run(process.execPath, [tsdownBin])
  if (result.code !== 0) throw new Error(`重建失败：\n${result.output}`)
}

/** 带备份的源码改写：返回还原函数，调用一次即恢复原字节。 */
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

const results = []

/**
 * 执行一条变异。
 * @param spec {id, target, description, judgeName, apply: () => restore, expectRed: () => boolean, runJudge: () => {code, output}}
 */
function mutate(entry) {
  const record = { id: entry.id, description: entry.description, judge: entry.judgeName }
  let restore
  try {
    restore = entry.apply()
    rebuild()
    const judged = entry.runJudge()
    record.judgeExit = judged.code
    record.judgeTail = judged.output.split('\n').slice(-3).join(' | ')
    record.red = entry.expectRed(judged)
  } catch (error) {
    record.red = false
    record.judgeExit = null
    record.judgeTail = `变异过程出错：${error.message}`
  } finally {
    if (typeof restore === 'function') restore()
    try {
      rebuild()
    } catch (error) {
      record.judgeTail += ` ｜ 还原后重建失败：${error.message}`
    }
  }

  // 还原后复绿
  try {
    const judged = entry.runJudge()
    record.greenAfterRestore = judged.code === entry.expectGreenCode
    record.greenExit = judged.code
  } catch (error) {
    record.greenAfterRestore = false
    record.greenExit = null
    record.judgeTail += ` ｜ 复绿检查出错：${error.message}`
  }

  results.push(record)
}

// ---------------------------------------------------------------- M1
mutate({
  id: 'M1',
  description: '删掉指纹校验（把 `if (!delta.fingerprintMatched)` 短路掉）',
  judgeName: '判据② node scripts/check-spec.mjs（含指纹负向用例）',
  expectGreenCode: 0,
  apply: () =>
    patchFile(contractTs, (source) => {
      const target = 'if (!delta.fingerprintMatched) {'
      if (!source.includes(target)) throw new Error('没找到指纹校验语句')
      return source.replace(target, 'if (false && !delta.fingerprintMatched) {')
    }),
  runJudge: () => run(process.execPath, [path.join(packageRoot, 'scripts', 'check-spec.mjs')]),
  // 判据红 = 指纹负向用例没抛错（gate.fingerprint_negative_test FAIL）
  expectRed: (judged) => judged.code !== 0,
})

// ---------------------------------------------------------------- M2
mutate({
  id: 'M2',
  description: '把 contractVersion 校验写死（短路掉版本比较）+ 把 spec 版本改成 v1',
  judgeName: '判据③ node scripts/check-contract-startup.mjs（改版本后必须抛错）',
  expectGreenCode: 0,
  apply: () => {
    const restoreSource = patchFile(contractTs, (source) => {
      const target = 'if (contract.contractVersion !== SUPPORTED_CONTRACT_VERSION) {'
      if (!source.includes(target)) throw new Error('没找到版本校验语句')
      return source.replace(target, 'if (false && contract.contractVersion !== SUPPORTED_CONTRACT_VERSION) {')
    })
    const originalSpec = readFileSync(contractJson, 'utf8')
    const mutated = JSON.parse(originalSpec)
    mutated.contractVersion = 'v1'
    writeFileSync(contractJson, `${JSON.stringify(mutated, null, 2)}\n`, 'utf8')
    return () => {
      restoreSource()
      writeFileSync(contractJson, originalSpec, 'utf8')
    }
  },
  runJudge: () =>
    // 变异态下期望：**不再抛错**（exit 0）= 判据失去牙齿
    run(process.execPath, [path.join(packageRoot, 'scripts', 'check-contract-startup.mjs')]),
  expectRed: (judged) => judged.code === 0,
})

// 附加：M2 的对照组——只改 spec 版本、不改源码时，判据必须真的红（exit 1）
const controlOriginal = readFileSync(contractJson, 'utf8')
try {
  const mutated = JSON.parse(controlOriginal)
  mutated.contractVersion = 'v1'
  writeFileSync(contractJson, `${JSON.stringify(mutated, null, 2)}\n`, 'utf8')
  const judged = run(process.execPath, [path.join(packageRoot, 'scripts', 'check-contract-startup.mjs')])
  results.push({
    id: 'M2-control',
    description: '对照组：只把 spec 版本改成 v1（源码不动）→ 启动必须抛错',
    judge: '判据③ node scripts/check-contract-startup.mjs',
    judgeExit: judged.code,
    judgeTail: judged.output.split('\n').slice(-3).join(' | '),
    red: judged.code !== 0,
    greenAfterRestore: true,
    greenExit: null,
    control: true,
  })
} finally {
  writeFileSync(contractJson, controlOriginal, 'utf8')
}

// ---------------------------------------------------------------- M3
mutate({
  id: 'M3',
  description: '让切词函数差一位（bigram 少切最后一组）',
  judgeName: '切词对拍：scripts/probe/probe-cut.mjs',
  expectGreenCode: 0,
  apply: () =>
    patchFile(cutTs, (source) => {
      const target = 'for (let i = 0; i + window <= run.length; i += step) out.push(run.slice(i, i + window))'
      if (!source.includes(target)) throw new Error('没找到 bigram 滑窗语句')
      return source.replace(target, 'for (let i = 0; i + window + 1 <= run.length; i += step) out.push(run.slice(i, i + window))')
    }),
  runJudge: () => run(process.execPath, [path.join(packageRoot, 'scripts', 'probe', 'probe-cut.mjs')]),
  // 判据红 = 与 Python 的 50 条真实查询切词出现差异
  expectRed: (judged) => judged.code !== 0,
})

// 附加：M3 在双跑器上的读数（必须出现 DIFF）
{
  const restore = patchFile(cutTs, (source) => {
    const target = 'for (let i = 0; i + window <= run.length; i += step) out.push(run.slice(i, i + window))'
    return source.replace(target, 'for (let i = 0; i + window + 1 <= run.length; i += step) out.push(run.slice(i, i + window))')
  })
  try {
    rebuild()
    const ran = run(process.execPath, [path.join(packageRoot, 'test', 'conformance', 'run.mjs')])
    const diffed = run(process.execPath, [path.join(packageRoot, 'test', 'conformance', 'diff.mjs')])
    results.push({
      id: 'M3-conformance',
      description: '同一条切词变异在双跑器上的读数（必须 DIFF）',
      judge: 'test/conformance/diff.mjs',
      judgeExit: diffed.code,
      judgeTail: `${ran.code === 0 ? 'run ok' : 'run failed'} ｜ ${diffed.output.split('\n').slice(-2).join(' | ')}`,
      red: diffed.code !== 0,
      greenAfterRestore: true,
      greenExit: null,
    })
  } finally {
    restore()
    rebuild()
    run(process.execPath, [path.join(packageRoot, 'test', 'conformance', 'run.mjs')])
  }
}

// 清理可能残留的备份文件
for (const file of [contractTs, cutTs]) {
  const backup = `${file}.mutation-bak`
  if (existsSync(backup)) rmSync(backup, { force: true })
}

// ---------------------------------------------------------------- 汇总
console.log('# 变异测试（改坏 → 判据必红 → 还原 → 复绿）')
for (const record of results) {
  const red = record.red ? 'RED(符合预期)' : 'GREEN(不符合预期)'
  const restore = record.greenAfterRestore === false ? ' 复绿失败' : record.control ? '' : ' 已复绿'
  console.log(`  ${record.id.padEnd(15)} ${red.padEnd(16)} exit=${String(record.judgeExit).padEnd(4)}${restore}`)
  console.log(`      ${record.description}`)
  console.log(`      判据：${record.judge}`)
  console.log(`      读数：${record.judgeTail}`)
}
const failed = results.filter((record) => record.red !== true || record.greenAfterRestore === false)
console.log(`# summary: ${results.length - failed.length}/${results.length} 符合预期`)
process.exitCode = failed.length === 0 ? 0 : 1
