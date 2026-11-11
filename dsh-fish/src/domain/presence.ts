/**
 * 面板数据源：**谁在场 + 最近几条动态**（两条只读 SELECT）。
 *
 * 为什么单独成模块（2026-11-09 减重第二批）：面板是「只读 + 本体能力补充」里唯一保留的 UI，
 * 而它需要的最小数据源原本寄生在 `board.ts`（36 KB：重叠判定 / 残留行 / 同公民折叠 / 可读名 /
 * 自动面聚合）。砍掉 `board.ts` 之后面板就断粮，所以这里只留**两条 SELECT**，一行判定都不带。
 *
 * 三条纪律（与 `core/db.ts` 同口径）：只经 `openReadOnly()` 打开；打开后立刻
 * `assertWriteRejected()` 真试一次写（"能写"必须当成错误）；除两条 SELECT 外不碰库、不写审计。
 *
 * 明确**不做**（原 `board.ts` 的职责，随它删除）：撞车/重叠判定、残留行、同公民多会话折叠、
 * 可读名消歧（`naming.ts`）、自动占用面聚合（`activity.ts`）。面板要这几样，等下一期「接真数据」
 * 时按新口径重做，而不是在这里复刻一份会跟着 fish 漂移的实现——那正是这一批要甩掉的耦合。
 *
 * `presence.session_key` 是 2026-10-26 进主键的列，本模块**直接**读它、不做「列在不在」兜底：
 * 老库在启动门禁（schema 指纹）就该被打回，让面板静默降级反而会把"库没迁移"藏起来。
 *
 * @module dsh-fish/domain/presence
 */

import type { DatabaseSync, SQLInputValue } from 'node:sqlite'

import { assertWriteRejected, openReadOnly } from '../core/db.ts'
import { FishError } from '../core/errors.ts'

/** 在场行的缺省条数。 */
export const PRESENCE_LIMIT_DEFAULT = 20

/** 动态的缺省条数（"最近几条"——面板只画最近这一段）。 */
export const BOARD_POSTS_LIMIT_DEFAULT = 5

/** 一行在场记录：`presence` 原列 + 公民可读名 + 它属于哪块板。 */
export interface PresenceRow {
  readonly citizen_id: string
  /** `citizens.display_name` 原值（空串 = 库里没填）；**不做**消歧（那是 `naming` 的活）。 */
  readonly display_name: string
  readonly host: string
  readonly session_key: string
  readonly status: string
  readonly doing: string
  /** 声明占用面（逗号分隔的原文）。 */
  readonly surface: string
  readonly last_seen_at: string
  readonly scope: string
  readonly scope_key: string
  readonly period_key: string
}

/** 一条看板动态（`board_posts` 行 + 它属于哪块板）。 */
export interface BoardPostRow {
  readonly citizen_id: string
  readonly kind: string
  readonly text: string
  readonly created_at: string
  readonly scope: string
  readonly scope_key: string
  readonly period_key: string
}

/** 面板要的全量数据（一次读、一次返回；面板不该自己去连库）。 */
export interface PresencePanel {
  /** 恒为 `true`：这是接口而不是建议——本模块没有任何写路径。 */
  readonly readOnly: true
  /** `true` = 自证写入被 SQLite 拒绝（正常）；`false` = 竟然写成功（严重问题）。 */
  readonly writeRejected: boolean
  readonly presence: readonly PresenceRow[]
  readonly posts: readonly BoardPostRow[]
}

/** 读面板数据的选项（全部给默认值：面板不缺参数也能拿到东西）。 */
export interface PresenceOptions {
  /** 只取某类板：`root` / `project` / `branch`；空 = 不分。 */
  readonly scope?: string | null
  /** 只取某个周期键（`YYMMDD`）；空 = 不分。 */
  readonly periodKey?: string | null
  /** 在场行上限。 */
  readonly presenceLimit?: number
  /** 动态条数上限。 */
  readonly postsLimit?: number
}

/** 上限归一：非法值**响亮失败**（`presenceLimit=0` 不许静默变成默认值）。 */
function normalizeLimit(value: number | undefined, fallback: number, name: string): number {
  if (value === undefined) return fallback
  if (!Number.isInteger(value) || value <= 0) {
    throw new FishError('COMMAND_USAGE', `${name} 必须是正整数，收到 ${String(value)}`, { hint: `例如 ${name}=${fallback}` })
  }
  return value
}

/**
 * 板过滤条件（两条 SQL 共用一份口径：只认 `boards` 的两列）。
 *
 * 为什么拼字符串而不在两条查询里各写一遍 `if (scope)`：那是会漂开的两份真相
 * （一个忘了带 period 就是"多读了别人的板"）。条件是**枚举出来的固定片段**，值一律走占位符。
 */
function boardFilter(options: PresenceOptions): { readonly sql: string; readonly params: SQLInputValue[] } {
  const conditions: string[] = []
  const params: SQLInputValue[] = []
  const scope = (options.scope ?? '').trim()
  if (scope !== '') {
    conditions.push('b.scope = ?')
    params.push(scope)
  }
  const periodKey = (options.periodKey ?? '').trim()
  if (periodKey !== '') {
    conditions.push('b.period_key = ?')
    params.push(periodKey)
  }
  return { sql: conditions.length === 0 ? '' : `WHERE ${conditions.join(' AND ')}`, params }
}

