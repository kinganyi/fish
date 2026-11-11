/**
 * 名字解析（项目别名 / 会话档期 / 工作区映射）。
 *
 * 依据（逐条对齐 Python）：
 *   · `_alias_map` / `_walk_alias` / `resolve_name` / `resolve_project_names`（fish.py:3089 / 3107 / 3145）
 *   · `normalize_workspace_path` / `resolve_project_by_cwd`（fish.py / 5464）
 *   · `current_open_stints` / `current_open_stint`（fish.py:5740）
 *   · `find_citizen_by_name`（fish.py:4514）
 *
 * 为什么别名要单独成模块：别名只影响**检索与展示**，不改写入归属（用户 2026-10-25 拍板）。
 * 把"展开名字"这件事收在一处，写侧才不会顺手也调它——那是这条约定唯一可能被破坏的方式。
 *
 * @module dsh-fish/domain/names
 */

import type { DatabaseSync } from 'node:sqlite'

/** 别名种类（Python `_ALIAS_KINDS`）。 */
export const ALIAS_KINDS = ['project', 'citizen'] as const
export type AliasKind = (typeof ALIAS_KINDS)[number]

/** 别名链最多走几跳（Python `_ALIAS_MAX_HOPS`）。 */
export const ALIAS_MAX_HOPS = 8

/** 一个名字的解析结果（形状对齐 `resolve_name` 的返回 dict）。 */
export interface NameResolution {
  readonly input: string
  readonly canonical: string
  readonly aliases: readonly string[]
  readonly chain: readonly string[]
  readonly status: string | null
  readonly found: boolean
}

/** 一次读出该 kind 的全部别名（from → to）。行数是个位数，不做增量查询。 */
export function aliasMap(db: DatabaseSync, kind: AliasKind): Map<string, string> {
  const rows = db.prepare('SELECT from_name, to_name FROM name_aliases WHERE kind=?').all(kind) as {
    from_name: string
    to_name: string
  }[]
  const out = new Map<string, string>()
  for (const row of rows) out.set(row.from_name, row.to_name)
  return out
}

/** 沿别名走到规范名；成环时停在环入口（**不抛错**：读要永远能出结果）。 */
export function walkAlias(amap: ReadonlyMap<string, string>, start: string): { canonical: string; chain: string[] } {
  const chain = [start]
  let cur = start
  for (let hop = 0; hop < ALIAS_MAX_HOPS; hop += 1) {
    const next = amap.get(cur)
    if (next === undefined || next === '' || chain.includes(next)) break
    chain.push(next)
    cur = next
  }
  return { canonical: cur, chain }
}

/**
 * 把一个名字解析成规范名（复刻 `resolve_name`）。
 * @throws FishError `CONTRACT_INVALID` —— kind 非法
 */
export function resolveName(db: DatabaseSync, kind: AliasKind, value: string | null | undefined): NameResolution {
  if (!(ALIAS_KINDS as readonly string[]).includes(kind)) {
    throw new Error(`kind 只能是 ${ALIAS_KINDS.join('/')}，收到 ${String(kind)}`)
  }
  const start = (value ?? '').trim()
  const empty: NameResolution = { input: start, canonical: '', aliases: [], chain: [], status: null, found: false }
  if (start === '') return empty
  const amap = aliasMap(db, kind)
  const { canonical, chain } = walkAlias(amap, start)
  const aliases = [...amap.keys()]
    .filter((from) => from !== canonical && walkAlias(amap, from).canonical === canonical)
    .sort()
  let status: string | null = null
  let found = false
  const row =
    kind === 'project'
      ? (db.prepare('SELECT status FROM code_registry WHERE prefix=?').get(canonical) as { status: string } | undefined)
      : (db
          .prepare(
            "SELECT status FROM citizens WHERE display_name=? AND status='active' ORDER BY created_at, citizen_id LIMIT 1",
          )
          .get(canonical) as { status: string } | undefined)
  if (row !== undefined) {
    found = true
    status = row.status
  }
  return { input: start, canonical, aliases, chain, status, found }
}

/**
 * 给定一个项目名，返回"同一个项目"的全部名字（规范名 + 别名，按 `resolve_name` 次序）。
 *
 * 这是别名机制对检索的唯一承诺：`recall(project='cdql')` 也要看到 `CDQL` 的记忆。
 * 返回空数组 = 输入为空（调用方据此决定"没给"还是"查不到"）。
 */
export function resolveProjectNames(db: DatabaseSync, project: string | null | undefined): string[] {
  const p = (project ?? '').trim()
  if (p === '') return []
  const info = resolveName(db, 'project', p)
  const out: string[] = []
  for (const name of [info.canonical, ...info.aliases]) {
    if (name !== '' && !out.includes(name)) out.push(name)
  }
  return out.length > 0 ? out : [p]
}

