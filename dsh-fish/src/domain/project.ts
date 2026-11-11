/**
 * 项目与别名的**只读**面：`project status` / `alias list` / `workspace list`。
 *
 * ## 三个命令为什么都在这个模块里
 *
 * 它们读的是同一组"项目坐标系"表：`code_registry`（项目清单的唯一来源，裁决 Q6）、
 * `name_aliases`（别名）、`workspace_map`（工作区归属）。三张表在生产的语义里互相引用
 * （别名展开决定"同一个项目的全部名字"，工作区映射决定"这段记忆属于谁"），
 * 拆成三个文件会让 `resolveName` 被复制三遍——而"同一件事两种语义"是本项目最贵的 bug 形态。
 *
 * ## 只读依据（逐条对过源码，不是"看起来像读"）
 *
 *   · `project_status_rows`（`p0/fish.py:14517`，docstring 明写"**只读**：不改任何表、不写盘"）；
 *   · `alias` 的 list 分支（`p0/fish.py:6722-6738`：两条 SELECT + `resolve_name`）；
 *   · `workspace` 的 list 分支（`p0/fish.py:10065-10076` → `list_workspace_mappings`，纯 SELECT）。
 *
 * ## 与生产的一处**有意差异**
 *
 * 生产 `fish project status` 默认会把渲染结果**写进** `<home>/10-项目库/项目状态表.md`
 * （`render_project_status` → `_layout_write`）。影子期不写任何文件，所以本模块只做
 * "同一份六列表 + 同一份 JSON"，并在回执末尾明写"没有落盘、终端里会写到哪"。
 * `--json` 形态在生产里本来就不写文件（`cmd_project_status` 的 `if args.json` 分支），
 * 因此 JSON 回执与生产**逐字节同形**——这也是对拍取 JSON 而不是取表格的原因。
 *
 * @module dsh-fish/domain/project
 */

import { readdirSync, statSync } from 'node:fs'
import path from 'node:path'
import type { DatabaseSync } from 'node:sqlite'

import { FishError } from '../core/errors.ts'
import { datePartsInOffset, isoWithOffset, offsetMinutesOf, parseStamp, utcStamp } from '../core/time.ts'
import { ALIAS_KINDS, findCitizenByName, resolveName } from './names.ts'
import { ATTENTION_OPEN_KEYWORDS, ATTENTION_OPEN_WINDOW_DAYS, openLoopRows } from './openloop.ts'

// 别名与未闭合谓词都**复用**既有实现（`names.ts` / `openloop.ts`）：同一件事两套语义
// 是本项目最贵的 bug 形态，而"未了事项口径与注入包同源"正是生产 docstring 明写的纪律。

// ---------------------------------------------------------------- 常量（逐条对齐 fish.py）

/** 六列的**严格顺序**（§3.4）。 */
export const PST_COLUMNS = ['项目', '状态', '最近活跃', '看板入口', '主责 AI', '未了事项'] as const

/** 未了事项与最近活跃的窗口（与注入包的 `ATTENTION_OPEN_WINDOW_DAYS` **同一个常量**）。 */
export const PST_OPEN_WINDOW_DAYS = ATTENTION_OPEN_WINDOW_DAYS
/** 无开档期时回退"最近 7 天记忆 owner"。 */
export const PST_OWNER_FALLBACK_DAYS = 7

export const PST_DASH_NO_MEMORY = '—（无记忆）'
export const PST_DASH_NO_BOARD = '—（今日无看板）'
export const PST_DASH_NO_OWNER = '—（无开档期）'
export const PST_DASH_NONE = '—'
export const PST_DASH_UNAVAILABLE = '—（数据源不可用）'

/** 口径常量：置顶到导出面，脚注与谓词都从这里取（两处各写一份必然漂开）。 */
export const LAYOUT_STATUS_TABLE = '10-项目库/项目状态表.md'

/** 未了事项词表（与注入包 A 类信号同源；来源在 `openloop.ts`）。 */
export { ATTENTION_OPEN_KEYWORDS } from './openloop.ts'

// ---------------------------------------------------------------- as-of 口径

/** 冻结时刻：`date` 是瞬时值，`offsetMinutes` 是**入参自己声明的**时区。 */
export interface ProjectAsOf {
  readonly date: Date
  readonly offsetMinutes: number
}

