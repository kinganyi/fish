#!/usr/bin/env node
/**
 * T05 变异自证（3 条）：**每一条都必须让对应判据变红，还原后必须复绿**。
 *
 * | # | 改坏什么 | 应红的判据 | 为什么这条值得证 |
 * |---|---|---|---|
 * | M01 | 知识库的强制忽略规则少一条（去掉 `90-模板/**`） | kb 对拍（`kb-tree-root` 等） | "忽略规则是硬边界"只有对着真实文件树才发现漏；规则表自洽永远自洽 |
 * | M02 | 别名展开少一跳（`ALIAS_MAX_HOPS` 8 → 1） | 项目/别名对拍（`alias-list`） | 多跳别名是**单向**展开的语义核心：少一跳，`anyi-kb` 就不再解析成 `ANYI` |
 * | M03 | 语料读回时丢掉正文的一截（"段拼接丢一段"） | 语料对拍（`corpus-read-seq`） | 正文少了几个字**不会报错**，只会让人以为"库里就是这么存的" |
 *
 * 三条纪律（T035/T04 的同一套）：
 *   1. **改坏 → 跑判据 → 还原 → 复跑** 四步齐全，缺"复绿"这一步的变异等于没做；
 *   2. 每段都在 `finally` 里还原，并在收尾**逐字节**核对文件已回原样；
 *   3. 三条变异都只改 **TS 实现**，所以 golden 侧（`out/py.json`）不变——复绿只重跑
 *      TS 侧 + diff，不重跑 Python（重跑只会让"同一份基准"变成"另一份基准"）。
 *
 * 用法：`FISH_HOME=… node test/mutation/run-t05-mutations.mjs`
 */

