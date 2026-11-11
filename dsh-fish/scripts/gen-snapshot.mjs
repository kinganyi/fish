#!/usr/bin/env node
/**
 * 生产库 → **冻结快照**（判据：`test/conformance/snapshot/` 下的 TS/Python 逐字节对拍）。
 *
 * ## 为什么要有"冻结"
 *
 * 生产库每天都在变（流水 / hook 写入）。拿活库做判据等于把"数据变了"当成"实现变了"；
 * 而只跑受控语料（`spec/schema.sql` + seed）又证明不了"生产规模下也等价"。
 * 快照是这两者之间的第三条路：**一次性冻结，之后两侧跑同一份字节**。
 *
 * ## 纪律
 *
 * 1. **源库只读**：`readOnly: true` + SQLite backup 通道读取（连 WAL 一起读到副本里）；
 * 2. **不覆盖已有快照**：除非显式 `--force`——快照是判据的基准，静默换掉它等于换判据；
 * 3. **可追溯**：`manifest.json` 记源库 sha256、快照 sha256、复制时刻、关键表行数。
 *    判据脚本读 manifest 校验快照未被改动（同 `spec` 指纹的作用）。
 *
 * ## 用法
 *
 * ```bash
 * node scripts/gen-snapshot.mjs                 # 用默认生产库（FISH_HOME 或包根兄弟 p0）
 * node scripts/gen-snapshot.mjs --force         # 覆盖已有快照（**会换掉判据基准**）
 * node scripts/gen-snapshot.mjs --source <db>   # 显式指定源库
 * ```
 */

import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { DatabaseSync, backup } from 'node:sqlite'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const packageRoot = path.resolve(here, '..')
const snapshotDir = path.join(packageRoot, 'test', 'conformance', 'snapshot')
const snapshotPath = path.join(snapshotDir, 'index.sqlite')
const manifestPath = path.join(snapshotDir, 'manifest.json')

/** 关键表行数（记进 manifest：快照若被换过，这些数字会一起变，比单个 sha 更可读）。 */
const COUNTED_TABLES = [
  'code_index',
  'citizens',
  'boards',
  'presence',
  'board_posts',
  'push_log',
  'links',
  'tool_activity',
  'memory_access_log',
  'kb_vault',
]

function pickFlag(name) {
  const index = process.argv.indexOf(name)
  return index > 0 ? process.argv[index + 1] : null
}

/** 默认源库：`FISH_HOME/data/index.sqlite`，否则包根兄弟 `p0/data/index.sqlite`。 */
function defaultSource() {
  const home = (process.env.FISH_HOME ?? '').trim()
  const candidates = []
  if (home !== '') candidates.push(path.join(home, 'data', 'index.sqlite'))
  candidates.push(path.resolve(packageRoot, '..', 'p0', 'data', 'index.sqlite'))
  return candidates.find((candidate) => existsSync(candidate)) ?? candidates[candidates.length - 1]
}

function sha256File(file) {
  return createHash('sha256').update(readFileSync(file)).digest('hex')
}

const force = process.argv.includes('--force')
const source = path.resolve(pickFlag('--source') ?? defaultSource())

if (!existsSync(source)) {
  console.error(`源库不存在：${source}\n（用 --source 指定，或设置 FISH_HOME）`)
  process.exit(1)
}
if (existsSync(snapshotPath) && !force) {
  console.error(
    `快照已存在：${snapshotPath}\n` +
      '快照是判据的**基准**，不静默替换。确实要换基准请加 --force，并在提交信息里写清为什么换。',
  )
  process.exit(1)
}

mkdirSync(snapshotDir, { recursive: true })
// 暂存目录必须与目标**同一个卷**：`rename` 跨卷会 EXDEV（实测：临时目录在 C:、仓库在 I:）。
// 用同目录下的临时文件，最后一步原子改名，避免"拷贝到一半的快照被当成判据基准"。
const stagingDb = path.join(snapshotDir, `.staging-index-${process.pid}.sqlite`)

const src = new DatabaseSync(source, { readOnly: true })
try {
  await backup(src, stagingDb)
} finally {
  src.close()
}

// 冻结后重算行数（读副本，不读源库：副本才是将来判据真正跑的那一份）。
const copy = new DatabaseSync(stagingDb, { readOnly: true })
const counts = {}
try {
  for (const table of COUNTED_TABLES) {
    try {
      const row = copy.prepare(`SELECT COUNT(*) AS n FROM ${table}`).get()
      counts[table] = Number(row?.n ?? 0)
    } catch {
      counts[table] = -1 // 表不存在（老库/未迁移）：-1 是"没有这张表"，不是 0 行
    }
  }
} finally {
  copy.close()
}

renameSync(stagingDb, snapshotPath)
// 暂存副本可能带出来的 `-wal` / `-shm` 一并清掉（成品快照只该有一个文件）。
for (const suffix of ['-wal', '-shm']) {
  const leftover = `${stagingDb}${suffix}`
  if (existsSync(leftover)) rmSync(leftover, { force: true })
}

const snapshotSha = sha256File(snapshotPath)
const sourceStat = statSync(source)
const manifest = {
  schemaVersion: 1,
  purpose: '冻结的生产库快照：TS / Python 两侧在**同一份字节**上做逐字节对拍（判据 DIFF=0）',
  copiedAt: `${new Date().toISOString().slice(0, 19)}Z`,
  source: {
    path: source,
    bytes: sourceStat.size,
    mtime: new Date(sourceStat.mtimeMs).toISOString(),
    sha256: sha256File(source),
  },
  snapshot: {
    path: 'test/conformance/snapshot/index.sqlite',
    bytes: statSync(snapshotPath).size,
    sha256: snapshotSha,
  },
  rowCounts: counts,
  generator: 'scripts/gen-snapshot.mjs',
  notes: [
    '快照一旦冻结**不再更新**：它是判据基准（换它 = 换判据，必须走 --force 并在提交信息里说明）。',
    '两侧跑的是快照的**副本**（SQLite backup 到临时目录），所以 Python 侧的写路径（board_seen/ensure_fts）不会污染快照。',
    '时刻敏感段（看板"实际在动"、推送的 10/30 分钟窗口、presence 四档状态）在快照上是"跑的那一刻"算的——',
    '两侧必须**背靠背**跑（同一轮 run.mjs → run.py），跨窗口边界的 flake 属于判据本身的已知边界，见 README。',
  ],
}
writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, 'utf8')

console.log(`# gen-snapshot → ${snapshotPath}`)
console.log(`  源库      ${source}`)
console.log(`  源库 sha  ${manifest.source.sha256.slice(0, 16)}…  ${manifest.source.bytes} B`)
console.log(`  快照 sha  ${snapshotSha}  ${manifest.snapshot.bytes} B`)
console.log(`  复制时刻  ${manifest.copiedAt}`)
console.log(`  行数      ${Object.entries(counts).map(([table, n]) => `${table}=${n}`).join(' ')}`)
console.log(`  清单      ${manifestPath}`)