/**
 * 解析 `--as-of`（与 `fish._parse_as_of` 同口径：`Z` / `+08:00` / 无时区按本地补）。
 * @throws FishError `COMMAND_USAGE` —— 解析失败（生产对应 exit 2）
 */
export function parseAsOf(raw?: string | null): ProjectAsOf {
  const text = String(raw ?? '').trim()
  if (text === '') return { date: new Date(), offsetMinutes: -new Date().getTimezoneOffset() }
  const date = parseStamp(text)
  if (date === null) {
    throw new FishError(
      'COMMAND_USAGE',
      `--as-of 无法解析：${JSON.stringify(raw)}（示例：2026-10-26T00:00:00+08:00 或 2026-10-26T00:00:00Z）`,
    )
  }
  const declared = offsetMinutesOf(text)
  return { date, offsetMinutes: declared ?? -date.getTimezoneOffset() }
}

/** as-of 的本地 ISO（`iso_local(as_of)`）。 */
export function asOfIso(asOf: ProjectAsOf): string {
  return isoWithOffset(asOf.date, asOf.offsetMinutes)
}

// ---------------------------------------------------------------- 别名（单向展开）

/** `fish alias list` 的一行（`kind` / `from` / `to` / `created_at` / `reason`）。 */
export interface AliasRow {
  readonly kind: string
  readonly from: string
  readonly to: string
  readonly createdAt: string
  readonly reason: string
}

/** 读全部别名行（`ORDER BY kind, from_name`）。 */
export function aliasRows(db: DatabaseSync): AliasRow[] {
  const rows = db
    .prepare('SELECT kind,from_name,to_name,created_at,reason FROM name_aliases ORDER BY kind,from_name')
    .all() as { kind: string; from_name: string; to_name: string; created_at: string; reason: string | null }[]
  return rows.map((row) => ({
    kind: String(row.kind),
    from: String(row.from_name),
    to: String(row.to_name),
    createdAt: String(row.created_at ?? ''),
    reason: row.reason === null || row.reason === undefined ? '' : String(row.reason),
  }))
}

/**
 * `fish alias list` 的回执：原始行 + "哪些名字解析成同一个"的分组。
 *
 * 分组顺序刻意与生产一致（**首次出现即入表**的 canonical 顺序）：换个顺序也能读，
 * 但逐字节对拍会红，而"顺序差异"和"分组差异"在回执上长得一样——那种红最难判。
 */
export function renderAliasList(db: DatabaseSync): string {
  const rows = aliasRows(db)
  const lines: string[] = []
  if (rows.length === 0) lines.push('（还没有任何别名）')
  for (const row of rows) lines.push(`${row.kind}\t${row.from} → ${row.to}\t${row.createdAt}\t${row.reason}`)
  for (const kind of ALIAS_KINDS) {
    const froms = (
      db.prepare('SELECT from_name FROM name_aliases WHERE kind=?').all(kind) as { from_name: string }[]
    ).map((row) => String(row.from_name))
    const groups = new Map<string, string[]>()
    for (const from of froms) {
      const canonical = resolveName(db, kind, from).canonical
      const bucket = groups.get(canonical)
      if (bucket === undefined) groups.set(canonical, [from])
      else bucket.push(from)
    }
    for (const [canonical, names] of groups) {
      lines.push(`[${kind}] ${canonical} ← ${[...names].sort().join(', ')}`)
    }
  }
  return lines.join('\n')
}

// ---------------------------------------------------------------- 工作区映射

/** `workspace_map` 一行。 */
export interface WorkspaceMapping {
  readonly pathPrefix: string
  readonly project: string
  readonly note: string
  readonly createdAt: string
}

/** 全部映射（按路径升序）。 */
export function workspaceMappings(db: DatabaseSync): WorkspaceMapping[] {
  const rows = db
    .prepare('SELECT path_prefix, project, note, created_at FROM workspace_map ORDER BY path_prefix')
    .all() as { path_prefix: string; project: string; note: string | null; created_at: string }[]
  return rows.map((row) => ({
    pathPrefix: String(row.path_prefix),
    project: String(row.project),
    note: row.note === null || row.note === undefined ? '' : String(row.note),
    createdAt: String(row.created_at ?? ''),
  }))
}

