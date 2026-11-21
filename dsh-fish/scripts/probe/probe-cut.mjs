#!/usr/bin/env node
/**
 * 先验假设 #2：TS 复刻的切词函数与 Python 侧**逐条等价**。
 *
 * 为什么这是 P0：FTS5 的 `unicode61` 不做中文分词，fish 自己把文本切成重叠 bigram
 * 再入库（`p0/fish.py:3857`）。TS 侧只要切词函数差一位，"看起来能查"的检索就会
 * 系统性漏召回——而这种坏不会报错，只会静默少结果。
 *
 * 实现来源：**只跑构建产物 `lib/` 里的那一份**（`src/layout/cut.ts`）。
 * 为什么不在本脚本里再写一份：写两份的第一天就对，第二周就开始漂——而"探针自己漂了"
 * 会给出一个假绿，比没有探针更坏（变异 M3 就是靠这条纪律才抓得住）。
 *
 * 做法：
 *   1. 从生产库（mode=ro）抽 50 条**真实语料字符串**当查询词；
 *   2. 交给 `python scripts/probe/cut_py.py`（内部直接 import p0/fish.py::index_tokens）；
 *   3. 用 lib 的 `cutTokens` 复算，逐条比对 token 序列。
 *
 * 判据：`diff === 0`。任何差异都原文打印（第几条、首个差异位置、两个序列），不许四舍五入。
 *
 * 用法：node scripts/probe/probe-cut.mjs [--json] [--keep <queries.json 输出路径>]
 */

import { DatabaseSync } from 'node:sqlite'
import { spawnSync } from 'node:child_process'
import { existsSync, mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'
import os from 'node:os'

const here = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(here, '..', '..')
const p0Root = path.resolve(repoRoot, '..', 'p0')
const dbPath = process.env.FISH_DB
  ? path.resolve(process.env.FISH_DB)
  : path.resolve(p0Root, 'data', 'index.sqlite')
const pythonBin = process.env.FISH_PYTHON || 'python'
const libPath = path.join(repoRoot, 'lib', 'index.js')

/** 从生产库抽样真实语料：标题 / 知识库路径 / 看板动态，各取一段。 */
function sampleQueries(db, limit = 50) {
  const titles = db
    .prepare("SELECT title AS q FROM code_index WHERE title <> '' ORDER BY code DESC LIMIT 25")
    .all()
  const paths = db
    .prepare("SELECT path AS q FROM kb_access WHERE path <> '' ORDER BY id DESC LIMIT 15")
    .all()
  const posts = db
    .prepare("SELECT text AS q FROM board_posts WHERE text <> '' ORDER BY id DESC LIMIT 10")
    .all()
  return [...titles, ...paths, ...posts].map((r) => String(r.q)).slice(0, limit)
}

function runPython(queriesFile) {
  const child = spawnSync(pythonBin, [path.join(here, 'cut_py.py'), queriesFile], {
    encoding: 'utf8',
    cwd: repoRoot,
    maxBuffer: 32 * 1024 * 1024,
    // 双保险：脚本自己 reconfigure 了 stdout，这里再把解释器层编码钉死。
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
  })
  if (child.error) throw new Error(`python 启动失败: ${child.error.message}`)
  if (child.status !== 0) {
    throw new Error(`python 退出码 ${child.status}: ${(child.stderr || '').trim()}`)
  }
  return JSON.parse(child.stdout)
}

async function main() {
  if (!existsSync(libPath)) {
    throw new Error(`找不到构建产物 ${libPath}：先跑 pnpm run build（本探针只验生产实现，不复制一份）`)
  }
  const lib = await import(pathToFileURL(libPath).href)

  const keepIdx = process.argv.indexOf('--keep')
  const keepPath = keepIdx > 0 ? path.resolve(process.argv[keepIdx + 1]) : ''

  const db = new DatabaseSync(dbPath, { readOnly: true })
  const queries = sampleQueries(db)
  db.close()
  if (queries.length !== 50) {
    throw new Error(`抽样不足 50 条（实得 ${queries.length}）：语料库比预期空`)
  }

  const tmpDir = mkdtempSync(path.join(os.tmpdir(), 'fish-cut-'))
  const queriesFile = path.join(tmpDir, 'queries.json')
  writeFileSync(queriesFile, JSON.stringify(queries, null, 2), 'utf8')

  let pyResult
  try {
    pyResult = runPython(queriesFile)
  } finally {
    if (!keepPath) rmSync(tmpDir, { recursive: true, force: true })
  }

  const diffs = []
  queries.forEach((q, i) => {
    const py = pyResult.tokens[i]
    const ts = lib.cutTokens(q)
    const same = py.length === ts.length && py.every((t, k) => t === ts[k])
    if (!same) {
      diffs.push({
        index: i,
        query: q,
        pythonCount: py.length,
        tsCount: ts.length,
        firstDiffAt: (() => {
          const n = Math.min(py.length, ts.length)
          for (let k = 0; k < n; k += 1) if (py[k] !== ts[k]) return k
          return n
        })(),
        pythonHead: py.slice(0, 12),
        tsHead: ts.slice(0, 12),
      })
    }
  })

  const report = {
    dbPath,
    pythonModule: pyResult.module,
    pythonVersion: pyResult.python,
    nodeVersion: process.version,
    queryCount: queries.length,
    diffCount: diffs.length,
    diffs: diffs.slice(0, 10),
    queriesFile: keepPath || '(已清理临时文件)',
  }

  if (keepPath) writeFileSync(keepPath, JSON.stringify(queries, null, 2), 'utf8')

  if (process.argv.includes('--json')) {
    process.stdout.write(JSON.stringify(report, null, 2))
  } else {
    console.log(`# probe-cut  queries=${report.queryCount} python=${report.pythonVersion} node=${report.nodeVersion}`)
    console.log(`# python module: ${report.pythonModule}`)
    if (diffs.length === 0) {
      console.log(`PASS  diff=0  50/50 条逐 token 相等`)
    } else {
      for (const d of report.diffs.slice(0, 5)) {
        console.log(`FAIL  #${d.index} ${JSON.stringify(d.query)}`)
        console.log(`      py(${d.pythonCount})=${JSON.stringify(d.pythonHead)}`)
        console.log(`      ts(${d.tsCount})=${JSON.stringify(d.tsHead)}`)
        console.log(`      首个差异位置=${d.firstDiffAt}`)
      }
      console.log(`FAIL  diff=${diffs.length}/${report.queryCount}`)
    }
  }
  process.exitCode = diffs.length === 0 ? 0 : 1
}

main().catch((error) => {
  console.error(`probe-cut 失败：${error.message}`)
  process.exitCode = 1
})
