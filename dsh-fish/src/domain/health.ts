/**
 * 自检（复刻 `cmd_healthcheck`，fish.py:10851 的**核心读数**）。
 *
 * 七项读数：事件数 / 活跃记忆 / 休眠 / 作废 / 断链 / FTS 是否落后 / owner 是否有效公民。
 *
 * 为什么断链必须在自检层报：`code_index.file_path` 指向的正文文件没了，记忆**依然查得到**
 * （索引行还在），只有真去读正文时才会发现"这条记忆是空的"。这类失真没有任何报错。
 *
 * 为什么 FTS 落后要单独一条：索引落后 1 条时"存进去但搜不到"，是同一类静默失真。
 *
 * 为什么 owner 要查：`owner` 是"这条记忆归谁"的唯一字段，指向不存在的公民就是
 * 一条指向幽灵的归属——它会被所有按 owner 的统计静默算错。
 *
 * @module dsh-fish/domain/health
 */

import { existsSync } from 'node:fs'
import path from 'node:path'
import type { DatabaseSync } from 'node:sqlite'

import { corpusStats } from './corpus.ts'

/**
 * 语料层读数（T05 新增，对齐生产 `cmd_healthcheck` 的"语料 N 段 / 会话 M / 降级 D"）。
 *
 * 为什么体检要带语料：语料库是**独立库**，主库体检看不见它；而"采了 0 段"与"库不在"
 * 在别处长得一模一样。生产实现自己写明这只是"锦上添花"——读失败就跳过这一行，
 * 所以本模块也把它做成可空字段（`null` = 读不出来，不是 0 段）。
 */
export interface HealthCorpusReadings {
  readonly segments: number
  readonly sessions: number
  readonly degraded: number
  readonly alarm: string | null
  readonly zeroStreak: number
}

/** 自检读数（全部是"读出来的事实"，不含判断）。 */
export interface HealthReadings {
  /** `events` 行数（append-only 的总量）。 */
  readonly events: number
  /** `code_index` 里 `status='active'` 的条数。 */
  readonly activeMemories: number
  /** `dormant=1` 的条数（**不筛 status**：Python 也是全量数）。 */
  readonly dormant: number
  /** `status='withdrawn'` 的条数。 */
  readonly withdrawn: number
  /** 正文文件不见了的编码（按库内顺序）。 */
  readonly brokenLinks: readonly string[]
  /** `memory_fts` 行数；`-1` = 索引不可用（表不存在）。 */
  readonly fts: number
  /** 索引落后条数（`code_index` 总数 − FTS 行数）。 */
  readonly ftsGap: number
  /** owner 不是有效公民的编码（库里没有任何公民时恒为空：空库不误报）。 */
  readonly ownerInvalid: readonly string[]
  /** 语料层读数；`null`/缺省 = 读不出来（生产在这一段是 `try/except: pass`）。 */
  readonly corpus?: HealthCorpusReadings | null
}

/** 采一次读数（**只读**：全部是 SELECT）。 */
export function healthcheckReadings(db: DatabaseSync, dataRoot: string): HealthReadings {
  const countOf = (sql: string): number => {
    const row = db.prepare(sql).get() as { n: number } | undefined
    return Number(row?.n ?? 0)
  }
  const events = countOf('SELECT COUNT(*) AS n FROM events')
  const activeMemories = countOf("SELECT COUNT(*) AS n FROM code_index WHERE status='active'")
  const dormant = countOf('SELECT COUNT(*) AS n FROM code_index WHERE dormant=1')
  const withdrawn = countOf("SELECT COUNT(*) AS n FROM code_index WHERE status='withdrawn'")

  const brokenLinks: string[] = []
  const rows = db.prepare('SELECT code,file_path FROM code_index').all() as Array<{
    code: string
    file_path: string | null
  }>
  for (const row of rows) {
    if (row.file_path === null || row.file_path === '') continue
    if (!existsSync(path.join(dataRoot, row.file_path))) brokenLinks.push(row.code)
  }

  let fts = -1
  try {
    fts = countOf('SELECT COUNT(*) AS n FROM memory_fts')
  } catch {
    fts = -1
  }
  const total = countOf('SELECT COUNT(*) AS n FROM code_index')
  const ftsGap = fts < 0 ? 0 : total - fts

  const citizens = db.prepare('SELECT citizen_id FROM citizens').all() as Array<{ citizen_id: string }>
  const ids = new Set(citizens.map((row) => row.citizen_id))
  const ownerInvalid: string[] = []
  if (ids.size > 0) {
    const owners = db.prepare('SELECT code,owner FROM code_index').all() as Array<{
      code: string
      owner: string | null
    }>
    for (const row of owners) {
      if (row.owner === 'unknown') continue
      if (row.owner === null || !ids.has(row.owner)) ownerInvalid.push(row.code)
    }
  }

  return { events, activeMemories, dormant, withdrawn, brokenLinks, fts, ftsGap, ownerInvalid, corpus: readCorpus(dataRoot) }
}

/** 语料层读数（读不出来 → `null`，不冒充"0 段"）。 */
function readCorpus(dataRoot: string): HealthCorpusReadings | null {
  try {
    const stats = corpusStats(dataRoot)
    return {
      segments: stats.segments,
      sessions: stats.sessions,
      degraded: stats.degraded.length,
      alarm: stats.alarm,
      zeroStreak: stats.stall.zero_streak,
    }
  } catch {
    return null
  }
}

/**
 * 渲染自检文本（逐行对齐 `cmd_healthcheck` 的 print）。
 *
 * 文案里的下一步（`跑 fish reindex` / `跑 fish rebuild`）是**结论的一部分**：
 * 只说"落后 3 条"而不说怎么补，读数就只是一句让人焦虑的话。
 */
export function renderHealthcheck(readings: HealthReadings): string {
  const lines: string[] = []
  lines.push(
    `事件 ${readings.events} 条 / 活跃记忆 ${readings.activeMemories} 条 / 休眠 ${readings.dormant} 条` +
      ` / 作废 ${readings.withdrawn} 条 / 断链 ${readings.brokenLinks.length} 条`,
  )
  for (const code of readings.brokenLinks) lines.push(`  断链：${code}`)
  if (readings.fts < 0) {
    lines.push('  ⚠ 全文索引不可用（memory_fts 不存在）——跑 fish reindex 重建')
  } else if (readings.ftsGap !== 0) {
    lines.push(`  全文索引 ${readings.fts} 条  ⚠ 落后 ${readings.ftsGap} 条（跑 fish reindex 补齐）`)
  } else {
    lines.push(`  全文索引 ${readings.fts} 条（与记忆同步）`)
  }
  if (readings.ownerInvalid.length > 0) {
    lines.push(
      `  ⚠ owner 不是有效公民 ${readings.ownerInvalid.length} 条：${readings.ownerInvalid.slice(0, 5).join(', ')}` +
        '（跑 fish rebuild 可归一到系统主人）',
    )
  }
  // `corpus` 允许缺省（老调用方手工构造 readings 时没这个字段）：缺 = 不打印这一行，
  // 与"读不出来"同一个表现——绝不把 undefined 当成"0 段"（那是把未知说成事实）。
  const corpus = readings.corpus ?? null
  if (corpus !== null) {
    lines.push(
      `  语料 ${corpus.segments} 段 / 会话 ${corpus.sessions} / 降级 ${corpus.degraded}` +
        (corpus.alarm !== null ? `  ⚠ 停滞 ${corpus.alarm}（连续 ${corpus.zeroStreak} 轮 0 新增）` : ''),
    )
  }
  return `${lines.join('\n')}\n`
}