/** `fish workspace list` 的回执（一行一条 + 缩进备注；不做固定列宽）。 */
export function renderWorkspaceList(db: DatabaseSync): string {
  const rows = workspaceMappings(db)
  if (rows.length === 0) {
    return (
      '还没有任何映射。用 `fish workspace set <路径> <项目>` 添加，' +
      '或 `fish workspace seed` 看看现有记忆里提到哪些工作区。'
    )
  }
  const lines = rows.map((row) => `${row.pathPrefix}  →  ${row.project}${row.note !== '' ? `（${row.note}）` : ''}`)
  lines.push('', `共 ${rows.length} 条映射（路径一律小写/正斜杠/无尾斜杠，匹配不区分大小写）`)
  return lines.join('\n')
}

// ---------------------------------------------------------------- project status

/** 状态表一行（字段名与 `--json` 完全一致：K1 同一形状）。 */
export interface ProjectStatusRow {
  prefix: string
  namespace: string
  status: string
  status_label: string
  last_active: string | null
  last_active_source: string | null
  board_entry: string | null
  board_entry_source: string | null
  owners: string | null
  owners_source: string | null
  open_loops: number | null
  open_loops_source: string | null
  sources: Record<string, string>
}

/** `project_status_rows` 的返回（字段名与生产一致）。 */
export interface ProjectStatusResult {
  readonly as_of: string
  readonly home_root: string
  readonly columns: readonly string[]
  readonly rows: readonly ProjectStatusRow[]
  readonly retired: readonly ProjectStatusRow[]
  readonly retired_total: number
  readonly degraded: readonly { readonly source: string; readonly reason: string }[]
  readonly footnotes: readonly string[]
  readonly non_prefix_dirs: { readonly count: number; readonly names: readonly string[] }
}

/** 状态 → 人读标签；未知取值原样显示（脚注会说明"未登记状态"）。 */
function statusLabel(status: string): string {
  const table: Record<string, string> = { active: '进行中', closed: '已闭合', retired: '已退役' }
  return table[status] ?? (status !== '' ? status : '—')
}

/** 表格单元格消毒：`|` 会破表、换行会断行；`null` 显示空（由调用方决定用哪个 `—`）。 */
function cell(value: unknown): string {
  if (value === null || value === undefined) return ''
  return String(value).replace(/\|/gu, '\\|').replace(/\r/gu, ' ').replace(/\n/gu, ' ').trim()
}

/** 公民 id → 可读短 id（12 位足够在同一份索引里互相区分）。 */
function shortCitizenId(citizenId: string): string {
  const text = String(citizenId ?? '').trim()
  if (text === '') return ''
  return text.length <= 12 ? text : `${text.slice(0, 12)}…`
}

/** citizen_id → display_name（查不到 → `null`）。复用生产的两条既有读点（先按名字、再按 id）。 */
function displayNameOf(db: DatabaseSync, citizenId: string): string | null {
  if (citizenId === '') return null
  const byName = findCitizenByName(db, citizenId)
  if (byName !== null) return byName.display_name
  const byId = db.prepare('SELECT citizen_id,display_name FROM citizens_all WHERE citizen_id=?').get(citizenId) as
    | { display_name?: string }
    | undefined
  return byId === undefined ? null : String(byId.display_name ?? '')
}

/** 一个 attr 名字的展示串（display_name 优先，回落短 id）。 */
function displayOf(db: DatabaseSync, citizenId: string): string {
  const display = (displayNameOf(db, citizenId) ?? '').trim()
  return display !== '' ? display : shortCitizenId(citizenId)
}

/** `recorded_at`（UTC Z）→ as-of 时区里的 `YYYY-MM-DD`。 */
function localDateOf(stamp: string, offsetMinutes: number): string {
  const date = parseStamp(stamp)
  if (date === null) return String(stamp ?? '').slice(0, 10)
  return datePartsInOffset(date, offsetMinutes).ymd
}

