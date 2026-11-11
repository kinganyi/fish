/**
 * 记忆检索（只读）：`search_memories` / `recent_fallback` / 列出模式 / 回执渲染。
 *
 * 依据（逐条对齐 Python）：
 *   · `search_memories`（fish.py:3941）——CLI 与 MCP **共用的唯一读入口**（ADR-005）
 *   · `_like_literal` / `LIKE_ESCAPE_SQL` / `tag_like_pattern`（fish.py:4057 / 4076 / 4079）
 *   · `_list_where` / `count_memories` / `list_memories` / `format_list_declaration`（fish.py:4099 / 4120 / 4131 / 4155）
 *   · `recent_fallback`（fish.py:4190）——未命中回退
 *   · `RECALL_DIMENSION_NOTE` / `format_status_tag`（fish.py:4182 / 4185）
 *   · MCP `tool_recall` 的回执文本（mcp_server.py:819）
 *
 * ## 三条纪律（都在 Python 里付过学费）
 *
 * 1. **过滤与 MATCH 同一次下推**：旧写法先 `LIMIT n` 取候选再按 status 过滤，于是
 *    "最早那几条命中恰好都已 superseded"的查询会被整体滤空——记忆明明在，却查不到。
 * 2. **项目过滤展开别名**，并且同一份名字集合必须用在**两处**（FTS 下推 + 回取行），
 *    两处用不同集合 = 结果忽多忽少且不报错。
 * 3. **回退列表遵守同样的过滤**：`recent_fallback` 曾经取全库最近 5 条，跨项目串台。
 *
 * @module dsh-fish/domain/memory
 */

import type { DatabaseSync, SQLInputValue } from 'node:sqlite'

import { FishError } from '../core/errors.ts'
import { cutTokens } from '../layout/cut.ts'
import { appendIdentityHint, IDENTITY_ANONYMOUS, type Identity } from './identity.ts'
import { resolveProjectNames } from './names.ts'

/** LIKE 的转义声明（必须与 `likeLiteral` 成对出现，否则反斜杠被当字面量）。 */
export const LIKE_ESCAPE_SQL = " ESCAPE '\\'"

/** 列出模式的"匹配来源"列：告诉读者这条不是搜到的，是列出来的。 */
export const LIST_SOURCE = '列出'

/** 列出模式的默认条数（比检索的 10 宽）。 */
export const LIST_DEFAULT_LIMIT = 20

/** `tags` 单独用时的引导语（MCP 与 CLI 共用一处）。 */
export const TAGS_ONLY_HINT =
  'tags 只作附加过滤；请加 project 限定' + '（例：recall(project="anyi-studio", tags=["fish-feedback"])）'

/** 检索结果第一行的维度说明（MCP 与 CLI 共用）。 */
export const RECALL_DIMENSION_NOTE = '说明：status 与 dormant 是两个维度（dormant=衰减休眠，可被召回唤醒）'

/** 检索结果第二行：告诉读者这是关键词匹配，不是语义检索。 */
export const RECALL_KEYWORD_NOTE = '（关键词匹配，不是语义检索；同义词查不到——见 ADR-006）'

/** 检索范围。 */
export type RecallScope = 'all' | 'title' | 'body'

/** `search_memories` 的入参。 */
export interface SearchMemoriesOptions {
  readonly query?: string | null
  readonly code?: string | null
  readonly project?: string | null
  readonly tags?: string | readonly string[] | null
  readonly scope?: RecallScope | string
  readonly limit?: number
  readonly includeSuperseded?: boolean
}

/** 一行检索结果：`(code, title, project, mem_type, status, file_path, dormant, 匹配来源)`。 */
export type MemoryRow = readonly [
  code: string,
  title: string,
  project: string | null,
  memType: string,
  status: string,
  filePath: string | null,
  dormant: number,
  source: string,
]

/**
 * SQLite 行（按列名取）。
 *
 * 为什么不用位置下标：`StatementSync.all()` 返回的是**对象**，不是数组。
 * 第一版按 Python 的元组下标写（`row[0]`），于是每个字段都是 `undefined`——
 * 命中的行全被丢进 `found` 的 `undefined` 键下，检索永远返回空。
 * 那不是"查不到"，是"查到了但读不出来"，只有对拍能发现（单测只测到无异常）。
 */
