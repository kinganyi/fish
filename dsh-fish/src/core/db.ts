/**
 * 数据库访问层：影子期**只有只读一种打开方式**。
 *
 * 三条纪律（每一条都有事故背景）：
 *   1. `readOnly: true` 打开——不是"我们小心不写"，而是让 SQLite 自己拒绝写；
 *   2. 额外 `PRAGMA query_only=1`——防住那些"看起来只读、其实会建临时结构"的语句；
 *   3. `busy_timeout` 显式设置——不设时默认 0，任何一次并发都会立刻 BUSY，
 *      而影子期恰恰与 Python 侧读同一库。
 *
 * 写路径（换心期才需要）不在本模块：等到那一刻再引入 `openReadWrite`，
 * 并同时引入 maintenance 检测，避免"库里多了个能写的手却没人管"。
 *
 * @module dsh-fish/core/db
 */

import { DatabaseSync } from 'node:sqlite'

import { DatabaseError } from './errors.ts'

/** 打开只读连接的选项。 */
export interface OpenReadOnlyOptions {
  /** SQLite busy 超时（毫秒）；与 Python 侧的 5000 对齐。 */
  readonly busyTimeoutMs?: number
}

/** 默认 busy 超时：与 `spec/contract.json` 的 `concurrency.busyTimeoutMs` 保持一致。 */
export const DEFAULT_BUSY_TIMEOUT_MS = 5000

/**
 * 以只读方式打开 fish 主库。
 * @throws DatabaseError `DB_UNAVAILABLE` —— 打不开（路径不存在 / 被独占锁住 / 不是 SQLite）
 */
export function openReadOnly(dbPath: string, options: OpenReadOnlyOptions = {}): DatabaseSync {
  const busyTimeoutMs = options.busyTimeoutMs ?? DEFAULT_BUSY_TIMEOUT_MS
  let db: DatabaseSync
  try {
    db = new DatabaseSync(dbPath, { readOnly: true })
  } catch (error) {
    throw new DatabaseError('DB_UNAVAILABLE', `只读打开失败：${dbPath}`, {
      hint: '确认文件存在且不是被 Python 侧独占（`-wal` 文件存在是正常现象）',
      details: { dbPath },
      cause: error,
    })
  }
  try {
    db.exec(`PRAGMA busy_timeout=${Math.max(0, Math.trunc(busyTimeoutMs))}`)
    db.exec('PRAGMA query_only=1')
  } catch (error) {
    db.close()
    throw new DatabaseError('DB_UNAVAILABLE', `设置只读 PRAGMA 失败：${(error as Error).message}`, {
      details: { dbPath },
      cause: error,
    })
  }
  return db
}

/** 表/视图/索引/触发器的数量统计（比"表数"更能发现"少读了一个文件"）。 */
export function objectCounts(db: DatabaseSync): Record<string, number> {
  const rows = db
    .prepare('SELECT type, COUNT(*) AS n FROM sqlite_master GROUP BY type ORDER BY type')
    .all() as { type: string; n: number }[]
  const counts: Record<string, number> = {}
  for (const row of rows) counts[row.type] = row.n
  return counts
}

/**
 * 业务表清单：排除 FTS 索引本体（`memory_fts`）、其影子表（`memory_fts_*`）与
 * `sqlite_sequence`。口径 = 21 张（`03-调研-fish全量能力.md` §4 实测）。
 */
export function businessTableNames(db: DatabaseSync): string[] {
  const rows = db
    .prepare("SELECT name FROM sqlite_master WHERE type='table' AND name <> 'memory_fts' AND name NOT LIKE 'memory\\_fts\\_%' ESCAPE '\\' AND name <> 'sqlite_sequence' ORDER BY name")
    .all() as { name: string }[]
  return rows.map((row) => row.name)
}

/**
 * 自证只读：真去试一次写，确认 SQLite 拒绝它。
 *
 * 为什么"能写"必须被当成错误：影子期若有一条能写的路径，双写者会互相覆盖，
 * 而覆盖的表现是"某些行莫名消失"——比崩溃难查得多。
 * @returns `true` = 写被拒绝（正常）；`false` = 竟然写成功了（严重问题）
 */
export function assertWriteRejected(db: DatabaseSync): boolean {
  try {
    db.exec('CREATE TABLE __dsh_fish_readonly_probe (x INTEGER)')
  } catch {
    return true
  }
  // 走到这里说明写成功了：清理并报告失败。
  try {
    db.exec('DROP TABLE __dsh_fish_readonly_probe')
  } catch {
    /* 清理失败不影响判定。 */
  }
  return false
}