/** 项目清单（`code_registry`，排序：active → closed → retired → 未知，再按 prefix）。 */
function registryRows(db: DatabaseSync): { prefix: string; namespace: string; status: string }[] {
  const rows = db
    .prepare(
      "SELECT prefix, namespace, status FROM code_registry ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'closed' THEN 1 WHEN 'retired' THEN 2 ELSE 3 END, prefix",
    )
    .all() as { prefix: string; namespace: string | null; status: string | null }[]
  return rows.map((row) => ({
    prefix: String(row.prefix ?? ''),
    namespace: String(row.namespace ?? ''),
    status: String(row.status ?? ''),
  }))
}

/** 最近活跃：`max(code_index.recorded_at) WHERE project=? AND dormant=0 [AND <= as_of]`。 */
function lastActiveOf(db: DatabaseSync, prefix: string, asOf: ProjectAsOf): [string, string] {
  let sql = 'SELECT MAX(recorded_at) AS v FROM code_index WHERE project=? AND dormant=0'
  const params: string[] = [prefix]
  sql += ' AND recorded_at<=?'
  params.push(utcStamp(asOf.date))
  const row = db.prepare(sql).get(...params) as { v: string | null } | undefined
  const value = row?.v ?? null
  if (value === null || value === '') return [PST_DASH_NO_MEMORY, 'code_index.recorded_at(无记录)']
  return [localDateOf(String(value), asOf.offsetMinutes), 'code_index.recorded_at(max)']
}

/** 看板入口：优先"as-of 当日的项目板"，否则"最近一条 open 板" → `(相对链接, 来源)`。 */
function boardEntryOf(
  db: DatabaseSync,
  prefix: string,
  asOf: ProjectAsOf,
  homeRoot: string | null,
): [string, string] {
  const parts = datePartsInOffset(asOf.date, asOf.offsetMinutes)
  const periodKey = parts.period
  let row = db
    .prepare(
      "SELECT path, period_key FROM boards WHERE scope='project' AND scope_key=? AND period_key=? ORDER BY id DESC LIMIT 1",
    )
    .get(prefix, periodKey) as { path: string | null } | undefined
  let source = `boards.path(period=${periodKey})`
  if (row === undefined) {
    row = db
      .prepare(
        "SELECT path, period_key FROM boards WHERE scope='project' AND scope_key=? AND status='open' ORDER BY period_key DESC, id DESC LIMIT 1",
      )
      .get(prefix) as { path: string | null } | undefined
    source = 'boards.path(最近 open 板)'
  }
  if (row === undefined) return [PST_DASH_NO_BOARD, 'boards(无板)']
  const relPath = String(row.path ?? '')
  const name = path.basename(relPath)
  let rel: string
  if (homeRoot !== null) {
    const target = path.join(homeRoot, relPath)
    const base = path.join(homeRoot, '10-项目库')
    rel = path.relative(base, target).split(path.sep).join('/')
  } else {
    rel = relPath.startsWith('10-项目库/') ? relPath.slice('10-项目库/'.length) : relPath
  }
  return [`[${name}](${rel})`, source]
}

/** 主责 AI（开档期优先 → 近 7 天 owner 回退 → `—（无开档期）`）。 */
function ownersOf(db: DatabaseSync, prefix: string, asOf: ProjectAsOf): [string, string] {
  const stints = db
    .prepare(
      'SELECT citizen_id, MIN(from_ts) AS first FROM project_stints WHERE project=? AND to_ts IS NULL GROUP BY citizen_id ORDER BY first ASC, citizen_id ASC',
    )
    .all(prefix) as { citizen_id: string }[]
  const names = stints.map((row) => displayOf(db, String(row.citizen_id)))
  if (names.length > 0) {
    if (names.length === 1) return [names[0] as string, 'project_stints(开档期)']
    return [`${names.slice(0, 2).join('、')}等 ${names.length} 人`, 'project_stints(开档期)']
  }
  const sinceMs = asOf.date.getTime() - PST_OWNER_FALLBACK_DAYS * 86400000
  const since = utcStamp(new Date(sinceMs))
  const recent = db
    .prepare(
      "SELECT owner, MAX(recorded_at) AS last FROM code_index WHERE project=? AND recorded_at>=? AND owner IS NOT NULL AND TRIM(owner)<>'' GROUP BY owner ORDER BY last DESC, owner ASC LIMIT 2",
    )
    .all(prefix, since) as { owner: string }[]
  const recentNames = recent.map((row) => displayOf(db, String(row.owner)))
  if (recentNames.length > 0) return [recentNames.join('、'), 'code_index.owner(recent_owner)']
  return [PST_DASH_NO_OWNER, 'none']
}

