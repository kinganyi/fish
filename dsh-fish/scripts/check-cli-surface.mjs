#!/usr/bin/env node
/**
 * 判据④：**写命令在影子期被拒**（以及只读命令真的跑得通）。
 *
 * ## 为什么这条判据必须"跑"而不是"读"
 *
 * `spec/cli-map.json` 上写着 `impl=unsupported-shadow` 只证明**声明**是这样；
 * 声明与行为之间还有一整段代码（路由 → 裁决 → 渲染）。真正要证的是三件事：
 *
 *   1. **拒绝得响亮**：`kind === 'error'`，文案里同时出现"影子期不执行写操作"与替代命令；
 *   2. **没有偷偷执行**：整轮跑完，**库副本字节不变**（Python 一次都没被启动过）；
 *   3. **只读的那一档真的能用**（`ts` 与 `proxy-readonly` 都产出 `success`）——
 *      否则"写命令被拒"可以靠"所有命令都拒"来实现，那是另一种坏。
 *
 * 库副本做法与 `check-shadow-readonly.mjs` 同源：复制生产库 → 在副本上跑命令 →
 * 比对 `index.sqlite / -wal / -shm` 三件套。副本上没有别的写者，所以"没变"
 * 能唯一归因于**命令面**。
 *
 * 用法：`FISH_HOME=… node scripts/check-cli-surface.mjs [--db <生产库>]`
 */

import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { DatabaseSync, backup } from 'node:sqlite'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const packageRoot = path.resolve(here, '..')
const libPath = path.join(packageRoot, 'lib', 'index.js')
const envHome = (process.env.FISH_HOME ?? '').trim()
const fishHome = envHome !== '' ? path.resolve(envHome) : path.resolve(packageRoot, '..', 'p0')
const defaultDb = process.env.FISH_DB ?? path.join(fishHome, 'data', 'index.sqlite')
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
const checks = []
const record = (id, ok, detail) => {
  checks.push({ id, ok, detail })
  return ok
}

/** 一个文件的三项读数（不存在返回 `null`）。 */
function fingerprint(filePath) {
  if (!existsSync(filePath)) return null
  const stat = statSync(filePath)
  let sha256 = null
  try {
    sha256 = createHash('sha256').update(readFileSync(filePath)).digest('hex')
  } catch {
    sha256 = null
  }
  return { size: stat.size, mtimeMs: stat.mtimeMs, sha256 }
}

/** 三件套（判定口径与 check-shadow-readonly 一致）。 */
function bundle(dir) {
  const base = path.join(dir, 'index.sqlite')
  return { db: fingerprint(base), wal: fingerprint(`${base}-wal`), shm: fingerprint(`${base}-shm`) }
}

/** 比对两份三件套指纹（空数组 = 一致）。 */
function diffBundle(before, after) {
  const diffs = []
  for (const key of ['db', 'wal', 'shm']) {
    const left = before[key]
    const right = after[key]
    if (left === null && right === null) continue
    if (left === null && right !== null) {
      if (key === 'shm') continue
      if (key === 'wal' && right.size === 0) continue
      diffs.push(`${key}: (不存在) → (存在，size=${right.size})`)
      continue
    }
    if (left !== null && right === null) {
      diffs.push(`${key}: (存在) → (不存在)`)
      continue
    }
    if (left.sha256 !== null && right.sha256 !== null && left.sha256 !== right.sha256) {
      diffs.push(`${key}.sha256 ${left.sha256.slice(0, 12)} → ${right.sha256.slice(0, 12)}`)
    } else if (key !== 'db' && left.size !== right.size) {
      diffs.push(`${key}.size ${left.size} → ${right.size}`)
    } else if (key === 'db' && (left.size !== right.size || left.mtimeMs !== right.mtimeMs)) {
      diffs.push(`db.size/mtime 变化（${left.size}/${left.mtimeMs} → ${right.size}/${right.mtimeMs}）`)
    }
  }
  return diffs
}

let tmpRoot = null
let fixtureHome = null

/**
 * ts 命令的**样例入参**：有几条只读命令必须给位置参数才跑得动（`kb open <path>`）。
 *
 * 为什么写在脚本里而不是从 spec 读：spec 记的是"命令树长什么样"，不记"怎么调一次"。
 * 把样例与夹具（`buildFixtureHome`）写在同一处，才能保证"给的路径真的存在"——
 * 两边分开写，样例早晚会指向一个夹具里没有的文件，那时红的是判据而不是实现。
 */
const TS_SAMPLE_ARGS = {
  recall: 'recall 协作 --limit 2',
  inject: 'inject --budget 200',
  'kb open': 'kb open 10-漫剧/示例.md',
  'kb search': 'kb search 协作',
  'corpus search': 'corpus search 协作',
  'corpus read': 'corpus read log:dsh:session-fixture0001#seq=1',
}