interface RowFields {
  readonly code: string
  readonly title: string | null
  readonly project: string | null
  readonly mem_type: string | null
  readonly status: string | null
  readonly file_path: string | null
  readonly dormant: number | null
}

/** LIKE 的**字面量**转义：`\` / `%` / `_`（标签是用户数据，通配符会查错却查到）。 */
export function likeLiteral(value: unknown): string {
  const text = value === null || value === undefined ? '' : String(value)
  return text.replace(/\\/g, '\\\\').replace(/%/g, '\\%').replace(/_/g, '\\_')
}

/**
 * 标签过滤的 LIKE 模式：`%"<转义后的标签>"%`。
 *
 * **两层转义，顺序不能反**：① 先过 JSON（库里的文本是 `json.dumps` 的产物，
 * 标签里的 `\` 与 `"` 已被 JSON 转义过一次）；② 再过 LIKE（JSON 吐出来的反斜杠
 * 在 LIKE 里必须再转义一次）。先 LIKE 后 JSON 等于把①的产物当用户数据再转义一遍。
 */
export function tagLikePattern(tag: unknown): string {
  const text = tag === null || tag === undefined ? '' : String(tag)
  const jsonLiteral = JSON.stringify(text).slice(1, -1)
  return `%"${likeLiteral(jsonLiteral)}"%`
}

/** 归一 `tags` 入参：字符串视为单元素，过滤空值。 */
function normalizeTags(tags: string | readonly string[] | null | undefined): string[] {
  if (tags === null || tags === undefined) return []
  const list = typeof tags === 'string' ? [tags] : tags
  return list.filter((tag): tag is string => typeof tag === 'string' && tag !== '')
}

/**
 * 检索记忆（复刻 `search_memories`）。
 *
 * 排序优先级：精确编码 > 编码片段 > 标题命中 > 正文命中。
 * 返回的 `rows` 已按 `order` 去重并截到 `limit`；`sources` 是 code → 匹配来源。
 */
