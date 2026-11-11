/**
 * Python 借用层：**只读转发到 `p0/fish.py`，但把库换成一次性快照**。
 *
 * ## 为什么不能"直接转发"
 *
 * 生产树里**每一条** `cmd_*` 都是这个开头（`p0/fish.py:980` 的 `connect()`）：
 *
 * ```python
 * conn = connect(Path(args.root)); conn.executescript(SCHEMA); migrate(conn)
 * ```
 *
 * `connect()` 会 `mkdir` + `PRAGMA journal_mode=WAL`，`executescript(SCHEMA)` 会跑一遍
 * 建表脚本、`migrate(conn)` 可能补列。也就是说：**即使是一条纯读命令，Python 进程也
 * 会以读写方式打开生产库并跑迁移**。影子期"零写入"是硬约束，所以"只读转发"不能
 * 只靠命令分类来保证——必须让 Python **根本拿不到生产库的路径**。
 *
 * 做法：把 `index.sqlite` / `-wal` / `-shm` 三件套复制到临时目录（实测 15ms 量级），
 * 用 `--root <临时目录>` 启动 Python。于是：
 *   · 命令读到的数据 = 调用前那一刻的快照（`asOf` 会写进回显）；
 *   · 它的 SCHEMA/migrate/审计追加全部落在**这次调用独有的临时副本**上，退出即删；
 *   · 生产库从头到尾没有被任何写连接打开过（回显里给出前后 sha256 作为证据）。
 *
 * ## 边界
 *
 * 只给 `impl='proxy-readonly'` 且 `kind='read'` 的命令用（调用方断言）。
 * 快照里没有的东西（`corpus.sqlite`、`_audit/hooks/*.jsonl`、fish-home 文件树写入）
 * 一律不转发——那些命令在 `cli-map` 里是 `python-only`，理由写在表里。
 *
 * @module dsh-fish/ports/python
 */