/** 未了事项（窗口内命中 8 词词表的条数）→ `(n, 来源)`。 */
function openLoopsOf(db: DatabaseSync, prefix: string, asOf: ProjectAsOf): [number, string] {
  const sinceMs = asOf.date.getTime() - PST_OPEN_WINDOW_DAYS * 86400000
  const since = utcStamp(new Date(sinceMs))
  // 探一次表：把"表坏了"显式抛给按列降级，而不是显示成 0 条。
  db.prepare('SELECT 1 FROM code_index LIMIT 1').all()
  const rows = openLoopRows(db, since, 10000, new Set<string>(), prefix)
  return [rows.length, 'code_index.title(8 词表)']
}

/** 正文目录里"不属于任何 registry 前缀"的一级目录（只计数不列表）。 */
function nonPrefixDirs(db: DatabaseSync, dataRoot: string): { count: number; names: string[] } {
  const base = path.join(dataRoot, 'projects')
  if (pathExistsDir(base) === false) return { count: 0, names: [] }
  let prefixes: Set<string>
  try {
    prefixes = new Set(
      (db.prepare('SELECT prefix FROM code_registry').all() as { prefix: string }[]).map((row) => String(row.prefix)),
    )
  } catch {
    return { count: 0, names: [] }
  }
  const extra = listDirNames(base)
    .filter((name) => !prefixes.has(name))
    .sort()
  return { count: extra.length, names: extra }
}

/** 目录存在且是目录？（读不到一律当"没有"）。 */
function pathExistsDir(target: string): boolean {
  try {
    return statSync(target).isDirectory()
  } catch {
    return false
  }
}

/** 目录下的名字（读不到 → 空）。 */
function listDirNames(target: string): string[] {
  try {
    return readdirSync(target)
  } catch {
    return []
  }
}

/**
 * 六列 + 每列来源 + 降级清单（`project_status_rows` 的同形复刻）。
 *
 * 降级的粒度是**列**而不是整表：某一张表的列坏了，只该让那一格变 `—（数据源不可用）`，
 * 其余五格照样有值——否则一次局部故障会让整张表看起来像"没有项目"。
 */