export function searchMemories(
  db: DatabaseSync,
  options: SearchMemoriesOptions = {},
): { rows: MemoryRow[]; sources: Record<string, string> } {
  const limit = Math.max(1, Math.trunc(options.limit ?? 10))
  const order: string[] = []
  const sources: Record<string, string> = {}
  const add = (codes: Iterable<string>, source: string): void => {
    for (const code of codes) {
      if (code !== '' && code !== undefined && code !== null && sources[code] === undefined) {
        sources[code] = source
        order.push(code)
      }
    }
  }

  const query = (options.query ?? '').trim()
  const code = options.code ?? null
  const project = options.project ?? null
  const includeSuperseded = options.includeSuperseded === true
  const scope = options.scope ?? 'all'

  const projectNames = project !== null && project !== undefined && project !== '' ? resolveProjectNames(db, project) : []
  const filters: string[] = []
  const filterParams: SQLInputValue[] = []
  if (!includeSuperseded) filters.push("ci.status = 'active'")
  if (project !== null && project !== undefined && project !== '') {
    filters.push(`ci.project IN (${projectNames.map(() => '?').join(',')})`)
    filterParams.push(...projectNames)
  }
  for (const tag of normalizeTags(options.tags)) {
    filters.push(`ci.tags LIKE ?${LIKE_ESCAPE_SQL}`)
    filterParams.push(tagLikePattern(tag))
  }
  const where = filters.length > 0 ? ` AND ${filters.join(' AND ')}` : ''
  // FTS 命中的取数与 Python **语义完全一致**（先 MATCH、再 JOIN、再下推过滤、最后 LIMIT），
  // 但 SQL 形态不能照抄：Python 写的是 `FROM memory_fts f JOIN code_index ci … WHERE memory_fts MATCH ?`，
  // 即**用真表名在一个带别名的表上做 MATCH**。老版本 SQLite（Python 3.11 自带）容忍这种写法，
  // Node 24 自带的 SQLite 直接报 `column index out of range`（实测）。
  // 所以这里改成子查询形态：MATCH 在子查询里完成，过滤与 LIMIT 与外层同一条语句。
  // ——判据是**结果集**而不是 SQL 文本；两边的差异必须在注释里说清，否则下一个人会"修"回去。
  const ftsSql =
    'SELECT f.code AS code FROM (SELECT code FROM memory_fts WHERE memory_fts MATCH ?) AS f ' +
    `JOIN code_index ci ON ci.code = f.code WHERE 1=1${where} LIMIT ?`

  if (code !== null && code !== '') {
    const rows = db.prepare('SELECT code FROM code_index WHERE code=?').all(code) as { code: string }[]
    add(
      rows.map((row) => row.code),
      '编码',
    )
  }

  if (query !== '') {
    const exact = db.prepare('SELECT code FROM code_index WHERE code=?').all(query) as { code: string }[]
    add(
      exact.map((row) => row.code),
      '编码',
    )
    const partial = db
      .prepare('SELECT code FROM code_index WHERE code LIKE ? ORDER BY code DESC LIMIT ?')
      .all(`%${query}%`, limit) as { code: string }[]
    add(
      partial.map((row) => row.code),
      '编码',
    )
    const expr = ftsMatch(query)
    if (expr !== null) {
      if (scope === 'all' || scope === 'title') {
        const rows = db
          .prepare(ftsSql)
          .all(`title_idx:(${expr})`, ...filterParams, limit) as { code: string }[]
        add(
          rows.map((row) => row.code),
          '标题',
        )
      }
      if (scope === 'all' || scope === 'body') {
        const rows = db
          .prepare(ftsSql)
          .all(`body_idx:(${expr})`, ...filterParams, limit) as { code: string }[]
        add(
          rows.map((row) => row.code),
          '正文',
        )
      }
    }
  }

  if (order.length === 0) return { rows: [], sources: {} }

  let sql =
    'SELECT code,title,project,mem_type,status,file_path,dormant FROM code_index ' +
    `WHERE code IN (${order.map(() => '?').join(',')})`
  const params: SQLInputValue[] = [...order]
  if (!includeSuperseded) sql += " AND status = 'active'"
  if (project !== null && project !== undefined && project !== '') {
    sql += ` AND project IN (${projectNames.map(() => '?').join(',')})`
    params.push(...projectNames)
  }
  for (const tag of normalizeTags(options.tags)) {
    sql += ` AND tags LIKE ?${LIKE_ESCAPE_SQL}`
    params.push(tagLikePattern(tag))
  }

  const found = new Map<string, RowFields>()
  for (const row of db.prepare(sql).all(...params) as unknown as RowFields[]) {
    found.set(row.code, row)
  }
  const rows: MemoryRow[] = []
  for (const codeInOrder of order) {
    const row = found.get(codeInOrder)
    if (row === undefined) continue
    rows.push([
      row.code,
      row.title ?? '',
      row.project ?? null,
      row.mem_type ?? '',
      row.status ?? '',
      row.file_path ?? null,
      Number(row.dormant ?? 0),
      sources[codeInOrder] ?? '',
    ])
    if (rows.length >= limit) break
  }
  return { rows, sources }
}

/**
 * 查询串 → FTS5 MATCH 表达式；没有可查 token 时 `null`。
 *
 * 切词复用 `src/layout/cut.ts`（与 Python `index_tokens` 逐 token 等价，已对拍 50/50），
 * 这里只负责"每个 token 加双引号"——不加的话 `CR-12` 会被 FTS5 解析成「CR 列包含 12」。
 */
function ftsMatch(query: string): string | null {
  const tokens = cutTokens(query).filter((token) => token !== '')
  if (tokens.length === 0) return null
  return tokens.map((token) => `"${token.replace(/"/g, '""')}"`).join(' AND ')
}

/**
 * 未命中时给"最近的条目"作提示——**必须遵守与检索同样的过滤**（复刻 `recent_fallback`）。
 */
export function recentFallback(
  db: DatabaseSync,
  options: {
    project?: string | null
    tags?: string | readonly string[] | null
    limit?: number
    includeSuperseded?: boolean
  } = {},
): Array<{ code: string; title: string }> {
  const limit = Math.max(1, Math.trunc(options.limit ?? 5))
  let sql = 'SELECT code,title FROM code_index'
  const where: string[] = []
  const params: SQLInputValue[] = []
  if (options.includeSuperseded !== true) where.push("status = 'active'")
  const project = options.project ?? null
  if (project !== null && project !== undefined && project !== '') {
    const names = resolveProjectNames(db, project)
    if (names.length <= 1) {
      where.push('project = ?')
      params.push(names.length === 1 ? (names[0] ?? project) : project)
    } else {
      where.push(`project IN (${names.map(() => '?').join(',')})`)
      params.push(...names)
    }
  }
  for (const tag of normalizeTags(options.tags)) {
    where.push(`tags LIKE ?${LIKE_ESCAPE_SQL}`)
    params.push(tagLikePattern(tag))
  }
  if (where.length > 0) sql += ` WHERE ${where.join(' AND ')}`
  sql += ' ORDER BY code DESC LIMIT ?'
  params.push(limit)
  return db.prepare(sql).all(...params) as Array<{ code: string; title: string }>
}