import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { copyFileSync, existsSync, mkdtempSync, readFileSync, rmSync, statSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { DatabaseSync } from 'node:sqlite'

import { FishError } from '../core/errors.ts'

/** 默认超时：Python 进程冷启动 ~0.3s，命令本身再慢也不该超过 8s（超了就是卡住了）。 */
export const DEFAULT_PY_TIMEOUT_MS = 8000

/** 输出上限：命令面回显另有一层 60 行截断，这里防的是"一次跑出几百 MB"。 */
const MAX_BUFFER_BYTES = 4 * 1024 * 1024

/** 快照信息（回显里必须出现：读者要知道自己看的是哪一刻的数据）。 */
export interface SnapshotInfo {
  /** 临时目录（调用结束即删）。 */
  readonly dir: string
  /** 副本里的主库路径。 */
  readonly dbPath: string
  /** 复制了哪几个文件（`-wal` / `-shm` 可能不存在）。 */
  readonly files: readonly string[]
  /** 主库字节数。 */
  readonly bytes: number
  /** 复制时刻（ISO8601，本地时区）。 */
  readonly asOf: string
  /** 副本 `PRAGMA integrity_check` 的结果（`ok` 才算复制成功）。 */
  readonly integrity: string
}

/** 一次转发的完整读数。 */
export interface FishCliRunResult {
  readonly ok: boolean
  /** 实际启动的 argv（脱敏：不含凭证；用于回显"跑了什么"）。 */
  readonly argv: readonly string[]
  readonly exitCode: number | null
  readonly signal: string | null
  readonly stdout: string
  readonly stderr: string
  readonly timedOut: boolean
  readonly snapshot: SnapshotInfo | null
  /** 快照复制失败/命令失败时的原因（人读）。 */
  readonly failure: string | null
  /** 生产库 sha256 前 12 位（调用前 / 调用后）：两者相同 = 本次调用没碰生产库。 */
  readonly productionShaBefore: string | null
  readonly productionShaAfter: string | null
}

/** 转发选项。 */
export interface FishCliRunOptions {
  /** fish 家目录（含 `fish.py` 与 `data/index.sqlite`）。 */
  readonly fishHome: string
  /** 生产主库路径（只用于**复制**，不会传给 Python）。 */
  readonly dbPath: string
  /** 交给 `fish.py` 的参数（不含 `--root`，本模块自己加）。 */
  readonly args: readonly string[]
  /** 超时（毫秒）。 */
  readonly timeoutMs?: number
}

/** 取 Python 解释器：`FISH_PYTHON` 优先（与 gen-spec / 判据脚本同一口径）。 */
export function resolvePythonBin(): string {
  const raw = (process.env.FISH_PYTHON ?? '').trim()
  return raw === '' ? 'python' : raw
}

/** 生产树里的 `fish.py`（不存在时抛错，别让 spawn 去报一个晦涩的 ENOENT）。 */
export function resolveFishPy(fishHome: string): string {
  const candidate = path.join(path.resolve(fishHome), 'fish.py')
  if (!existsSync(candidate)) {
    throw new FishError('SHADOW_UNSUPPORTED', `找不到生产树入口：${candidate}`, {
      hint: '设置 fishHome 配置项 / FISH_HOME 指向含 fish.py 的生产树',
      details: { fishHome },
    })
  }
  return candidate
}

/** 文件 sha256 前 12 位；读不到返回 `null`（有活跃写者时可能读不到，如实降级）。 */
function sha12(filePath: string): string | null {
  try {
    return createHash('sha256').update(readFileSync(filePath)).digest('hex').slice(0, 12)
  } catch {
    return null
  }
}

/**
 * 复制一份只读快照到临时目录，并自证副本可用。
 *
 * 为什么是"复制三件套"而不是 `VACUUM INTO` / `backup()`：
 *   · `VACUUM INTO` 在 `readOnly` 连接上直接报 `attempt to write a readonly database`
 *     （实测：`PRAGMA query_only=1` 之后连 INTO 都不许）；
 *   · `node:sqlite` 的 `backup()` 是**异步**的，而斜杠命令的 handler 是同步的；
 *   · 三件套字节复制是同步的、15ms 量级，且 SQLite 打开副本时会自己应用 `-wal`。
 *     最坏情况（复制期间正发生 checkpoint）得到的是**稍旧**的快照，不是坏库——
 *     这里再用 `PRAGMA integrity_check` 兜一道，不 `ok` 就响亮报错。
 */
export function copySnapshot(dbPath: string, options: { readonly dir?: string } = {}): SnapshotInfo {
  if (!existsSync(dbPath)) {
    throw new FishError('DB_UNAVAILABLE', `找不到生产主库：${dbPath}`, {
      hint: '设置 dbPath / FISH_DB，或确认 fishHome 指向含 data/index.sqlite 的目录',
      details: { dbPath },
    })
  }
  const dir = options.dir ?? mkdtempSync(path.join(os.tmpdir(), 'fish-proxy-'))
  const files: string[] = []
  try {
    for (const suffix of ['', '-wal', '-shm']) {
      const from = `${dbPath}${suffix}`
      if (!existsSync(from)) continue
      copyFileSync(from, path.join(dir, `index.sqlite${suffix}`))
      files.push(`index.sqlite${suffix}`)
    }
    const snapshotDb = path.join(dir, 'index.sqlite')
    let integrity = 'unchecked'
    const probe = new DatabaseSync(snapshotDb, { readOnly: true })
    try {
      const rows = probe.prepare('PRAGMA integrity_check').all() as { integrity_check?: string }[]
      integrity = rows.map((row) => String(row.integrity_check ?? '')).join(';') || 'empty'
    } finally {
      probe.close()
    }
    if (integrity !== 'ok') {
      throw new FishError('DB_UNAVAILABLE', `快照副本完整性检查未通过：${integrity}`, {
        hint: '重跑一次（复制期间正好有 checkpoint 时会拿到旧副本；连续失败请先确认生产库是否损坏）',
        details: { dbPath, dir, integrity },
      })
    }
    return {
      dir,
      dbPath: snapshotDb,
      files,
      bytes: statSync(snapshotDb).size,
      asOf: new Date().toISOString(),
      integrity,
    }
  } catch (error) {
    rmSync(dir, { recursive: true, force: true })
    throw error
  }
}

/**
 * 在**只读快照**上跑一条 `fish.py` 命令。
 *
 * 调用方必须自己保证"这条命令是只读的"（`cli-map` 的 `impl='proxy-readonly'`）；
 * 本函数额外拒绝 `--apply` / `--write` 这类强写开关——双保险，因为"分类标错"
 * 与"参数拼错"是两种不同的事故。
 */
export function runFishCliOnSnapshot(options: FishCliRunOptions): FishCliRunResult {
  const timeoutMs = options.timeoutMs ?? DEFAULT_PY_TIMEOUT_MS
  const forbidden = options.args.find((arg) => arg === '--apply' || arg === '--force' || arg === '--write')
  if (forbidden !== undefined) {
    throw new FishError('SHADOW_READ_ONLY', `只读转发不许带写开关：${forbidden}`, {
      hint: '这条命令的写形态请在终端里跑（`fish …`），影子期不在生产库执行',
      details: { args: options.args },
    })
  }
  const fishPy = resolveFishPy(options.fishHome)
  const productionShaBefore = sha12(options.dbPath)
  let snapshot: SnapshotInfo | null = null
  try {
    snapshot = copySnapshot(options.dbPath)
    const argv = [fishPy, '--root', snapshot.dir, ...options.args]
    const child = spawnSync(resolvePythonBin(), argv, {
      encoding: 'utf8',
      timeout: timeoutMs,
      maxBuffer: MAX_BUFFER_BYTES,
      windowsHide: true,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
    })
    const timedOut = child.error !== undefined && (child.error as NodeJS.ErrnoException).code === 'ETIMEDOUT'
    const ok = child.error === undefined && child.status === 0
    return {
      ok,
      argv,
      exitCode: child.status,
      signal: child.signal,
      stdout: typeof child.stdout === 'string' ? child.stdout : '',
      stderr: typeof child.stderr === 'string' ? child.stderr : '',
      timedOut,
      snapshot,
      failure:
        child.error === undefined
          ? child.status === 0
            ? null
            : `fish.py 退出码 ${String(child.status)}`
          : `启动/执行失败：${child.error.message}`,
      productionShaBefore,
      productionShaAfter: sha12(options.dbPath),
    }
  } catch (error) {
    return {
      ok: false,
      argv: [],
      exitCode: null,
      signal: null,
      stdout: '',
      stderr: '',
      timedOut: false,
      snapshot,
      failure: error instanceof Error ? error.message : String(error),
      productionShaBefore,
      productionShaAfter: sha12(options.dbPath),
    }
  } finally {
    if (snapshot !== null) rmSync(snapshot.dir, { recursive: true, force: true })
  }
}