/**
 * SQL ①：谁在场。
 *
 * 列名一律显式起别名：`node:sqlite` 的 `all()` 返回**对象**，裸列名拿不到值（T02 踩过一次）。
 */
export function presenceRows(db: DatabaseSync, options: PresenceOptions = {}): PresenceRow[] {
  const filter = boardFilter(options)
  const limit = normalizeLimit(options.presenceLimit, PRESENCE_LIMIT_DEFAULT, 'presenceLimit')
  const sql =
    'SELECT p.citizen_id AS citizen_id, COALESCE(c.display_name, \'\') AS display_name, ' +
    'COALESCE(c.host, \'\') AS host, p.session_key AS session_key, p.status AS status, ' +
    'COALESCE(p.doing, \'\') AS doing, COALESCE(p.surface, \'\') AS surface, ' +
    'p.last_seen_at AS last_seen_at, b.scope AS scope, b.scope_key AS scope_key, ' +
    'b.period_key AS period_key ' +
    'FROM presence p LEFT JOIN citizens c ON c.citizen_id = p.citizen_id ' +
    'JOIN boards b ON b.id = p.board_id ' +
    `${filter.sql === '' ? '' : `${filter.sql} `}` +
    'ORDER BY p.last_seen_at DESC LIMIT ?'
  return db.prepare(sql).all(...filter.params, limit) as unknown as PresenceRow[]
}

/** SQL ②：最近几条动态（按 `id` 倒序——最新的在最上面，符合"此刻"的语义）。 */
export function recentBoardPosts(db: DatabaseSync, options: PresenceOptions = {}): BoardPostRow[] {
  const filter = boardFilter(options)
  const limit = normalizeLimit(options.postsLimit, BOARD_POSTS_LIMIT_DEFAULT, 'postsLimit')
  const sql =
    'SELECT bp.citizen_id AS citizen_id, bp.kind AS kind, bp.text AS text, ' +
    'bp.created_at AS created_at, b.scope AS scope, b.scope_key AS scope_key, ' +
    'b.period_key AS period_key ' +
    'FROM board_posts bp JOIN boards b ON b.id = bp.board_id ' +
    `${filter.sql === '' ? '' : `${filter.sql} `}` +
    'ORDER BY bp.id DESC LIMIT ?'
  return db.prepare(sql).all(...filter.params, limit) as unknown as BoardPostRow[]
}

/**
 * 打开库 → 自证只读 → 两条 SELECT → 关库。面板侧的**唯一**入口。
 *
 * 顺序有意如此：先自证"写不进去"再读——自证失败时不该继续把数据递给面板。
 * @throws DatabaseError `DB_UNAVAILABLE` —— 打不开（路径不存在 / 被独占锁住 / 不是 SQLite）
 * @throws FishError `COMMAND_USAGE` —— 上限不是正整数
 */
export function readPresencePanel(dbPath: string, options: PresenceOptions = {}): PresencePanel {
  const db = openReadOnly(dbPath)
  try {
    const writeRejected = assertWriteRejected(db)
    return { readOnly: true, writeRejected, presence: presenceRows(db, options), posts: recentBoardPosts(db, options) }
  } finally {
    db.close()
  }
}

/** 面板数据的人读形态（`/fish shadow presence` 与判据脚本共用同一份渲染）。 */
export function renderPresencePanel(panel: PresencePanel): string {
  const lines = [
    `面板数据源（只读两条 SELECT）：在场 ${panel.presence.length} 行 / 动态 ${panel.posts.length} 行`,
    `只读自证：${panel.writeRejected ? '写入被 SQLite 拒绝（正常）' : '⚠ 写入竟然成功——立刻停用插件'}`,
  ]
  if (panel.presence.length === 0) {
    lines.push('', '在场：**还没有人签到**（`presence` 里没有行；未登记 = 未发生）')
  } else {
    lines.push('', '在场（按 last_seen_at 倒序）：')
    for (const row of panel.presence) {
      const doing = row.doing.trim() === '' ? '（未写 doing）' : row.doing
      const board = row.scope === 'root' ? '根' : `${row.scope}:${row.scope_key === '' ? '（空）' : row.scope_key}`
      const who = row.display_name === '' ? '（未填显示名）' : row.display_name
      lines.push(`  ${who}（${row.citizen_id}）· ${board} · ${row.status} · ${doing}`)
      if (row.surface.trim() !== '') lines.push(`    声明占用面：${row.surface}`)
      lines.push(`    最后活跃：${row.last_seen_at}`)
    }
  }
  if (panel.posts.length === 0) {
    lines.push('', '动态：最近没有动态')
  } else {
    lines.push('', '动态（最新在上）：')
    for (const post of panel.posts) lines.push(`  ${post.created_at}  ${post.citizen_id}  [${post.kind}] ${post.text}`)
  }
  lines.push('', '口径：不做重叠判定 / 残留行 / 可读名消歧 / 自动面聚合（原文原样给人看）。')
  return lines.join('\n')
}
