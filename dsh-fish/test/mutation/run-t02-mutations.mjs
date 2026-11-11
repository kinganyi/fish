#!/usr/bin/env node
/**
 * 变异测试 · T02 批次：**证明四条只读能力的对拍有牙齿**。
 *
 * 与 T01 的 `run-mutations.mjs` 分开写：那个文件钉的是**契约门禁 / 切词**，本文件钉的是
 * **对拍本身**（注入段顺序、检索状态过滤、身份证据分级）。同一批变异放进一个脚本，
 * 出红时分不清是"哪一代判据"退化了。
 *
 * 每条变异都走完整闭环：**改坏 → 重建 → 判据必红 → 还原 → 重建 → 复绿**，
 * 且还原写在 `finally` 里——任何中断都不会留下被改坏的仓库。
 *
 * 用法：node test/mutation/run-t02-mutations.mjs
 */

import { spawnSync } from 'node:child_process'
import { copyFileSync, existsSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const packageRoot = path.resolve(here, '..', '..')
const tsdownBin = path.join(packageRoot, 'node_modules', 'tsdown', 'dist', 'run.mjs')
const conformanceDir = path.join(packageRoot, 'test', 'conformance')

if (!existsSync(tsdownBin)) {
  console.error(`找不到 ${tsdownBin}：先 pnpm install`)
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

/** 跑一遍完整对拍：两侧 + diff。返回 `{code, diff, output}`。 */
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

const injectTs = path.join(packageRoot, 'src', 'domain', 'inject.ts')
const memoryTs = path.join(packageRoot, 'src', 'domain', 'memory.ts')
const identityTs = path.join(packageRoot, 'src', 'domain', 'identity.ts')

const MUTATIONS = [
  {
    id: 'M01',
    description: '注入段顺序调换一位：③规矩 与 ④该注意什么 的装配次序互换',
    file: injectTs,
    transform(source) {
      // 只动**装配点**：把这两段在 body parts 里的先后换个位置。
      const before =
        "  if (rulesText !== '') parts.push(rulesText)\n" +
        "  if (attentionText !== '') parts.push(attentionText)\n"
      const after =
        "  if (attentionText !== '') parts.push(attentionText)\n" +
        "  if (rulesText !== '') parts.push(rulesText)\n"
      if (!source.includes(before)) throw new Error('M01 目标代码没匹配到')
      return source.replace(before, after)
    },
  },
  {
    id: 'M02',
    description: '检索去掉 status 过滤：superseded / withdrawn 也会被命中',
    file: memoryTs,
    transform(source) {
      // 两处都去掉：FTS 下推过滤 + 回取行时的过滤（Python 侧这两处是同一个纪律）。
      const before = "  if (!includeSuperseded) filters.push(\"ci.status = 'active'\")\n"
      const afterFts = source.replace(before, '  // MUTATION: status 过滤已移除\n')
      if (afterFts === source) throw new Error('M02 第一处没匹配到')
      const beforeRow = "  if (!includeSuperseded) sql += \" AND status = 'active'\"\n"
      const afterRow = afterFts.replace(beforeRow, '  // MUTATION: 回取行时的 status 过滤已移除\n')
      if (afterRow === afterFts) throw new Error('M02 第二处没匹配到')
      return afterRow
    },
  },
  {
    id: 'M03',
    description: '身份证据升级：valid 凭证一律报 machine（self-reported / inherited 被抬升）',
    file: identityTs,
    transform(source) {
      const before = `  return {
    status: IDENTITY_STATUS.VALID,
    citizenId,
    sessionKey: info.session_key,
    evidence: info.evidence,
  }`
      const after = `  return {
    status: IDENTITY_STATUS.VALID,
    citizenId,
    sessionKey: info.session_key,
    evidence: 'machine',
  }`
      if (!source.includes(before)) throw new Error('M03 目标代码没匹配到')
      return source.replace(before, after)
    },
  },
]

const results = []
let overallOk = true
for (const mutation of MUTATIONS) {
  const record = { id: mutation.id, description: mutation.description }
  let restore
  try {
    restore = patchFile(mutation.file, mutation.transform)
    rebuild()
    const red = runConformance()
    record.redDiff = red.diff
    record.redMatched = red.matched
    record.redExit = red.code
    record.redAsExpected = red.diff > 0 && red.code !== 0
    if (!record.redAsExpected) overallOk = false
  } finally {
    if (restore !== undefined) restore()
  }
  rebuild()
  const green = runConformance()
  record.greenDiff = green.diff
  record.greenMatched = green.matched
  record.greenExit = green.code
  record.greenAsExpected = green.diff === 0 && green.code === 0
  if (!record.greenAsExpected) overallOk = false
  results.push(record)
  console.log(
    `${record.redAsExpected && record.greenAsExpected ? 'PASS' : 'FAIL'}  ${record.id}  ` +
      `红：diff=${record.redDiff}（exit=${record.redExit}）→ 绿：diff=${record.greenDiff} matched=${record.greenMatched}（exit=${record.greenExit}）  ${record.description}`,
  )
}

const bad = results.filter((record) => !(record.redAsExpected && record.greenAsExpected))
console.log(`# summary: ${results.length - bad.length}/${results.length} 符合预期（改坏必红、还原必绿）`)
process.exitCode = bad.length === 0 ? 0 : 1
