/**
 * ④ 段 A 类（未闭合）与 ⑤ 段（错题本）的取数原语。
 *
 * 为什么从 `inject.ts` 抽出来：推送候选的 A 类（`rule`）复用**同一份**
 * `_open_loop_rows`，而"临时在 inject.ts 里导出给 push.ts 用"会造出
 * `inject → push → inject` 的循环依赖。抽成独立模块后，注入包与推送候选
 * 拿到的是同一个谓词（口径只有一处，改门槛不会只改一半）。
 *
 * @module dsh-fish/domain/openloop
 */

import type { DatabaseSync, SQLInputValue } from 'node:sqlite'

/** ④ 段的窗口与上限（fish.py:7322）。 */
export const ATTENTION_PER_TYPE_LIMIT = 2
export const ATTENTION_BLOCK_MAX_TOKENS = 400
export const ATTENTION_TITLE_MAX_CHARS = 50
export const ATTENTION_OPEN_WINDOW_DAYS = 30
export const ATTENTION_LESSON_WINDOW_DAYS = 7
export const ATTENTION_GAP_SCAN_LIMIT = 50

/** 未闭合的关键词表（标题命中任一即算 A 类信号）。 */
export const ATTENTION_OPEN_KEYWORDS = [
  '未闭合',
  '待办',
  '下一步',
  'todo',
  '待确认',
  '待定',
  '待验证',
  '待补',
] as const

/** A 类「未闭合事项」的唯一谓词（`_open_loop_rows` 与 `count_open_loop_rows` 共用）。 */
export function openLoopPredicate(): { clauses: string; keywords: string[] } {
  const cond = ATTENTION_OPEN_KEYWORDS.map(() => "INSTR(LOWER(COALESCE(title,'')), ?) > 0").join(' OR ')
  return {
    clauses: `status='active' AND dormant=0 AND recorded_at >= ? AND (${cond})`,
    keywords: ATTENTION_OPEN_KEYWORDS.map((keyword) => keyword.toLowerCase()),
  }
}

/** A 类在窗口内**总共有多少条**（不受 `perType` 限制）——段头 N/M 的 M。 */
export function countOpenLoopRows(db: DatabaseSync, sinceZ: string, exclude: readonly string[] = []): number | null {
  const predicate = openLoopPredicate()
  let sql = `SELECT COUNT(*) AS n FROM code_index WHERE ${predicate.clauses}`
  const params: SQLInputValue[] = [sinceZ, ...predicate.keywords]
  const extra = exclude.filter((code) => code !== '')
  if (extra.length > 0) {
    sql += ` AND code NOT IN (${extra.map(() => '?').join(',')})`
    params.push(...extra)
  }
  try {
    const row = db.prepare(sql).get(...params) as { n: number } | undefined
    return Number(row?.n ?? 0)
  } catch {
    return null
  }
}

/**
 * A 类「未闭合事项」：标题命中开放词表的 active 记忆（窗口内，时间降序）。
 *
 * `project`（T05 起）非空时只取该项目的条目——项目状态表的「未了事项」列走这一档。
 * 为什么加在这里而不是另写一份 SQL：状态表与注入包的计数必须是**同一个谓词**，
 * 否则会出现"注入包说有 3 条、状态表说 2 条"这种最难解释的分歧。
 */
export function openLoopRows(
  db: DatabaseSync,
  sinceZ: string,
  limit: number,
  exclude: ReadonlySet<string> = new Set<string>(),
  project: string | null = null,
): Array<{ code: string; title: string }> {
  const predicate = openLoopPredicate()
  let where = predicate.clauses
  const params: SQLInputValue[] = [sinceZ, ...predicate.keywords]
  const key = (project ?? '').trim()
  if (key !== '') {
    where += ' AND project = ?'
    params.push(key)
  }
  const sql = `SELECT code,title FROM code_index WHERE ${where}` + ' ORDER BY recorded_at DESC, code DESC LIMIT ?'
  params.push(Math.trunc(limit) + exclude.size)
  let rows: Array<{ code: string; title: string | null }>
  try {
    rows = db.prepare(sql).all(...params) as Array<{ code: string; title: string | null }>
  } catch {
    return []
  }
  const out: Array<{ code: string; title: string }> = []
  for (const row of rows) {
    if (exclude.has(row.code)) continue
    out.push({ code: row.code, title: (row.title ?? '').trim() })
    if (out.length >= Math.trunc(limit)) break
  }
  return out
}

/** 窗口内 lesson 的总条数（不受 `perType` 限制）——段⑤ 段头 N/M 的 M。 */
export function countLessonRows(db: DatabaseSync, sinceZ: string, exclude: readonly string[] = []): number | null {
  let sql = "SELECT COUNT(*) AS n FROM code_index WHERE mem_type='lesson' AND status='active' AND dormant=0 AND recorded_at >= ?"
  const params: SQLInputValue[] = [sinceZ]
  const extra = exclude.filter((code) => code !== '')
  if (extra.length > 0) {
    sql += ` AND code NOT IN (${extra.map(() => '?').join(',')})`
    params.push(...extra)
  }
  try {
    const row = db.prepare(sql).get(...params) as { n: number } | undefined
    return Number(row?.n ?? 0)
  } catch {
    return null
  }
}

/** 窗口内 lesson 的 `(code, title)`：时间降序；`sameProject` 给定时只取该项目。 */
export function lessonRows(
  db: DatabaseSync,
  sinceZ: string,
  limit: number,
  exclude: ReadonlySet<string> = new Set<string>(),
  sameProject: string | null = null,
): Array<{ code: string; title: string }> {
  let where = 'recorded_at >= ?'
  const params: SQLInputValue[] = [sinceZ]
  if (sameProject !== null) {
    where += ' AND project = ?'
    params.push(sameProject)
  }
  const sql =
    "SELECT code,title FROM code_index WHERE mem_type='lesson' AND status='active' AND dormant=0 AND " +
    `${where} ORDER BY recorded_at DESC, code DESC LIMIT ?`
  params.push(Math.trunc(limit) + exclude.size)
  let rows: Array<{ code: string; title: string | null }>
  try {
    rows = db.prepare(sql).all(...params) as Array<{ code: string; title: string | null }>
  } catch {
    return []
  }
  const out: Array<{ code: string; title: string }> = []
  for (const row of rows) {
    if (exclude.has(row.code)) continue
    out.push({ code: row.code, title: (row.title ?? '').trim() })
    if (out.length >= Math.trunc(limit)) break
  }
  return out
}

/** B 类「最近的坑」：同项目优先，不足再用跨项目补齐（复刻 `_recent_lesson_rows`）。 */
export function recentLessonRows(
  db: DatabaseSync,
  sinceZ: string,
  limit: number,
  exclude: ReadonlySet<string> = new Set<string>(),
  project: string | null = null,
): Array<{ code: string; title: string }> {
  const key = (project ?? '').trim() === '' ? null : (project ?? '').trim()
  const out = key === null ? [] : lessonRows(db, sinceZ, limit, exclude, key)
  if (out.length < limit) {
    const taken = new Set([...exclude, ...out.map((item) => item.code)])
    out.push(...lessonRows(db, sinceZ, limit - out.length, taken, null))
  }
  return out
}
