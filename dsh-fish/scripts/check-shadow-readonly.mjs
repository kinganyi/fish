#!/usr/bin/env node
/**
 * 判据⑤：影子期「生产库零写入」的可执行证明。
 *
 * 两段证据（缺一段都不成立）：
 *
 *   A. **严格对照**（副本上）：把生产库用 SQLite backup 通道复制到临时目录，在副本上依次跑
 *      `/fish shadow health|inject|recall|whoami`，比对副本 `index.sqlite` / `-wal` / `-shm`
 *      三件套的 `size + mtime + sha256`。副本上没有别的写者，所以"没变"能唯一归因于**我们**。
 *
 *   B. **生产现场观察**：在真库上跑同样四条命令，记录运行前后的 `size + mtime`（sha256 在
 *      有活跃写者时读不到，如实降级并注明）。生产库**同一时刻还有其它会话的 fish 进程在写**
 *      （hooks / MCP），所以这一段是**观察**，不是判决——判决在 A。
 *
 *   两段都要求：命令本身抛错即失败（读不出结果 = 没证明）。
 *
 * 用法：node scripts/check-shadow-readonly.mjs [--db <生产库>]
 */

import { createHash } from 'node:crypto'
import { existsSync, mkdtempSync, readFileSync, rmSync, statSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { DatabaseSync, backup } from 'node:sqlite'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const packageRoot = path.resolve(here, '..')
const libPath = path.join(packageRoot, 'lib', 'index.js')
/**
 * 生产库位置：`--db` > `FISH_DB` > `FISH_HOME/data/index.sqlite` > 包根兄弟 `p0/data/index.sqlite`。
 *
 * 为什么补 `FISH_HOME`：`gen-snapshot` / `gen-spec` / `check-spec` 都用它定位生产树。
 * 只认 `FISH_DB` 的话，同一条判据在不同脚本里要对同一个库写两种环境变量——
 * 而"忘了设其中一个"的表现是**检查跑在别的库上**（假绿），不是报错。
 */
const envHome = (process.env.FISH_HOME ?? '').trim()
const defaultDb =
  process.env.FISH_DB ??
  (envHome !== ''
    ? path.join(envHome, 'data', 'index.sqlite')
    : path.resolve(packageRoot, '..', 'p0', 'data', 'index.sqlite'))

const dbArgIndex = process.argv.indexOf('--db')
const productionDb = dbArgIndex > 0 ? path.resolve(process.argv[dbArgIndex + 1]) : defaultDb

if (!existsSync(libPath)) {
  console.error(`找不到 ${libPath}：先跑 pnpm run build`)
  process.exit(1)
}
if (!existsSync(productionDb)) {
  console.error(`找不到生产库：${productionDb}（用 --db 指定，或设 FISH_DB / FISH_HOME）`)
  process.exit(1)
}

const lib = await import(pathToFileURL(libPath).href)

/** 一个文件的三项读数；文件不存在返回 `null`（`-wal` 不存在是正常态）。 */
function fingerprint(filePath) {
  if (!existsSync(filePath)) return null
  let stat
  try {
    stat = statSync(filePath)
  } catch (error) {
    return { unavailable: String(error.message) }
  }
  let sha256 = null
  try {
    sha256 = createHash('sha256').update(readFileSync(filePath)).digest('hex')
  } catch {
    // 有活跃写者时可能读不到：如实降级，不拿 size/mtime 冒充 sha256。
    sha256 = null
  }
  return { size: stat.size, mtimeMs: stat.mtimeMs, sha256 }
}

/** 三件套指纹（`base` 可换成 `corpus.sqlite`：语料库是独立库，判据要一起看）。 */
function bundle(dir, base = 'index.sqlite') {
  const target = path.join(dir, base)
  return { db: fingerprint(target), wal: fingerprint(`${target}-wal`), shm: fingerprint(`${target}-shm`) }
}

/**
 * 比对两份指纹，返回差异描述（空数组 = 完全一致）。
 *
 * 两条**已知的 SQLite 行为**必须写在这儿，否则会得出"只读打开也写了库"的假红：
 *   · 只读连接打开 WAL 模式的库时，若 `-wal` 不存在，SQLite 会建一个**空** `-wal`
 *     （内容 0 字节 = 谁都没写；这份文件本身是协调用的）；
 *   · `-shm` 是共享内存索引文件，新建时固定 32 KiB —— 它**不含任何数据页**，
 *     大小恒定、内容随连接生命周期变化，不能当"写入"的证据。
 * 所以判据是：主库 `size/mtime/sha256` 全等；原存的 `-wal` 内容全等；新建的 `-wal` 必须 0 字节。
 */
function diffBundle(before, after) {
  const diffs = []
  for (const key of ['db', 'wal', 'shm']) {
    const left = before[key]
    const right = after[key]
    if (left === null && right === null) continue
    if (left === null && right !== null) {
      if (key === 'shm') continue // SQLite WAL 协调文件（固定 32 KiB，无数据页）
      if (key === 'wal' && right.size === 0) continue // 空 -wal = 无人写
      diffs.push(`${key}: (不存在) → (存在，size=${right.size ?? '?'})`)
      continue
    }
    if (left !== null && right === null) {
      diffs.push(`${key}: (存在) → (不存在)`)
      continue
    }
    if (left.unavailable !== undefined || right.unavailable !== undefined) {
      diffs.push(`${key}: 不可读（跳过比对）`)
      continue
    }
    if (left.size !== right.size) diffs.push(`${key}.size ${left.size} → ${right.size}`)
    if (key === 'db' && left.mtimeMs !== right.mtimeMs) diffs.push(`${key}.mtime ${left.mtimeMs} → ${right.mtimeMs}`)
    if (left.sha256 !== null && right.sha256 !== null && left.sha256 !== right.sha256) {
      diffs.push(`${key}.sha256 ${left.sha256.slice(0, 12)} → ${right.sha256.slice(0, 12)}`)
    }
  }
  return diffs
}

/** 依次跑一组只读命令，返回每条的输出行数与首行（证明"真的执行了"）。 */
function runCommands(paths, log, commands = DEFAULT_COMMANDS) {
  const runtime = lib.prepareShadow({ mode: 'shadow', fishHome: paths.fishHome, dbPath: paths.dbPath })
  const deps = {
    paths: runtime.paths,
    spec: runtime.spec,
    gate: runtime.gate,
    logger: runtime.logger ?? lib.SILENT_LOGGER,
    config: { mode: runtime.config.mode, budgetTokens: runtime.config.budgetTokens, commandEnabled: true },
  }
  const readings = []
  for (const input of commands) {
    const result = lib.handleFishCommand(input, deps)
    const text = typeof result.text === 'string' ? result.text : ''
    const lines = text.split('\n')
    if (result.kind !== 'success') {
      throw new Error(`命令失败：/fish ${input} → ${text.slice(0, 200)}`)
    }
    log(`  · /fish ${input} → ${lines.length} 行；首行：${lines[0]?.slice(0, 60) ?? ''}`)
    readings.push({ input, lines: lines.length })
  }
  return readings
}

console.log(`# check-shadow-readonly  db=${productionDb}  node=${process.version}`)

/**
 * 主库只读命令（T01–T03 的能力）。
 *
 * T05 之后追加的那几条（`kb vault list` / `alias list` / `workspace list` / `project status`）
 * 都只读**主库**、不碰文件树，所以能放进严格对照段；`kb tree/open/search` 与 `kb stats`
 * 需要 fish-home 的文件树（生产 vault 可能在别的盘、可能离线），它们的零写入由
 * `check-cli-surface` 的夹具家目录段负责——那一段是**同一条纪律的另一个判据点**，
 * 不是"没测"。
 */
const DEFAULT_COMMANDS = [
  'shadow health',
  'shadow inject --budget 300',
  'shadow recall 协作 --limit 3',
  'shadow whoami',
  'kb vault list',
  'alias list',
  'workspace list',
  'project status',
]

/** 语料库只读命令（T05）：库是独立的 `corpus.sqlite`，指针从副本里现取。 */
function corpusCommands(corpusDir) {
  const commands = ['corpus stats', 'corpus search 协作 --limit 3']
  const dbPath = path.join(corpusDir, 'corpus.sqlite')
  if (!existsSync(dbPath)) return commands
  try {
    const db = new DatabaseSync(dbPath, { readOnly: true })
    try {
      const row = db
        .prepare("SELECT host, session_id, loc, part FROM corpus_segment WHERE loc LIKE 'seq=%' ORDER BY id LIMIT 1")
        .get()
      if (row !== undefined) {
        const pointer = `log:${row.host}:${row.session_id}#${row.loc}${row.part === 0 ? '' : `.${row.part}`}`
        commands.push(`corpus read ${pointer} --max-chars 200`)
      }
    } finally {
      db.close()
    }
  } catch {
    // 语料库打不开：只跑前两条（stats 会给出 db_missing 读数，仍是"执行过"）。
  }
  return commands
}

// ---- A. 严格对照：副本上跑，主库三件套 + 语料库三件套都必须逐字节不变 ----
const tmp = mkdtempSync(path.join(os.tmpdir(), 'fish-ro-'))
let passStrict = false
try {
  const copyDir = path.join(tmp, 'data')
  const { mkdirSync, copyFileSync } = await import('node:fs')
  mkdirSync(copyDir, { recursive: true })
  const copyPath = path.join(copyDir, 'index.sqlite')
  const src = new DatabaseSync(productionDb, { readOnly: true })
  try {
    await backup(src, copyPath)
  } finally {
    src.close()
  }
  // 语料库是**独立库**（`<data>/corpus.sqlite`）：它是 T05 新读面的数据源，
  // 「读语料不写库」必须有自己的读数，不能靠主库那三件套"顺带代表"。
  const productionCorpus = path.join(path.dirname(productionDb), 'corpus.sqlite')
  let corpusCopied = false
  if (existsSync(productionCorpus)) {
    copyFileSync(productionCorpus, path.join(copyDir, 'corpus.sqlite'))
    corpusCopied = true
    for (const suffix of ['-wal', '-shm']) {
      if (existsSync(`${productionCorpus}${suffix}`)) copyFileSync(`${productionCorpus}${suffix}`, path.join(copyDir, `corpus.sqlite${suffix}`))
    }
  }
  console.log(`# A. 副本对照：${copyPath}${corpusCopied ? `（含 corpus.sqlite，${statSync(productionCorpus).size} B）` : '（生产无 corpus.sqlite）'}`)
  const before = bundle(copyDir)
  const beforeCorpus = corpusCopied ? bundle(copyDir, 'corpus.sqlite') : null
  // fishHome 指**副本根**（`<tmp>`），于是 `paths.dataDir = <tmp>/data` —— 语料库与主库
  // 都在同一个副本目录里。指 `p0` 会让语料命令去读生产目录（那正是本判据要防的"跑在别的库上"）。
  const copyHome = tmp
  const readings = runCommands({ fishHome: copyHome, dbPath: copyPath }, console.log)
  const corpusReadings = corpusCopied ? runCommands({ fishHome: copyHome, dbPath: copyPath }, console.log, corpusCommands(copyDir)) : []
  const after = bundle(copyDir)
  const afterCorpus = corpusCopied ? bundle(copyDir, 'corpus.sqlite') : null
  const diffs = diffBundle(before, after)
  const corpusDiffs = beforeCorpus === null || afterCorpus === null ? [] : diffBundle(beforeCorpus, afterCorpus)
  passStrict = diffs.length === 0 && corpusDiffs.length === 0 && readings.length === DEFAULT_COMMANDS.length
  console.log(
    `${passStrict ? 'PASS' : 'FAIL'}  A.zero_write_on_copy  差异=${diffs.length}${diffs.length > 0 ? ` → ${diffs.join('; ')}` : ''}` +
      `  sha256.db=${after.db?.sha256?.slice(0, 12) ?? 'n/a'}  wal=${after.wal === null ? '(无 -wal)' : (after.wal.sha256?.slice(0, 12) ?? 'n/a')}`,
  )
  console.log(
    `${corpusDiffs.length === 0 ? 'PASS' : 'FAIL'}  A.zero_write_on_corpus  语料命令 ${corpusReadings.length} 条` +
      `  差异=${corpusDiffs.length}${corpusDiffs.length > 0 ? ` → ${corpusDiffs.join('; ')}` : ''}` +
      `  sha256.corpus=${afterCorpus?.db?.sha256?.slice(0, 12) ?? '（无语料库）'}`,
  )
  if (corpusDiffs.length > 0) passStrict = false
} catch (error) {
  console.log(`FAIL  A.zero_write_on_copy  ${error instanceof Error ? error.message : String(error)}`)
} finally {
  rmSync(tmp, { recursive: true, force: true })
}

// ---- B. 生产现场观察：只记录，不判决（同期有别的写者）----
const prodDir = path.dirname(productionDb)
console.log('# B. 生产现场观察（其它会话的 fish 进程可能同时在写，故这一段只作观察）')
const beforeProd = bundle(prodDir)
try {
  runCommands({ fishHome: prodDir.replace(/[\\/]data$/, ''), dbPath: productionDb }, console.log)
  const afterProd = bundle(prodDir)
  const diffs = diffBundle(beforeProd, afterProd)
  const note =
    diffs.length === 0
      ? '运行前后三件套无变化'
      : `运行前后有 ${diffs.length} 处变化（含其它写者，不可单独归因）：${diffs.join('; ')}`
  console.log(`INFO  B.production_observation  ${note}`)
  console.log(
    `      before: db(size=${beforeProd.db?.size}, sha=${beforeProd.db?.sha256?.slice(0, 12) ?? '读不到'}) ` +
      `wal(mtime=${beforeProd.wal?.mtimeMs ?? 'n/a'}, size=${beforeProd.wal?.size ?? 'n/a'})`,
  )
  console.log(
    `      after : db(size=${afterProd.db?.size}, sha=${afterProd.db?.sha256?.slice(0, 12) ?? '读不到'}) ` +
      `wal(mtime=${afterProd.wal?.mtimeMs ?? 'n/a'}, size=${afterProd.wal?.size ?? 'n/a'})`,
  )
} catch (error) {
  console.log(`FAIL  B.production_observation  ${error instanceof Error ? error.message : String(error)}`)
  passStrict = false
}

console.log(`# summary: 严格对照 ${passStrict ? 'PASS' : 'FAIL'}（判决在 A；B 只作现场观察）`)
process.exitCode = passStrict ? 0 : 1