import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { readFileSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const packageRoot = path.resolve(here, '..', '..')
const nodeBin = process.execPath
const tsdownBin = path.join(packageRoot, 'node_modules', 'tsdown', 'dist', 'run.mjs')
const pythonBin = process.env.FISH_PYTHON ?? 'python'

const envHome = (process.env.FISH_HOME ?? '').trim()
const fishHome = envHome !== '' ? path.resolve(envHome) : path.resolve(packageRoot, '..', 'p0')
const baseEnv = { ...process.env, FISH_HOME: fishHome, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' }

const sha = (buffer) => createHash('sha256').update(buffer).digest('hex')
const results = []

/** 跑一条 node 脚本，返回 `{code, out}`。 */
function runNode(script, args = []) {
  const child = spawnSync(nodeBin, [script, ...args], {
    cwd: packageRoot,
    encoding: 'utf8',
    env: baseEnv,
    maxBuffer: 64 * 1024 * 1024,
    timeout: 600000,
  })
  return { code: child.status ?? 1, out: `${child.stdout ?? ''}${child.stderr ?? ''}` }
}

/** 跑 Python 侧的 golden（只在基线那一次跑）。 */
function runPython() {
  const child = spawnSync(pythonBin, ['test/conformance/run.py'], {
    cwd: packageRoot,
    encoding: 'utf8',
    env: baseEnv,
    maxBuffer: 64 * 1024 * 1024,
    timeout: 600000,
  })
  return { code: child.status ?? 1, out: `${child.stdout ?? ''}${child.stderr ?? ''}` }
}

/** 构建 lib（变异后必须重建，否则判据跑的还是旧产物）。 */
function build() {
  const child = spawnSync(nodeBin, [tsdownBin], { cwd: packageRoot, encoding: 'utf8', env: baseEnv, maxBuffer: 8 * 1024 * 1024 })
  if (child.status !== 0) throw new Error(`构建失败：${(child.stderr ?? '').slice(0, 500)}`)
}

/**
 * 跑一次"TS 侧 + 同侧比对"，返回 `{code, diffIds, summary}`。
 *
 * golden 侧（`out/py.json`）由基线那次跑出来，变异只改 TS 实现，所以它不需要重跑——
 * 重跑 Python 会让"同一份基准"悄悄换成另一份，那种复绿不算证据。
 */
function runConformanceTs() {
  const ts = runNode('test/conformance/run.mjs')
  if (ts.code !== 0) return { code: ts.code, diffIds: ['ts-run-error'], summary: ts.out.slice(-400) }
  const diff = runNode('test/conformance/diff.mjs')
  const lines = diff.out.split('\n')
  const diffIds = lines.filter((line) => line.startsWith('DIFF')).map((line) => line.trim().split(/\s+/)[1])
  const summary = lines.filter((line) => line.startsWith('# summary')).join(' ')
  return { code: diff.code, diffIds, summary }
}

/** 一条变异：改坏 → 判据红 → 还原 → 判据绿（并逐字节核对还原）。 */
function mutate({ id, title, file, patch, expectDiff }) {
  const target = path.join(packageRoot, file)
  const original = readFileSync(target)
  const outcome = { id, title, ok: false }
  try {
    const patched = patch(original.toString('utf8'))
    if (patched === original.toString('utf8')) {
      throw new Error(`变异没有改动任何字节（补丁没生效）：${file}`)
    }
    writeFileSync(target, patched, 'utf8')
    build()
    const red = runConformanceTs()
    outcome.redDiff = red.diffIds
    outcome.redSummary = red.summary
    outcome.hit = expectDiff.every((expected) => red.diffIds.includes(expected))
    if (!outcome.hit) {
      throw new Error(`变异没有命中预期用例 ${expectDiff.join(',')}（实际 DIFF：${red.diffIds.join(',') || '无'}）`)
    }
  } finally {
    writeFileSync(target, original)
    build()
  }
  const green = runConformanceTs()
  outcome.greenDiff = green.diffIds
  outcome.greenSummary = green.summary
  outcome.restored = sha(readFileSync(target)) === sha(original)
  outcome.ok = outcome.hit === true && green.diffIds.length === 0 && outcome.restored
  results.push(outcome)
  return outcome
}

// ---------------------------------------------------------------- 基线（先证明"没变异时是绿的"）
//
// 顺序要紧：**先**跑 Python golden（它生成 `out/py.json`），**再**跑 TS + diff。
// 反过来的话，`diff.mjs` 读旧 `py.json`，"基线"就不再是这一轮的基线。
const python = runPython()
if (python.code !== 0) {
  console.error(`Python golden 侧失败：${python.out.slice(-600)}`)
  process.exit(1)
}
const baseline = runConformanceTs()
if (baseline.diffIds.length !== 0) {
  console.error(`基线就不是绿的：DIFF=${baseline.diffIds.join(',')}（先修好再做变异）`)
  process.exit(1)
}

// ---------------------------------------------------------------- M01 忽略规则少一条
mutate({
  id: 'M01',
  title: '知识库强制忽略规则去掉 `90-模板/**`（kb 对拍必红）',
  file: 'src/domain/kb.ts',
  patch: (source) => {
    const patched = source.replace(
      "export const KB_MANDATORY_DENY = ['.obsidian/**', '.trash/**', '90-模板/**'] as const",
      "export const KB_MANDATORY_DENY = ['.obsidian/**', '.trash/**'] as const",
    )
    if (patched === source) throw new Error('M01 补丁没匹配到 KB_MANDATORY_DENY 定义')
    return patched
  },
  expectDiff: ['kb-tree-root'],
})

// ---------------------------------------------------------------- M02 别名少一跳
mutate({
  id: 'M02',
  title: '别名展开最多 1 跳（ALIAS_MAX_HOPS 8 → 1；项目/别名对拍必红）',
  file: 'src/domain/names.ts',
  patch: (source) => {
    const patched = source.replace('export const ALIAS_MAX_HOPS = 8', 'export const ALIAS_MAX_HOPS = 1')
    if (patched === source) throw new Error('M02 补丁没匹配到 ALIAS_MAX_HOPS 定义')
    return patched
  },
  expectDiff: ['alias-list'],
})

// ---------------------------------------------------------------- M03 语料正文丢一截
mutate({
  id: 'M03',
  title: '语料读回时丢掉正文的一截（"段拼接丢一段"；语料对拍必红）',
  file: 'src/domain/corpus.ts',
  patch: (source) => {
    const anchor = '    const text = truncated ? sliceCodePoints(text0, 0, limit) : text0'
    if (!source.includes(anchor)) throw new Error('M03 补丁没匹配到 corpusRead 的正文切片')
    // 模拟"拼回原文时少了一段"：未截断的分支从第 1 个码点开始（丢掉首字）。
    return source.replace(anchor, '    const text = truncated ? sliceCodePoints(text0, 0, limit) : sliceCodePoints(text0, 1)')
  },
  expectDiff: ['corpus-read-seq'],
})

// ---------------------------------------------------------------- 收尾：最终复绿 + up-to-date 自证
const finalRun = runConformanceTs()
const finalGreen = finalRun.diffIds.length === 0

const failed = results.filter((item) => item.ok !== true)
console.log('# T05 变异自检')
for (const item of results) {
  console.log(
    `${item.ok ? 'PASS' : 'FAIL'}  ${item.id}  ${item.title}\n` +
      `      变异后：DIFF=[${(item.redDiff ?? []).join(', ') || '（无）'}]  命中=${item.hit === true}\n` +
      `      还原后：DIFF=[${(item.greenDiff ?? []).join(', ') || '（无）'}]  逐字节还原=${item.restored === true}`,
  )
}
console.log(
  `${finalGreen ? 'PASS' : 'FAIL'}  收尾复绿  最终 ${finalRun.summary || '（无摘要）'}` +
    `  golden=out/py.json（基线一次）`,
)
console.log(`# summary: ${results.filter((item) => item.ok).length}/${results.length} 变异符合预期；收尾复绿=${finalGreen}`)

const output = JSON.stringify({ results, finalGreen }, null, 2)
writeFileSync(path.join(here, 'out-t05.json'), `${output}\n`, 'utf8')
process.exitCode = failed.length === 0 && finalGreen ? 0 : 1