/**
 * 造一个**夹具家目录**：T05 的只读命令需要"真实的知识库文件 + 语料库"才跑得出内容。
 *
 * 为什么不能拿生产库副本顶替：`kb *` 读的是 fish-home 的文件树、`corpus *` 读的是独立库
 * `corpus.sqlite`——两者都不在主库副本里。用副本跑它们得到的只会是"没有默认知识库"，
 * 那是**环境缺件**而不是命令不会跑；把这两件事混起来，`ts_commands_run` 就变成了
 * "只要命令会报错就绿"的假判据。
 *
 * 夹具自己建在临时目录里，主库只用 `spec/schema.sql` / `spec/corpus.schema.sql` 建空库
 * ——生产数据一个字节都不经过这里。
 */
function buildFixtureHome(root) {
  const home = path.join(root, 'fixture-home')
  const dataDir = path.join(home, 'data')
  mkdirSync(dataDir, { recursive: true })
  const dbPath = path.join(dataDir, 'index.sqlite')
  const vaultDir = path.join(home, 'vault')
  mkdirSync(path.join(vaultDir, '10-漫剧'), { recursive: true })
  writeFileSync(path.join(vaultDir, '10-漫剧', '示例.md'), '# 示例\n\n协作约定：先对齐再动手。\n', 'utf8')
  const db = new DatabaseSync(dbPath)
  try {
    db.exec(readFileSync(path.join(packageRoot, 'spec', 'schema.sql'), 'utf8'))
    db.prepare(
      'INSERT INTO kb_vault (vault_id,name,path,path_key,mode,ignore_globs,max_open_bytes,is_default,' +
        'status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
    ).run('01FIXTUREVAULT00000000000A', '夹具库', vaultDir, vaultDir.toLowerCase(), 'ro', '[]', 2097152, 1, 'active', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')
    db.prepare("INSERT INTO code_registry (prefix,namespace,status) VALUES ('FIXTURE','夹具项目','active')").run()
    db.prepare("INSERT INTO name_aliases (kind,from_name,to_name,created_at) VALUES ('project','fixture','FIXTURE','2026-10-01T00:00:00Z')").run()
    db.prepare("INSERT INTO workspace_map (path_prefix,project,note,created_at) VALUES ('i:/app/fixture','FIXTURE',NULL,'2026-10-01T00:00:00Z')").run()
    db.prepare(
      "INSERT INTO code_index (code,title,project,owner,mem_type,status,recorded_at,dormant) VALUES ('FIXTURE-261001-01','协作约定落地','FIXTURE','unknown','fact','active','2026-10-01T02:00:00Z',0)",
    ).run()
    // 全文索引也要有一条：`recall` 有一条**真实护栏**——"索引为空而记忆非空"会响亮拒绝
    // （那正是"存进去但搜不到"的失真）。夹具不种 FTS，`recall` 就会红在这条护栏上，
    // 而红的原因与命令能不能跑无关。
    db.prepare('INSERT INTO memory_fts (code, title_idx, body_idx) VALUES (?,?,?)').run(
      'FIXTURE-261001-01',
      lib.cutText('FIXTURE-261001-01 协作约定落地'),
      lib.cutText('协作约定：先对齐再动手。'),
    )
  } finally {
    db.close()
  }
  const corpusDb = new DatabaseSync(path.join(dataDir, 'corpus.sqlite'))
  try {
    corpusDb.exec(readFileSync(path.join(packageRoot, 'spec', 'corpus.schema.sql'), 'utf8'))
    corpusDb
      .prepare(
        "INSERT INTO corpus_session (host,session_id,sid8,title,workspace,project,project_source,origin,segments) VALUES ('dsh','session-fixture0001','fixture0','夹具会话','','FIXTURE','cwd','main',1)",
      )
      .run()
    corpusDb
      .prepare(
        "INSERT INTO corpus_source (host,session_id,path,format,bytes_total,mtime_ns,events_seen,segments_kept,status,first_ingested_at,last_ingested_at) VALUES ('dsh','session-fixture0001','C:\\logs\\fixture.jsonl','dsh.v4',128,0,2,1,'ok','2026-10-26T00:00:00Z','2026-10-26T00:00:00Z')",
      )
      .run()
    corpusDb
      .prepare(
        "INSERT INTO corpus_segment (id,host,session_id,loc,part,role,occurred_at,text,chars,redacted_count,raw_sha256,byte_from,byte_to,offset_space,shadowed,ingested_at) VALUES (1,'dsh','session-fixture0001','seq=1',0,'user','2026-10-26T00:00:00Z','夹具会话里的协作约定。',12,0,'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',0,64,'byte',0,'2026-10-26T00:00:00Z')",
      )
      .run()
    corpusDb.prepare('INSERT INTO corpus_fts (seg_id, text_idx) VALUES (1, ?)').run(lib.cutText('夹具会话里的协作约定。'))
  } finally {
    corpusDb.close()
  }
  return home
}

try {
  const cliMap = lib.loadSpec(lib.resolvePaths({ fishHome: fishHome, dbPath: productionDb }).specDir).cliMap
  const deps = (dbPath, homeOverride = fishHome) => {
    const runtime = lib.prepareShadow({ mode: 'shadow', fishHome: homeOverride, dbPath, logLevel: 'warn' })
    return {
      paths: runtime.paths,
      spec: runtime.spec,
      gate: runtime.gate,
      logger: lib.SILENT_LOGGER,
      config: { mode: runtime.config.mode, budgetTokens: runtime.config.budgetTokens, commandEnabled: true },
    }
  }

  // ---- A. 拒绝面：所有 unsupported-shadow / python-only 都必须响亮拒绝 + 给替代 ----
  tmpRoot = mkdtempSync(path.join(os.tmpdir(), 'fish-cli-surface-'))
  const copyDir = path.join(tmpRoot, 'data')
  mkdirSync(copyDir, { recursive: true })
  const copyDb = path.join(copyDir, 'index.sqlite')
  {
    const source = new DatabaseSync(productionDb, { readOnly: true })
    try {
      await backup(source, copyDb)
    } finally {
      source.close()
    }
  }
  const before = bundle(copyDir)
  const copyDeps = deps(copyDb)

  const refusing = cliMap.entries.filter((entry) => entry.impl === 'unsupported-shadow' || entry.impl === 'python-only')
  const refusalFailures = []
  for (const entry of refusing) {
    const result = lib.handleFishCommand(entry.path, copyDeps)
    const text = typeof result.text === 'string' ? result.text : ''
    const marker = entry.impl === 'unsupported-shadow' ? '影子期不执行写操作' : '影子期只给指引，不执行'
    const replacementCli = entry.replacement?.cli ?? ''
    const problems = []
    if (result.kind !== 'error') problems.push(`kind=${result.kind}（应为 error）`)
    if (!text.includes(marker)) problems.push(`缺标记「${marker}」`)
    if (replacementCli !== '' && !text.includes(`fish ${replacementCli}`)) problems.push(`缺替代命令 fish ${replacementCli}`)
    if (!text.includes('没有')) problems.push('缺"没有写入/没有启动 Python"的显式声明')
    if (problems.length > 0) refusalFailures.push(`${entry.path}: ${problems.join('；')}`)
  }
  record(
    'cli.write_commands_refused',
    refusalFailures.length === 0,
    refusalFailures.length === 0
      ? `${refusing.length} 条写/运维命令全部拒绝并给出替代（无例外）`
      : `${refusalFailures.length} 条不合格：${refusalFailures.slice(0, 6).join(' | ')}`,
  )

  // ---- B. 只读面：ts 档必须真的能跑出内容 ----
  //
  // 为什么这一段的 deps 换成**夹具家目录**而不是生产库副本：T05 的只读命令里有几条
  // 的依赖根本不在主库里——知识库读 fish-home 的文件树、语料读独立的 `corpus.sqlite`。
  // 拿生产库副本跑它们，得到的只会是"没有默认知识库 / 语料库不存在"，那是**环境缺件**
  // 而不是命令不会跑（把这两件事混在一起，判据就变成装饰品）。
  fixtureHome = buildFixtureHome(tmpRoot)
  const fixtureDb = path.join(fixtureHome, 'data', 'index.sqlite')
  const fixtureDeps = deps(fixtureDb, fixtureHome)
  const fixtureBefore = bundle(path.join(fixtureHome, 'data'))
  const corpusBefore = fingerprint(path.join(fixtureHome, 'data', 'corpus.sqlite'))
  const tsEntries = cliMap.entries.filter((entry) => entry.impl === 'ts')
  const tsFailures = []
  const tsOk = []
  for (const entry of tsEntries) {
    const args = TS_SAMPLE_ARGS[entry.path] ?? entry.path
    const result = lib.handleFishCommand(args, fixtureDeps)
    const text = typeof result.text === 'string' ? result.text : ''
    if (result.kind !== 'success' || text.trim() === '') tsFailures.push(`${entry.path}: kind=${result.kind} len=${text.length} head=${text.split('\n')[0]?.slice(0, 60) ?? ''}`)
    else tsOk.push(`${entry.path}(${text.split('\n').length}行)`)
  }
  record(
    'cli.ts_commands_run',
    tsFailures.length === 0 && tsEntries.length > 0,
    tsFailures.length === 0 ? `${tsEntries.length} 条 ts 命令全部产出内容：${tsOk.join(' ')}` : tsFailures.join(' | '),
  )

  // ---- B2. 只读面**零写入**：跑完一整轮 ts 命令，夹具家目录的三件套 + 语料库不变 ----
  const fixtureAfter = bundle(path.join(fixtureHome, 'data'))
  const corpusAfter = fingerprint(path.join(fixtureHome, 'data', 'corpus.sqlite'))
  const readOnlyDiffs = [
    ...diffBundle(fixtureBefore, fixtureAfter),
    ...(corpusBefore?.sha256 !== corpusAfter?.sha256 ? ['corpus.sqlite.sha256 变化'] : []),
    ...(corpusBefore?.size !== corpusAfter?.size ? ['corpus.sqlite.size 变化'] : []),
  ]
  record(
    'cli.ts_commands_zero_write',
    readOnlyDiffs.length === 0,
    readOnlyDiffs.length === 0
      ? `${tsEntries.length} 条 ts 命令跑完后夹具库三件套 + corpus.sqlite 逐字节不变（sha=${corpusAfter?.sha256?.slice(0, 12) ?? 'n/a'}）`
      : `只读命令写出了东西：${readOnlyDiffs.join('; ')}`,
  )

  // ---- C. proxy 档：抽两条实跑（快照隔离 + 真出结果）----
  const proxyEntries = cliMap.entries.filter((entry) => entry.impl === 'proxy-readonly')
  const proxySamples = ['events --limit 3', 'list'].filter((sample) => proxyEntries.some((entry) => sample.startsWith(entry.path)))
  const proxyFailures = []
  const proxyOk = []
  for (const sample of proxySamples) {
    const result = lib.handleFishCommand(sample, copyDeps)
    const text = typeof result.text === 'string' ? result.text : ''
    const okSuccess = result.kind === 'success' && text.includes('只读转发')
    if (!okSuccess) proxyFailures.push(`${sample}: kind=${result.kind} head=${text.split('\n')[0]?.slice(0, 80) ?? ''}`)
    else proxyOk.push(sample)
  }
  record(
    'cli.proxy_readonly_runs',
    proxyFailures.length === 0 && proxyOk.length > 0,
    proxyFailures.length === 0
      ? `${proxyOk.length} 条 proxy 实跑成功（快照转发；共登记 ${proxyEntries.length} 条）`
      : proxyFailures.join(' | '),
  )

  const after = bundle(copyDir)
  const diffs = diffBundle(before, after)
  record(
    'cli.copy_db_unchanged',
    diffs.length === 0,
    diffs.length === 0
      ? `副本三件套前后一致（${refusing.length} 条拒绝 + ${tsEntries.length} 条 ts + ${proxyOk.length} 条 proxy）；sha=${after.db?.sha256?.slice(0, 12) ?? 'n/a'}`
      : `副本被改动（写出了东西）：${diffs.join('; ')}`,
  )

  // ---- D. 命令面注册一致性：cli-map 的每条命令都能被 /fish 解析到 ----
  const surfaceFailures = []
  for (const entry of cliMap.entries) {
    const result = lib.handleFishCommand(`${entry.path} --help`, {})
    const text = typeof result.text === 'string' ? result.text : ''
    if (result.kind !== 'success' || !text.includes(entry.path)) {
      surfaceFailures.push(`${entry.path}: kind=${result.kind} head=${text.split('\n')[0]?.slice(0, 60) ?? ''}`)
    }
  }
  record(
    'cli.surface_registered',
    surfaceFailures.length === 0,
    surfaceFailures.length === 0
      ? `${cliMap.entries.length} 条命令全部能被 \`/fish <path> --help\` 解析（且 --help 不碰库/不需要运行时事实）`
      : `${surfaceFailures.length} 条解析不到：${surfaceFailures.slice(0, 6).join(' | ')}`,
  )
  const unknown = lib.handleFishCommand('definitely-not-a-command', {})
  record(
    'cli.surface_unknown_rejected',
    unknown.kind === 'error' && (unknown.text ?? '').includes('未知'),
    `未知命令 → kind=${unknown.kind}（首行：${(unknown.text ?? '').split('\n')[0]?.slice(0, 40) ?? ''}）`,
  )
} catch (error) {
  record('x.unexpected', false, error instanceof Error ? `${error.name}: ${error.message}` : String(error))
} finally {
  if (tmpRoot !== null) rmSync(tmpRoot, { recursive: true, force: true })
}

const failed = checks.filter((check) => !check.ok)
console.log('# check-cli-surface')
for (const check of checks) console.log(`${check.ok ? 'PASS' : 'FAIL'}  ${check.id}  ${check.detail}`)
console.log(`# summary: ${checks.length - failed.length}/${checks.length} pass`)
process.exitCode = failed.length === 0 ? 0 : 1