/** 列出模式的 WHERE 子句（`count_memories` 与 `list_memories` 共用同一谓词）。 */
function listWhere(
  db: DatabaseSync,
  options: { project?: string | null; tags?: string | readonly string[] | null; includeSuperseded?: boolean },
): { clause: string; params: SQLInputValue[] } {
  const where: string[] = []
  const params: SQLInputValue[] = []
  if (options.includeSuperseded !== true) where.push("status = 'active'")
  const project = options.project ?? null
  const names = project !== null && project !== undefined && project !== '' ? resolveProjectNames(db, project) : []
  if (names.length > 0) {
    where.push(`project IN (${names.map(() => '?').join(',')})`)
    params.push(...names)
  }
  for (const tag of normalizeTags(options.tags)) {
    where.push(`tags LIKE ?${LIKE_ESCAPE_SQL}`)
    params.push(tagLikePattern(tag))
  }
  return { clause: where.length > 0 ? ` WHERE ${where.join(' AND ')}` : '', params }
}

/** 列出模式命中多少条（**不含** limit）。 */
export function countMemories(
  db: DatabaseSync,
  options: { project?: string | null; tags?: string | readonly string[] | null; includeSuperseded?: boolean } = {},
): number {
  const { clause, params } = listWhere(db, options)
  try {
    const row = db.prepare(`SELECT COUNT(*) AS n FROM code_index${clause}`).get(...params) as { n: number } | undefined
    return Number(row?.n ?? 0)
  } catch {
    return 0
  }
}

/** 列出模式：无 query 时按 项目（含别名展开）+ 标签 过滤，时间倒序。 */
export function listMemories(
  db: DatabaseSync,
  options: {
    project?: string | null
    tags?: string | readonly string[] | null
    limit?: number
    includeSuperseded?: boolean
  } = {},
): MemoryRow[] {
  const limit = Math.max(1, Math.trunc(options.limit ?? LIST_DEFAULT_LIMIT))
  const { clause, params } = listWhere(db, options)
  const sql =
    'SELECT code,title,project,mem_type,status,file_path,dormant FROM code_index' +
    `${clause} ORDER BY recorded_at DESC, code DESC LIMIT ?`
  const rows = db.prepare(sql).all(...params, limit) as unknown as RowFields[]
  return rows.map((row) => [
    row.code,
    row.title ?? '',
    row.project ?? null,
    row.mem_type ?? '',
    row.status ?? '',
    row.file_path ?? null,
    Number(row.dormant ?? 0),
    LIST_SOURCE,
  ])
}

/** 列出模式回包的首行声明：说清这次按什么过滤、共几条。 */
export function formatListDeclaration(input: {
  project?: string | null
  tags?: string | readonly string[] | null
  shown: number
  total: number
}): string {
  const conds: string[] = []
  if (input.project !== null && input.project !== undefined && input.project !== '') conds.push(`project=${input.project}`)
  const tags = normalizeTags(input.tags)
  if (tags.length > 0) conds.push(`tags=${tags.join('、')}`)
  let head = `已按 ${conds.length > 0 ? conds.join('、') : '无过滤'} 过滤，共 ${input.total} 条（时间倒序`
  if (input.shown < input.total) head += `，仅列前 ${input.shown} 条`
  return `${head}）`
}

/** 状态列的唯一渲染点：`[类型/状态]`，休眠时合并成 `[类型/状态+dormant]`。 */
export function formatStatusTag(memType: string, status: string, dormant: number | boolean): string {
  return `[${memType}/${status}${dormant ? '+dormant' : ''}]`
}

/**
 * 影子期的 FTS 自愈闸门（替代 Python 的 `ensure_fts`）。
 *
 * Python 在"索引空、记忆非空"时会**自动重建**（写路径）。影子期 TS 只有 `mode=ro`，
 * 写不了——所以这里**响亮失败**并给出该跑的命令，而不是"静默少召回"：
 * 少召回的表现是"查不到"，与"本来就没有"长得一模一样。
 */