export function projectStatusRows(
  db: DatabaseSync,
  options: {
    readonly dataRoot: string
    readonly asOf: ProjectAsOf
    readonly homeRoot?: string | null
    readonly includeRetired?: boolean
  },
): ProjectStatusResult {
  const asOf = options.asOf
  const homeRoot = options.homeRoot ?? defaultHomeRoot(options.dataRoot)
  const degraded: { source: string; reason: string }[] = []
  const rows: ProjectStatusRow[] = []
  let registry: { prefix: string; namespace: string; status: string }[] = []
  try {
    registry = registryRows(db)
  } catch (error) {
    degraded.push({ source: 'code_registry', reason: describeError(error) })
    registry = []
  }
  for (const item of registry) {
    const row: ProjectStatusRow = {
      prefix: item.prefix,
      namespace: item.namespace,
      status: item.status,
      status_label: statusLabel(item.status),
      last_active: null,
      last_active_source: null,
      board_entry: null,
      board_entry_source: null,
      owners: null,
      owners_source: null,
      open_loops: null,
      open_loops_source: null,
      sources: {},
    }
    if (!['active', 'closed', 'retired'].includes(item.status)) {
      row.status_label = `${item.status !== '' ? item.status : '—'}（未登记状态）`
    }
    const columns: { key: 'last_active' | 'board_entry' | 'owners' | 'open_loops'; run: () => [unknown, string] }[] = [
      { key: 'last_active', run: () => lastActiveOf(db, item.prefix, asOf) },
      { key: 'board_entry', run: () => boardEntryOf(db, item.prefix, asOf, homeRoot) },
      { key: 'owners', run: () => ownersOf(db, item.prefix, asOf) },
      { key: 'open_loops', run: () => openLoopsOf(db, item.prefix, asOf) },
    ]
    for (const column of columns) {
      try {
        const [value, source] = column.run()
        if (column.key === 'open_loops') row.open_loops = Number(value)
        else if (column.key === 'last_active') row.last_active = String(value)
        else if (column.key === 'board_entry') row.board_entry = String(value)
        else row.owners = String(value)
        ;(row as unknown as Record<string, unknown>)[`${column.key}_source`] = source
        row.sources[column.key] = source
      } catch (error) {
        degraded.push({ source: `${column.key}:${item.prefix}`, reason: describeError(error) })
        if (column.key === 'open_loops') row.open_loops = null
        else (row as unknown as Record<string, unknown>)[column.key] = PST_DASH_UNAVAILABLE
        ;(row as unknown as Record<string, unknown>)[`${column.key}_source`] = 'unavailable'
        row.sources[column.key] = 'unavailable'
      }
    }
    row.sources.status = 'code_registry.status'
    rows.push(row)
  }
  const retiredAll = rows.filter((row) => row.status === 'retired')
  const includeRetired = options.includeRetired === true
  const visible = includeRetired ? rows : rows.filter((row) => row.status !== 'retired')
  const retired = includeRetired ? [] : retiredAll
  let nonPrefix: { count: number; names: string[] }
  try {
    nonPrefix = nonPrefixDirs(db, options.dataRoot)
  } catch (error) {
    degraded.push({ source: 'data_root/projects', reason: describeError(error) })
    nonPrefix = { count: 0, names: [] }
  }
  const keywords = ATTENTION_OPEN_KEYWORDS.join('、')
  const footnotes = [
    `窗口：最近活跃与未了事项都只看 \`as-of\` 前 ${PST_OPEN_WINDOW_DAYS} 天（与注入包的 \`ATTENTION_OPEN_WINDOW_DAYS\` 同口径）。`,
    '`—（无记忆）` = 该项目在 `code_index` 里没有任何 active 记忆；' +
      '`—（今日无看板）` = 今日没有开过板（**不等于**没人干活，可能只是没签到）；' +
      '`—（无开档期）` = 既无开档期、也没有近 7 天的记忆；`—` = 未了事项为 0。',
    `主责 AI 口径（Q4）：① 该项目当前开档期（\`project_stints.to_ts IS NULL\`）的公民，` +
      `多名列前 2 + 「等 N 人」；② 无开档期时回退近 ${PST_OWNER_FALLBACK_DAYS} 天内` +
      `该项目记忆的 owner，并在来源里标 \`recent_owner\`；③ 都无 → \`—（无开档期）\`。`,
    `未了事项词表（${ATTENTION_OPEN_KEYWORDS.length} 个）：${keywords}。`,
    '项目清单以 `code_registry` 为准（裁决 Q6）。',
    `正文目录里不属于任何前缀的目录：${nonPrefix.count} 个` +
      (nonPrefix.count > 0
        ? `（${nonPrefix.names.slice(0, 5).join(', ')}${nonPrefix.count > 5 ? '…' : ''}）`
        : '') +
      '——只计数不列表（裁决 Q6/Q18）。',
    '本页是投影：真相在 `data_root/index.sqlite`，重建命令 `fish project status`；' +
      '同一 `--as-of` 重渲染逐字节一致（as-of 行除外）。',
  ]
  return {
    as_of: asOfIso(asOf),
    home_root: homeRoot,
    columns: [...PST_COLUMNS],
    rows: visible,
    retired,
    retired_total: retiredAll.length,
    degraded,
    footnotes,
    non_prefix_dirs: nonPrefix,
  }
}

/** 错误一行摘要（`_exc_text`：`TypeName: message`）。 */
function describeError(error: unknown): string {
  if (error instanceof Error) return `${error.name}: ${error.message}`
  return String(error)
}

/**
 * 门面根解析的**只读子集**（`resolve_home_root` 的规则②/③）。
 *
 * 为什么只复刻两条：①/⓪ 是"显式指定"（影子期没有 `--home`，也不该替人改落点）；
 * ②/③ 才是"没指定时落在哪"——项目状态表的相对链接要靠它，否则 `board_entry` 会算错基准。
 */
