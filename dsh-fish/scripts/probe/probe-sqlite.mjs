#!/usr/bin/env node
/**
 * 先验假设 #1：`node:sqlite` 在宿主运行时可用、能只读打开生产库、FTS5 `MATCH` 可用。
 *
 * 判据（全部必须通过，任一失败退出码 1）：
 *   A. `import('node:sqlite')` 成功，且导出 `DatabaseSync`。
 *   B. `mode=ro` 能打开 `p0/data/index.sqlite`，`sqlite_master` 可读且表数 > 0。
 *   C. 只读连接上的写操作必须被拒绝（证明"只读铁律"不是靠自觉）。
 *   D. `memory_fts` 上能用**生产实现的切词 + MATCH 表达式**命中真实行
 *      （表达式来自 `lib/`，本脚本不另写一份——两份切词必然漂）。
 *
 * 用法：node scripts/probe/probe-sqlite.mjs [--json]
 * 环境：FISH_DB 覆盖库路径（默认 ../p0/data/index.sqlite，相对 dsh-fish/）。
 */

import { existsSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(here, '..', '..')
const libPath = path.join(repoRoot, 'lib', 'index.js')
const dbPath = process.env.FISH_DB
  ? path.resolve(process.env.FISH_DB)
  : path.resolve(repoRoot, '..', 'p0', 'data', 'index.sqlite')

/** 结果收集；最后统一打印并决定退出码。 */
const checks = []
const record = (id, ok, detail) => {
  checks.push({ id, ok, detail })
  return ok
}

async function main() {
  // A. 模块可用性
  let sqlite
  try {
    sqlite = await import('node:sqlite')
  } catch (err) {
    record('A.module', false, `import('node:sqlite') 失败: ${err.message}`)
    return finish()
  }
  const hasDb = typeof sqlite.DatabaseSync === 'function'
  record('A.module', hasDb, `exports=${Object.keys(sqlite).join(',')} node=${process.version}`)

  if (!existsSync(libPath)) {
    record('A.lib', false, `找不到 ${libPath}：先跑 pnpm run build（D 项要用生产实现的切词）`)
    return finish()
  }
  const lib = await import(pathToFileURL(libPath).href)
  record('A.lib', true, `lib=${libPath}`)

  // B. 只读打开 + sqlite_master
  let db
  try {
    db = new sqlite.DatabaseSync(dbPath, { readOnly: true })
  } catch (err) {
    record('B.open', false, `只读打开失败(${dbPath}): ${err.message}`)
    return finish()
  }
  try {
    const rows = db
      .prepare('SELECT type, COUNT(*) AS n FROM sqlite_master GROUP BY type ORDER BY type')
      .all()
    const objectCounts = Object.fromEntries(rows.map((r) => [r.type, r.n]))
    record('B.master', true, `objects=${JSON.stringify(objectCounts)}`)
  } catch (err) {
    record('B.master', false, `sqlite_master 读取失败: ${err.message}`)
  }

  // B2. FTS 定义必须在（影子期只读校验依赖它）
  try {
    const fts = db.prepare("SELECT sql FROM sqlite_master WHERE name='memory_fts'").get()
    record('B.fts_def', Boolean(fts && fts.sql), fts ? fts.sql.replace(/\s+/g, ' ').trim() : 'memory_fts 不存在')
  } catch (err) {
    record('B.fts_def', false, `memory_fts 定义读取失败: ${err.message}`)
  }

  // C. 只读连接上的写必须抛错（SQLITE_READONLY）
  try {
    db.exec('CREATE TABLE __probe_should_fail (x INTEGER)')
    record('C.readonly_enforced', false, '只读连接竟然允许 CREATE TABLE')
  } catch (err) {
    const msg = String(err.message || err)
    record('C.readonly_enforced', /readonly|read-only/i.test(msg), `写入被拒绝: ${msg}`)
  }

  // D. FTS5 MATCH：用生产实现的切词 + MATCH 表达式查询真实行
  try {
    const sample = db
      .prepare("SELECT code, title FROM code_index WHERE status='active' ORDER BY code DESC LIMIT 1")
      .get()
    const expression = lib.ftsMatchExpression(String((sample && sample.title) || ''))
    if (expression === null) throw new Error('切词后没有可查 token（标题为空？）')
    const ftsCount = db.prepare('SELECT COUNT(*) AS n FROM memory_fts').get().n
    const hits = db.prepare('SELECT code FROM memory_fts WHERE memory_fts MATCH ? LIMIT 5').all(expression)
    record(
      'D.fts_match',
      hits.length > 0,
      `expr=${JSON.stringify(expression)} fts_rows=${ftsCount} hits=${hits.map((h) => h.code).join('|')}`,
    )
  } catch (err) {
    record('D.fts_match', false, `FTS5 MATCH 失败: ${err.message}`)
  }

  db.close()
  return finish()
}

function finish() {
  const failed = checks.filter((c) => !c.ok)
  if (process.argv.includes('--json')) {
    process.stdout.write(JSON.stringify({ dbPath, node: process.version, pass: failed.length === 0, checks }, null, 2))
  } else {
    console.log(`# probe-sqlite  db=${dbPath}  node=${process.version}`)
    for (const c of checks) console.log(`${c.ok ? 'PASS' : 'FAIL'}  ${c.id}  ${c.detail}`)
    console.log(`# summary: ${checks.length - failed.length}/${checks.length} pass`)
  }
  process.exitCode = failed.length === 0 ? 0 : 1
}

main().catch((err) => {
  record('X.unexpected', false, `未捕获异常: ${err && err.stack ? err.stack : err}`)
  finish()
})