/**
 * 工作区路径归一（复刻 `normalize_workspace_path`）：正斜杠 · 无尾斜杠 · 全小写。
 *
 * 三个平台事实（写在 Python docstring 里，这里照搬）：
 *   · Windows 上 `\` 与 `/` 混用是常态，不统一就会"明明映射了却匹配不上"；
 *   · 尾斜杠差一个字符就漏（前缀匹配是字符串操作），根路径保留最小形态；
 *   · Windows 路径比较不区分大小写（POSIX 上这条会误合并，已声明为平台取舍）。
 */
export function normalizeWorkspacePath(target: string | null | undefined): string {
  if (target === null || target === undefined) return ''
  const raw = String(target).trim()
  if (raw === '') return ''
  let s = raw.replace(/\\/g, '/').replace(/\/+/g, '/')
  s = posixNormalize(s)
  while (s.length > 1 && s.endsWith('/')) s = s.slice(0, -1)
  // 盘符根：`c:` → `c:/`（posixpath.normpath 会把 `c:/` 塌成 `c:`）。
  if (/^[A-Za-z]:$/.test(s)) s = `${s}/`
  return s.toLowerCase()
}

/**
 * POSIX 风格的 `normpath`（只实现路径归一必需的那部分）。
 *
 * 为什么不用 `node:path`：`path.posix.normalize` 与本函数的行为差异点在"盘符根"与
 * "前导 `//`"，而 Windows 上 `path.win32` 又会把 `/` 转回 `\`——归一化白做。
 */
function posixNormalize(input: string): string {
  const absolute = input.startsWith('/')
  const parts = input.split('/')
  const out: string[] = []
  for (const part of parts) {
    if (part === '' || part === '.') continue
    if (part === '..') {
      if (out.length > 0 && out[out.length - 1] !== '..') out.pop()
      else if (!absolute) out.push('..')
      continue
    }
    out.push(part)
  }
  const joined = `${absolute ? '/' : ''}${out.join('/')}`
  return joined === '' ? '.' : joined
}

/**
 * 按**最长前缀 + 路径分量边界**把工作区路径解析成项目前缀；没有匹配返回 `null`。
 *
 * 为什么不按"包含"匹配：`/a/b` 是 `/a/bc` 的字符串前缀但不是它的父路径，必须按分量边界判。
 * 为什么全表扫：`workspace_map` 是按项目维护的配置表（行数是个位数），
 * 而用 SQL LIKE 就得转义 `_` / `%`——转义写错是**静默错配**，最难查的那类 bug。
 */
export function resolveProjectByCwd(db: DatabaseSync, cwd: string | null | undefined): string | null {
  const target = normalizeWorkspacePath(cwd)
  if (target === '') return null
  const rows = db.prepare('SELECT path_prefix, project FROM workspace_map').all() as {
    path_prefix: string
    project: string
  }[]
  let best: { length: number; project: string } | null = null
  for (const row of rows) {
    const norm = normalizeWorkspacePath(row.path_prefix)
    if (norm === '') continue
    if (target !== norm && !target.startsWith(`${norm}/`)) continue
    const candidate = { length: norm.length, project: row.project }
    if (
      best === null ||
      candidate.length > best.length ||
      (candidate.length === best.length && candidate.project < best.project)
    ) {
      best = candidate
    }
  }
  return best === null ? null : best.project
}

/** 一个开着的项目档期（形状对齐 `_STINT_READ_FIELDS`）。 */
export interface ProjectStint {
  readonly id: number
  readonly citizen_id: string
  readonly account_id: string | null
  readonly project: string
  readonly from_ts: string
  readonly set_by: string | null
}

/** 当前所有开档期（按 `(from_ts, id)` 升序；复刻 `current_open_stints`）。 */
export function currentOpenStints(db: DatabaseSync, citizenId: string): ProjectStint[] {
  return db
    .prepare(
      'SELECT id,citizen_id,account_id,project,from_ts,set_by FROM project_stints ' +
        'WHERE citizen_id=? AND to_ts IS NULL ORDER BY from_ts ASC, id ASC',
    )
    .all(citizenId) as unknown as ProjectStint[]
}

/** 当前开档期里**最近开的**那一个（多项目时的"主项目"）；没有返回 `null`。 */
export function currentOpenStint(db: DatabaseSync, citizenId: string): ProjectStint | null {
  const stints = currentOpenStints(db, citizenId)
  return stints.length === 0 ? null : (stints[stints.length - 1] as ProjectStint)
}

/** 按 display_name 查活跃公民（取最早创建的那条，结果确定）。 */
export function findCitizenByName(
  db: DatabaseSync,
  name: string | null | undefined,
): { citizen_id: string; display_name: string; kind: string; role: string | null; host: string | null } | null {
  const key = (name ?? '').trim()
  if (key === '') return null
  const row = db
    .prepare(
      "SELECT citizen_id,display_name,kind,role,host FROM citizens WHERE display_name=? AND status='active' " +
        'ORDER BY created_at, citizen_id LIMIT 1',
    )
    .get(key) as
    | { citizen_id: string; display_name: string; kind: string; role: string | null; host: string | null }
    | undefined
  return row ?? null
}