export function ensureFtsReadable(db: DatabaseSync): void {
  let have = 0
  try {
    const row = db.prepare('SELECT COUNT(*) AS n FROM memory_fts').get() as { n: number } | undefined
    have = Number(row?.n ?? 0)
  } catch {
    return
  }
  const needRow = db.prepare('SELECT COUNT(*) AS n FROM code_index').get() as { n: number } | undefined
  const need = Number(needRow?.n ?? 0)
  if (need > 0 && have === 0) {
    throw new FishError('SHADOW_READ_ONLY', '全文索引为空而记忆非空：需要 `fish reindex` 重建（影子期只读，不能代劳）', {
      hint: '先跑 `fish reindex`（Python 侧写路径），再重试；或确认这不是一个空索引的新库',
      details: { memories: need, fts: have },
    })
  }
}

/** `recall` 的完整回执（逐行对齐 MCP `tool_recall`，mcp_server.py:819）。 */
export function renderRecall(
  db: DatabaseSync,
  options: {
    query?: string | null
    code?: string | null
    project?: string | null
    tags?: string | readonly string[] | null
    scope?: RecallScope | string
    limit?: number
    includeSuperseded?: boolean
  } = {},
  identity: Identity = IDENTITY_ANONYMOUS,
): string {
  const query = (options.query ?? '').trim()
  const code = options.code ?? null
  const project = options.project ?? null
  const tags = options.tags ?? null
  const includeSuperseded = options.includeSuperseded === true
  /** 所有出口都过这一处：MCP 的归属提示是**统一追加**的，漏一个分支就少一整段。 */
  const finish = (text: string): string => appendIdentityHint(text, identity, 'recall')
  if (query === '' && (code === null || code === '')) {
    if (project === null || project === undefined || project === '') {
      if (normalizeTags(tags).length > 0) return finish(`${TAGS_ONLY_HINT}\n`)
      throw new FishError('COMMAND_USAGE', 'query / code / project 至少要给一个', {
        hint: '例：/fish shadow recall 协作约定｜/fish shadow recall --project anyi-studio',
      })
    }
    const limit = Math.trunc(options.limit ?? LIST_DEFAULT_LIMIT)
    const rows = listMemories(db, { project, tags, limit, includeSuperseded })
    const total = countMemories(db, { project, tags, includeSuperseded })
    const head = formatListDeclaration({ project, tags, shown: rows.length, total })
    if (rows.length === 0) {
      const hint =
        normalizeTags(tags).length > 0
          ? '去掉 tags 再列一次，或换 project'
          : '该项目下还没有记忆（可换 project，或先 remember 一条）'
      return finish(`${head}\n（该项目下没有符合筛选条件的条目；${hint}）\n`)
    }
    const lines = [head, RECALL_DIMENSION_NOTE]
    for (const row of rows) {
      const [c, t, p, m, s, , d, src] = row
      lines.push(`- ${c} ${formatStatusTag(m, s, d)} ${t}  ← 匹配:${src}｜项目:${p ?? ''}｜正文用 read_body`)
    }
    return finish(`${lines.join('\n')}\n`)
  }
  ensureFtsReadable(db)
  const { rows } = searchMemories(db, {
    query,
    code,
    project,
    tags,
    scope: options.scope ?? 'all',
    limit: options.limit ?? 10,
    includeSuperseded,
  })
  if (rows.length === 0) {
    const label = query !== '' ? query : (code ?? project ?? '')
    const candidates = recentFallback(db, { project, tags })
    let head = `未找到「${label}」`
    if (project !== null && project !== undefined && project !== '') head += `（已限定 project=${project}）`
    return finish(`${head}。最近的条目：\n${candidates.map((row) => `- ${row.code} ${row.title}`).join('\n')}\n`)
  }
  const lines = [RECALL_DIMENSION_NOTE, RECALL_KEYWORD_NOTE]
  for (const row of rows) {
    const [c, t, , m, s, , d, src] = row
    lines.push(`- ${c} ${formatStatusTag(m, s, d)} ${t}  ← 匹配:${src}｜正文用 read_body`)
  }
  return finish(`${lines.join('\n')}\n`)
}