export function defaultHomeRoot(dataRoot: string): string {
  const base = path.resolve(dataRoot)
  const name = path.basename(base)
  const parent = path.dirname(base)
  if (name === 'data' && path.basename(parent) === 'p0') return path.join(path.dirname(parent), 'fish-home')
  return path.join(parent, 'fish-home')
}

/** 状态表单元格：`null`/空串 → `—`；0 条未了事项 → `—`（显式，不写 0）。 */
function pstCell(value: unknown): string {
  if (value === null || value === undefined || value === '') return PST_DASH_NONE
  return cell(value)
}

/** 六列表的 markdown 渲染（`_pst_render_table` 的同形复刻）。 */
export function renderProjectStatusTable(rows: readonly ProjectStatusRow[]): string[] {
  const lines = [`| ${PST_COLUMNS.join(' | ')} |`, `|${'---|'.repeat(PST_COLUMNS.length)}`]
  if (rows.length === 0) {
    lines.push('| —（registry 里没有项目） | — | — | — | — | — |')
    return lines
  }
  for (const row of rows) {
    const base = cell(row.namespace !== '' ? row.namespace : row.prefix) || '（未命名）'
    const name = row.namespace !== '' ? `${base}（\`${cell(row.prefix)}\`）` : `\`${cell(row.prefix)}\``
    let loopsCell = row.open_loops === null || row.open_loops === 0 ? PST_DASH_NONE : String(row.open_loops)
    if (row.open_loops === null) loopsCell = PST_DASH_UNAVAILABLE
    lines.push(
      `| ${name} | ${cell(row.status_label) || PST_DASH_NONE} ` +
        `| ${pstCell(row.last_active)} ` +
        `| ${row.board_entry !== null && row.board_entry !== '' ? row.board_entry : PST_DASH_NO_BOARD} ` +
        `| ${pstCell(row.owners)} ` +
        `| ${loopsCell} |`,
    )
  }
  return lines
}

/**
 * `fish project status` 的**只读**回执：同一份标题/表格/降级段/脚注/折叠区，
 * 但**不写文件**，末尾明写"终端里会写到哪"。
 */
export function renderProjectStatus(
  result: ProjectStatusResult,
  options: { readonly includeRetired?: boolean } = {},
): string {
  const includeRetired = options.includeRetired === true
  const lines = [
    '# 项目状态表',
    '',
    `> 生成器：\`fish project status\` ｜ as-of ${result.as_of}`,
    '> 每格的取值来源见 `--json` 的 `sources`；本页是投影，真相在 `data_root/index.sqlite`。',
    '> 六列严格顺序：项目 / 状态 / 最近活跃 / 看板入口 / 主责 AI / 未了事项。',
    '',
    ...renderProjectStatusTable(result.rows),
    '',
    `主表 ${result.rows.length} 个项目` + (includeRetired ? '（含 retired，`--include-retired`）' : ''),
  ]
  for (const item of result.degraded) {
    lines.push('', `## 数据源不可用：${item.source}`, '', `原因：\`${item.reason}\``, '该格显示 `—（数据源不可用）`，**不是 0**。修好数据源后重跑 `fish project status`。')
  }
  lines.push('', '## 脚注', '')
  for (const note of result.footnotes) lines.push(`- ${note}`)
  if (result.retired.length > 0) {
    lines.push('', `<details><summary>已退役（${result.retired.length}）</summary>`, '', ...renderProjectStatusTable(result.retired), '', '</details>', '')
  }
  lines.push(
    '',
    `（影子期不落盘：终端里 \`fish project status\` 会把同一份内容写到 \`${result.home_root}/${LAYOUT_STATUS_TABLE}\`；` +
      '这里只读，一个文件都没写。要机器可比的形状请加 `--json`。）',
  )
  return lines.join('\n')
}

/** `project status --json` 的载荷（**字段顺序**与生产 `json.dumps` 一致）。 */
export function projectStatusPayload(result: ProjectStatusResult): Record<string, unknown> {
  return {
    as_of: result.as_of,
    home_root: result.home_root,
    columns: result.columns,
    rows: result.rows,
    retired: result.retired,
    degraded: result.degraded,
    footnotes: result.footnotes,
    retired_total: result.retired_total,
  }
}
