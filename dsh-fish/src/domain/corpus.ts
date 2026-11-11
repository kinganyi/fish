/**
 * 语料层只读面（`corpus.sqlite`：search / read / stats）。
 *
 * ## 为什么 TS 侧可以直接读它（而不像 T04 那样标 `python-only`）
 *
 * T04 把 `corpus` 标成 `python-only` 的理由是"**快照副本里没有它**"——那是转发路径的
 * 限制，不是读能力的限制。语料库是独立文件 `<data>/corpus.sqlite`（`DB_NAME`），
 * 只读打开它就能算出同一份读数；读不出"假空"的根因（快照缺库）在这里不存在。
 *
 * ## 与生产的两处**有意差异**（都登记在 NOTES-T05 §5）
 *
 *   1. 生产 `search`/`read` 会把访问记进 `corpus_access_log`（`_log_access`，
 *      `p0/fish_corpus.py:859`）。影子期**不写**——所以本模块没有 `logAccess`。
 *   2. 生产 MCP 的 `corpus.read` 读完会回填 `push_log.reaction`（`mcp_server.py:2056`）。
 *      那是**主库写**，影子期更不做；本模块只返回正文与来源状态。
 *
 * 代价：DSH 里的检索不出现在语料审计里；收益：读命令不可能改任何一个字节。
 *
 * ## 逐条复刻的生产语义（每条都能在源码里指到行）
 *
 *   · 门牌三档（`bad_pointer` / `not_found` / `ambiguous_pointer`）——**不许猜一个最像的**；
 *   · 过滤先于排序：`MATCH` 与全部过滤在一条 SQL 里下推（`build_filter_sql`）；
 *   · 排序 = 0.6×批内归一 bm25 + 0.2×命中词比 + 0.2×时间衰减（`DefaultRanker`）；
 *   · **出口二次脱敏**：正文、标题、workspace、cwd 都在出口过一遍规则表；
 *   · `query` 只以 sha256 形式出现（本模块不算它——不写审计就不需要它）。
 *
 * @module dsh-fish/domain/corpus
 */

import { existsSync } from 'node:fs'
import path from 'node:path'
import { DatabaseSync } from 'node:sqlite'

import { isoLocal, utcStamp } from '../core/time.ts'
import { pyRepr } from '../core/pytext.ts'
import { cutTokens } from '../layout/cut.ts'
import { codePointLength, sliceCodePoints } from './token.ts'

// ---------------------------------------------------------------- 常量（对齐 fish_corpus.py）

/** 库文件名（`fish_corpus_store.DB_NAME`）。 */
export const CORPUS_DB_NAME = 'corpus.sqlite'
/** 检索缺省条数。 */
export const SEARCH_DEFAULT_LIMIT = 8
/** 检索条数硬上限（超出夹紧）。 */
export const SEARCH_MAX_LIMIT = 32
/** 送 ranker 的候选 = limit × 8 … */
export const SEARCH_CANDIDATE_FACTOR = 8
/** … 且不少于 64（太少会让排序退化成"谁先入库"）。 */
export const SEARCH_CANDIDATE_MIN = 64
/** 片段长度。 */
export const SNIPPET_CHARS = 200
/** `read` 的缺省字符上限。 */
export const READ_DEFAULT_MAX_CHARS = 8000

/** 段级/会话级台账的作用域词表（`forget` 的四档）。 */
export const CORPUS_SOURCE_LINE = 'source=对话语料（corpus.sqlite 投影）'

// ---------------------------------------------------------------- 脱敏（唯一一份规则表）

/** 占位符（命中即替换并计数，**绝不整段删成空串**）。 */
export const REDACT_PLACEHOLDER = '〔已脱敏:%s〕'

/**
 * 脱敏规则（`fish_corpus_redact.RULES` 的同序复刻）。
 *
 * 顺序**有意义**：具体形态（`fsh_…`）先于泛化形态（`token=`），否则
 * `token = fsh_xxx` 会先被泛化规则吃掉、具体类型就永远统计不到。
 */
export const REDACT_RULES: readonly { readonly name: string; readonly pattern: RegExp }[] = [
  { name: 'fish_token', pattern: /fsh_[A-Za-z0-9_-]{20,}/gu },
  { name: 'openai_key', pattern: /sk-[A-Za-z0-9_-]{20,}/gu },
  { name: 'github_token', pattern: /(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}/gu },
  { name: 'aws_access_key', pattern: /(?:AKIA|ASIA)[0-9A-Z]{16}/gu },
  { name: 'private_key', pattern: /BEGIN [A-Z ]*PRIVATE KEY/gu },
  { name: 'bearer_token', pattern: /Bearer\s+\S{8,}/giu },
  { name: 'token_env', pattern: /[a-z0-9_]*(?:token|secret|api[_-]?key)["']?\s*[=:：]\s*["']?\S+/giu },
  { name: 'password', pattern: /(?:password|passwd|pwd|密码|口令)["']?\s*[=:：]\s*["']?\S+/giu },
]

/** 脱敏一段文本，返回 `[脱敏后文本, 命中数]`。 */
export function redact(text: string | null | undefined): [string, number] {
  let out = text === null || text === undefined ? '' : String(text)
  let total = 0
  for (const rule of REDACT_RULES) {
    // 每条规则重置 lastIndex：全局正则在 `replace` 里不会残留状态，但 `test` 会。
    rule.pattern.lastIndex = 0
    const replaced = out.replace(rule.pattern, () => {
      total += 1
      return REDACT_PLACEHOLDER.replace('%s', rule.name)
    })
    out = replaced
  }
  return [out, total]
}

/** **非正文**可回显列的出口二次脱敏（`workspace` / `cwd` / 标题族）。 */
export function safeField(value: unknown): string {
  if (value === null || value === undefined || value === '') return ''
  return redact(String(value))[0]
}

// ---------------------------------------------------------------- 门牌（指针）

/** 门牌解析失败（三档语义，**不许猜一个最像的**）。 */
export class PointerError extends Error {
  readonly code: 'bad_pointer' | 'not_found' | 'ambiguous_pointer'
  readonly candidates: readonly string[]

  constructor(code: 'bad_pointer' | 'not_found' | 'ambiguous_pointer', message: string, candidates: readonly string[] = []) {
    super(message)
    this.name = 'PointerError'
    this.code = code
    this.candidates = candidates
  }
}

const POINTER_RE = /^(?<kind>log|conv):(?<host>[A-Za-z0-9_.-]+):(?<sid>[^#\s]+)#(?<loc>(?:seq|line)=\d+)(?:\.(?<part>\d+))?$/u

/** 会话短址：剥掉可选 `session-` 前缀后取前 8 位。 */
export function sid8(sessionId: string): string {
  const text = String(sessionId ?? '')
  return text.startsWith('session-') ? text.slice('session-'.length, 'session-'.length + 8) : text.slice(0, 8)
}

/** 规范门牌：`log:<host>:<session_id>#<loc>[.<part>]`。 */
export function canonicalPointer(host: string, sessionId: string, loc: string, part = 0): string {
  const base = `log:${host}:${sessionId}#${loc}`
  return part === 0 ? base : `${base}.${part}`
}

/** 推送用缩写：`conv:<host>:<sid8>#<loc>[.<part>]`。 */
export function pushPointer(host: string, sessionId: string, loc: string, part = 0): string {
  const base = `conv:${host}:${sid8(sessionId)}#${loc}`
  return part === 0 ? base : `${base}.${part}`
}

/** 纯字符串解析（不查库）。 */
export function parsePointer(pointer: string): { kind: string; host: string; sid: string; loc: string; part: number } {
  const text = String(pointer ?? '').trim()
  if (text === '') throw new PointerError('bad_pointer', '门牌是空串')
  const match = POINTER_RE.exec(text)
  if (match === null) {
    throw new PointerError(
      'bad_pointer',
      `门牌形态不对：${pyRepr(text)}（要 log:<host>:<session_id>#seq=123[.1] 或 conv:<host>:<sid8>#seq=123）`,
    )
  }
  const groups = match.groups ?? {}
  return {
    kind: String(groups.kind),
    host: String(groups.host),
    sid: String(groups.sid),
    loc: String(groups.loc),
    part: Number(groups.part ?? 0),
  }
}

/** 解析 + 补全：`conv:` 按 `sid8` 查回完整 `session_id`（多命中必须歧义报错）。 */
export function resolvePointer(
  db: DatabaseSync,
  pointer: string,
): { kind: string; host: string; sid: string; sessionId: string; loc: string; part: number; pointer: string; code: string } {
  const parsed = parsePointer(pointer)
  let sessionId: string
  if (parsed.kind === 'log') {
    sessionId = parsed.sid
  } else {
    const rows = db
      .prepare('SELECT session_id FROM corpus_session WHERE host=? AND sid8=? ORDER BY session_id')
      .all(parsed.host, parsed.sid) as { session_id: string }[]
    const ids = rows.map((row) => String(row.session_id))
    if (ids.length === 0) {
      throw new PointerError('not_found', `conv 缩写 ${pyRepr(pointer)} 找不到会话（host=${parsed.host}, sid8=${parsed.sid}）`)
    }
    if (ids.length > 1) {
      throw new PointerError(
        'ambiguous_pointer',
        `conv 缩写 ${pyRepr(pointer)} 命中 ${ids.length} 个会话，请用完整 log: 门牌`,
        ids.slice(0, 5).map((sid) => canonicalPointer(parsed.host, sid, parsed.loc, parsed.part)),
      )
    }
    sessionId = ids[0] as string
  }
  return {
    kind: parsed.kind,
    host: parsed.host,
    sid: parsed.sid,
    sessionId,
    loc: parsed.loc,
    part: parsed.part,
    pointer: canonicalPointer(parsed.host, sessionId, parsed.loc, parsed.part),
    code: pushPointer(parsed.host, sessionId, parsed.loc, parsed.part),
  }
}

// ---------------------------------------------------------------- 连接（只读）

/** 语料库路径：`<root>/corpus.sqlite`（root 就是 `p0/data`）。 */
export function corpusDbPath(dataDir: string): string {
  return path.join(dataDir, CORPUS_DB_NAME)
}

/**
 * 只读打开语料库；库不存在返回 `null`（**读不建库**，也不留空库文件）。
 *
 * 为什么 `query_only=1`：与主库同一条纪律——让 SQLite 自己拒绝写，
 * 而不是"我们小心不写"。
 */
export function openCorpusReadOnly(dataDir: string): DatabaseSync | null {
  const target = corpusDbPath(dataDir)
  let db: DatabaseSync
  try {
    db = new DatabaseSync(target, { readOnly: true })
  } catch {
    return null
  }
  try {
    db.exec('PRAGMA busy_timeout=5000')
    db.exec('PRAGMA query_only=1')
  } catch {
    db.close()
    return null
  }
  return db
}

// ---------------------------------------------------------------- 排序（DefaultRanker）

/** 批内 min-max 归一；退化解（全相等 / 只有一个）→ 全部 1.0。 */
export function rankNorm(values: readonly number[]): number[] {
  if (values.length === 0) return []
  const lo = Math.min(...values)
  const hi = Math.max(...values)
  if (hi === lo) return values.map(() => 1)
  return values.map((value) => (hi - value) / (hi - lo))
}

/** `2026-10-27T04:06:43Z` → 毫秒；解析失败返回 `null`（不猜、不抛）。 */
function parseUtc(stamp: unknown): number | null {
  if (typeof stamp !== 'string' || stamp === '') return null
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/u.test(stamp)) return null
  const parsed = new Date(stamp)
  return Number.isNaN(parsed.getTime()) ? null : parsed.getTime()
}

/** 段的年龄（天）；时间缺失/坏 → 0.0（当作"刚发生"）。 */
function ageDays(occurredAt: unknown, now: number): number {
  const stamp = parseUtc(occurredAt)
  if (stamp === null) return 0
  return Math.max(0, (now - stamp) / 86400000)
}

/** `why` 里的人类读数（"14 天前" / "今天"）。 */
export function ageText(days: number): string {
  if (days < 1) return '今天'
  if (days < 30) return `${Math.trunc(days)} 天前`
  if (days < 365) return `${Math.max(1, Math.trunc(days / 30))} 个月前`
  return `${(days / 365).toFixed(1)} 年前`
}

/** 排序协议的一行：`(pointer, score, why)`。 */
export interface RankedHit {
  readonly pointer: string
  readonly score: number
  readonly why: readonly string[]
}

/** 默认排序（BM25 + 命中词比 + 时间衰减；权重视为契约）。 */
export function rankCandidates(
  cands: readonly CorpusCandidate[],
  terms: readonly string[],
  context: { readonly project?: string; readonly now?: number },
): RankedHit[] {
  if (cands.length === 0) return []
  const now = context.now ?? Date.now()
  const project = context.project ?? ''
  const norms = rankNorm(cands.map((cand) => cand.bm25))
  const scored: RankedHit[] = cands.map((cand, index) => {
    const doc = new Set(cutTokens(String(cand.text ?? '')))
    const matched = terms.filter((term) => doc.has(term)).length
    const ratio = terms.length > 0 ? matched / terms.length : 0
    const age = ageDays(cand.occurred_at, now)
    const recency = Math.exp(-age / 180)
    const score = 0.6 * (norms[index] as number) + 0.2 * ratio + 0.2 * recency
    const why: string[] = []
    if (terms.length > 0) why.push(`命中 ${matched}/${terms.length} 个词`)
    if (project !== '' && String(cand.project ?? '') === project) why.push('同项目')
    why.push(ageText(age))
    return { pointer: String(cand.pointer), score: round6(score), why }
  })
  scored.sort((left, right) => (right.score !== left.score ? right.score - left.score : left.pointer < right.pointer ? -1 : left.pointer > right.pointer ? 1 : 0))
  return scored
}

/** 六位小数（与 `round(score, 6)` 同用途）。 */
function round6(value: number): number {
  return Number(value.toFixed(6))
}

// ---------------------------------------------------------------- 检索

/** 检索候选（一条段行 + 会话标签 + bm25）。 */
export interface CorpusCandidate {
  readonly seg_id: number
  readonly host: string
  readonly session_id: string
  readonly loc: string
  readonly part: number
  readonly role: string
  readonly occurred_at: string
  readonly text: string
  readonly chars: number
  readonly redacted_count: number
  readonly shadowed: number
  readonly sid8: string | null
  readonly session_title: string | null
  readonly project: string | null
  readonly workspace: string | null
  readonly citizen_id: string | null
  readonly origin: string | null
  readonly bm25: number
  /** 检索时补上的门牌（`canonical_pointer`）；候选行本身没有这一列。 */
  pointer?: string
}

/** 过滤键 → 列（全 AND；同一键多值 = OR；空列表 = 不限）。 */
export const FILTER_COLUMNS: Readonly<Record<string, string>> = {
  host: 's.host',
  project: 'ss.project',
  workspace: 'ss.workspace',
  session: 's.session_id',
  citizen: 'ss.citizen_id',
  role: 's.role',
  origin: 'ss.origin',
}

const FTS_SELECT =
  'SELECT s.id AS seg_id, s.host, s.session_id, s.loc, s.part, s.role,' +
  ' s.occurred_at, s.text, s.chars, s.redacted_count, s.shadowed,' +
  ' ss.sid8, ss.title AS session_title, ss.project, ss.workspace,' +
  ' ss.citizen_id, ss.origin, bm25(corpus_fts) AS bm25' +
  ' FROM corpus_fts' +
  ' JOIN corpus_segment s ON s.id=corpus_fts.seg_id' +
  ' LEFT JOIN corpus_session ss ON ss.host=s.host AND ss.session_id=s.session_id' +
  ' WHERE corpus_fts MATCH ?'

/** 过滤条件 → `[whereSql, args]`。未知键 / 本地时间形态**响亮失败**。 */
export function buildFilterSql(
  filters: Readonly<Record<string, readonly string[]>> | null | undefined,
  options: { readonly includeShadowed?: boolean; readonly timeFrom?: string | null; readonly timeTo?: string | null } = {},
): [string, string[]] {
  const where: string[] = []
  const args: string[] = []
  for (const [key, value] of Object.entries(filters ?? {})) {
    const column = FILTER_COLUMNS[key]
    if (column === undefined) {
      throw new Error(`未知过滤键 ${JSON.stringify(key)}（只认 ${Object.keys(FILTER_COLUMNS).join('/')}）`)
    }
    const values = value.map((item) => String(item))
    if (values.length === 0) continue
    where.push(`${column} IN (${values.map(() => '?').join(',')})`)
    args.push(...values)
  }
  if (options.includeShadowed !== true) where.push('s.shadowed=0')
  for (const [name, value, operator] of [
    ['time_from', options.timeFrom ?? null, '>='],
    ['time_to', options.timeTo ?? null, '<='],
  ] as const) {
    if (value === null || value === '') continue
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/u.test(value)) {
      throw new Error(`${name} 必须是 UTC 'Z' 形式（如 2026-10-27T00:00:00Z），收到 ${JSON.stringify(value)}——本地时间会静默差 8 小时`)
    }
    where.push(`s.occurred_at${operator}?`)
    args.push(value)
  }
  return [where.length > 0 ? where.join(' AND ') : '1=1', args]
}

/** 一次 SQL 把 MATCH + 全部过滤下推（过滤先于排序）。 */
function searchSegments(
  db: DatabaseSync,
  matchExpr: string,
  options: { readonly filters?: Readonly<Record<string, readonly string[]>> | null; readonly includeShadowed?: boolean; readonly limit: number },
): CorpusCandidate[] {
  const [where, args] = buildFilterSql(options.filters, { includeShadowed: options.includeShadowed })
  const sql = `${FTS_SELECT} AND ${where} ORDER BY bm25(corpus_fts) LIMIT ?`
  return db.prepare(sql).all(matchExpr, ...args, Math.trunc(options.limit)) as unknown as CorpusCandidate[]
}

/** 同一次过滤下的**总命中数**。 */
function countMatching(
  db: DatabaseSync,
  matchExpr: string,
  options: { readonly filters?: Readonly<Record<string, readonly string[]>> | null; readonly includeShadowed?: boolean },
): number {
  const [where, args] = buildFilterSql(options.filters, { includeShadowed: options.includeShadowed })
  const sql =
    'SELECT COUNT(*) AS n FROM corpus_fts' +
    ' JOIN corpus_segment s ON s.id=corpus_fts.seg_id' +
    ' LEFT JOIN corpus_session ss ON ss.host=s.host AND ss.session_id=s.session_id' +
    ' WHERE corpus_fts MATCH ? AND ' + where
  const row = db.prepare(sql).get(matchExpr, ...args) as { n?: number } | undefined
  return Number(row?.n ?? 0)
}

/** 片段（**出口二次脱敏**：入库漏了、出口补上）。 */
function snippetOf(cand: CorpusCandidate, chars = SNIPPET_CHARS): [string, number] {
  const [text, count] = redact(cand.text ?? '')
  const clip = codePointLength(text) > chars ? `${sliceCodePoints(text, 0, chars)}…` : text
  return [clip, Math.max(Number(cand.redacted_count ?? 0), count)]
}

/** `hits[]` 的一行（字段名与顺序按 PRD §8.1 冻结）。 */
export interface CorpusHit {
  readonly pointer: string
  readonly host: string
  readonly session_id: string
  readonly session_title: string
  readonly role: string
  readonly occurred_at: string
  readonly project: string
  readonly workspace: string
  readonly citizen_id: string | null
  readonly origin: string | null
  readonly shadowed: number
  readonly snippet: string
  readonly chars: number
  readonly score: number
  readonly ranker: string
  readonly why: readonly string[]
}

/** `search` 的返回。 */
export interface CorpusSearchResult {
  readonly hits: readonly CorpusHit[]
  readonly meta: {
    readonly total_matched: number
    readonly returned: number
    readonly ranker: string
    readonly filters_echo: Readonly<Record<string, readonly string[]>>
    readonly degraded: readonly { readonly reason: string; readonly note: string }[]
    readonly as_of: string
    readonly truncated: boolean
    readonly limit: number
    readonly budget_tokens: number | null
  }
}

/** 检索原话（设计 §6）：一次 SQL 把 `MATCH` 与全部过滤下推，排序交给 ranker。 */
export function corpusSearch(
  dataDir: string,
  query: string,
  options: {
    readonly filters?: Readonly<Record<string, readonly string[]>> | null
    readonly limit?: number
    readonly ranker?: string
    readonly now?: number
  } = {},
): CorpusSearchResult {
  const rankerName = String(options.ranker ?? 'default').trim().toLowerCase()
  if (rankerName !== 'default') {
    throw new Error(`未知 ranker：${JSON.stringify(options.ranker)}（已登记：default）`)
  }
  const filters: Record<string, readonly string[]> = { ...(options.filters ?? {}) }
  const limitValue = Math.max(1, Math.min(Math.trunc(options.limit ?? SEARCH_DEFAULT_LIMIT), SEARCH_MAX_LIMIT))
  const now = options.now ?? Date.now()
  const degraded: { reason: string; note: string }[] = []
  const hits: CorpusHit[] = []
  let total = 0
  let truncated = false
  const db = openCorpusReadOnly(dataDir)
  try {
    if (db === null) {
      degraded.push({ reason: 'db_missing', note: '语料库还没建：先跑 `fish corpus ingest`' })
    } else {
      const terms = dedupe(cutTokens(String(query ?? '')))
      const expr = matchExpression(terms)
      if (expr !== null) {
        const pool = Math.max(limitValue * SEARCH_CANDIDATE_FACTOR, SEARCH_CANDIDATE_MIN)
        const cands = searchSegments(db, expr, { filters, limit: pool })
        total = countMatching(db, expr, { filters })
        for (const cand of cands) {
          cand.pointer = canonicalPointer(cand.host, cand.session_id, cand.loc, cand.part)
        }
        const projectFilter = filters.project?.[0] ?? ''
        const ranked = rankCandidates(cands, terms, { project: projectFilter, now })
        const byPointer = new Map(cands.map((cand) => [String(cand.pointer), cand]))
        for (const item of ranked) {
          const cand = byPointer.get(item.pointer)
          if (cand === undefined) continue
          const [snippet] = snippetOf(cand)
          hits.push({
            pointer: item.pointer,
            host: cand.host,
            session_id: cand.session_id,
            session_title: safeField(cand.session_title),
            role: cand.role,
            occurred_at: cand.occurred_at,
            project: String(cand.project ?? ''),
            workspace: safeField(cand.workspace),
            citizen_id: cand.citizen_id,
            origin: cand.origin,
            shadowed: Number(cand.shadowed ?? 0),
            snippet,
            chars: Number(cand.chars ?? 0),
            score: item.score,
            ranker: rankerName,
            why: item.why,
          })
          if (hits.length >= limitValue) break
        }
        truncated = total > cands.length || hits.length < ranked.length
      }
    }
  } finally {
    db?.close()
  }
  return {
    hits,
    meta: {
      total_matched: total,
      returned: hits.length,
      ranker: rankerName,
      filters_echo: filters,
      degraded,
      as_of: utcStamp(new Date()),
      truncated,
      limit: limitValue,
      budget_tokens: null,
    },
  }
}

/** 去重（保序、丢空）。 */
function dedupe(items: readonly string[]): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const item of items) {
    if (item === '' || seen.has(item)) continue
    seen.add(item)
    out.push(item)
  }
  return out
}

/** token 列表 → FTS5 MATCH 表达式（每个 token 加双引号，OR 连接）。 */
export function matchExpression(terms: readonly string[]): string | null {
  const tokens = terms.map((term) => String(term).trim()).filter((term) => term !== '')
  if (tokens.length === 0) return null
  return tokens.map((token) => `"${token.replace(/"/gu, '""')}"`).join(' OR ')
}

// ---------------------------------------------------------------- 读原文

/** 源文件状态（只影响声明，不影响正文）。 */
export interface CorpusSourceState {
  readonly path: string | null
  readonly missing: boolean
  readonly status: string | null
  readonly note: string
}

/** 源文件现在还在不在（`source.missing`）。 */
function sourceState(db: DatabaseSync, host: string, sessionId: string): CorpusSourceState {
  // 只取需要的列：`corpus_source` 里有 `mtime_ns`（纳秒时间戳，约 1.8e18），
  // node:sqlite 读超过 2^53 的 INTEGER 会**抛错**（"Value is too large…"）。
  // `SELECT *` 在这个表上不是"顺手"，是把整条读路径变成一次可能失败的调用。
  const row = db
    .prepare('SELECT path, status FROM corpus_source WHERE host=? AND session_id=?')
    .get(host, sessionId) as { path?: string | null; status?: string | null } | undefined
  if (row === undefined) {
    return { path: null, missing: true, status: null, note: '没有采集游标：源文件路径未知（正文来自投影）' }
  }
  const rawPath = String(row.path ?? '')
  let exists = false
  if (rawPath !== '') {
    exists = existsSync(rawPath)
  }
  return {
    path: safeField(rawPath) === '' ? null : safeField(rawPath),
    missing: !exists,
    status: row.status === null || row.status === undefined ? null : String(row.status),
    note: exists ? '' : '源文件已不在：正文来自投影（脱敏后的切片），不猜',
  }
}

/** `read` 的返回。 */
export interface CorpusReadResult {
  readonly pointer: string
  readonly code: string
  readonly host: string
  readonly session_id: string
  readonly sid8: string
  readonly loc: string
  readonly part: number
  readonly role: string
  readonly occurred_at: string
  readonly session_title: string
  readonly project: string
  readonly workspace: string
  readonly citizen_id: string | null
  readonly origin: string | null
  readonly shadowed: number
  readonly chars: number
  readonly redacted_count: number
  readonly text: string
  readonly truncated: boolean
  readonly truncated_in_store: boolean
  readonly offset_space: string
  readonly source: CorpusSourceState
  readonly text_from: 'projection'
  readonly as_of: string
}

/** 门牌 → 原文（源文件缺失时仍返回已存正文并声明）。 */
export function corpusRead(
  dataDir: string,
  pointer: string,
  options: { readonly maxChars?: number } = {},
): CorpusReadResult {
  const db = openCorpusReadOnly(dataDir)
  if (db === null) {
    throw new Error(`语料库不存在：先跑 \`fish corpus ingest\`（${corpusDbPath(dataDir)}）`)
  }
  try {
    const resolved = resolvePointer(db, pointer)
    const seg = db
      .prepare(
        'SELECT s.*, ss.title AS session_title, ss.project, ss.workspace,' +
          ' ss.citizen_id, ss.origin, ss.cwd FROM corpus_segment s' +
          ' LEFT JOIN corpus_session ss ON ss.host=s.host AND ss.session_id=s.session_id' +
          ' WHERE s.host=? AND s.session_id=? AND s.loc=? AND s.part=?',
      )
      .get(resolved.host, resolved.sessionId, resolved.loc, resolved.part) as Record<string, unknown> | undefined
    if (seg === undefined) {
      throw new PointerError('not_found', `门牌 ${resolved.pointer} 在库里没有对应段（可能已 forget）`)
    }
    const [text0, count] = redact(String(seg.text ?? ''))
    const limit = Math.max(1, Math.trunc(options.maxChars ?? READ_DEFAULT_MAX_CHARS))
    const truncated = codePointLength(text0) > limit
    const text = truncated ? sliceCodePoints(text0, 0, limit) : text0
    const source = sourceState(db, resolved.host, resolved.sessionId)
    return {
      pointer: resolved.pointer,
      code: resolved.code,
      host: resolved.host,
      session_id: resolved.sessionId,
      sid8: sid8(resolved.sessionId),
      loc: resolved.loc,
      part: resolved.part,
      role: String(seg.role ?? ''),
      occurred_at: String(seg.occurred_at ?? ''),
      session_title: safeField(seg.session_title),
      project: String(seg.project ?? ''),
      workspace: safeField(seg.workspace),
      citizen_id: seg.citizen_id === null || seg.citizen_id === undefined ? null : String(seg.citizen_id),
      origin: seg.origin === null || seg.origin === undefined ? null : String(seg.origin),
      shadowed: Number(seg.shadowed ?? 0),
      chars: Number(seg.chars ?? 0),
      redacted_count: Math.max(Number(seg.redacted_count ?? 0), count),
      text,
      truncated,
      truncated_in_store: Number(seg.chars ?? 0) > codePointLength(String(seg.text ?? '')),
      offset_space: String(seg.offset_space ?? ''),
      source,
      text_from: 'projection',
      as_of: utcStamp(new Date()),
    }
  } finally {
    db.close()
  }
}

// ---------------------------------------------------------------- 统计

/** `stats` 的返回（字段名与生产一致）。 */
export interface CorpusStatsResult {
  readonly as_of: string
  readonly scope: Readonly<Record<string, string>>
  readonly segments: number
  readonly sessions: number
  readonly by_host: Readonly<Record<string, { sources: number; segments_kept: number; sessions: number; segments: number }>>
  readonly by_project: Readonly<Record<string, { project: string; sessions: number; segments: number }[]>>
  readonly by_status: Readonly<Record<string, Record<string, number>>>
  readonly degraded: readonly Record<string, unknown>[]
  readonly orphan: readonly { host: string; session_id: string; path: string }[]
  readonly unmapped_cwds: readonly { workspace: string; sessions: number }[]
  readonly window_days: number
  readonly skipped_by_window: number
  readonly lag_sec: number | null
  readonly db_bytes: Readonly<Record<string, number>>
  readonly forgotten_sessions: number
  readonly tombstones: { session: number; host: number; pointer: number; range: number }
  readonly alarm: string | null
  readonly stall: { zero_streak: number; alarm: string | null }
  readonly db_missing?: boolean
}

/** 读到整数型 meta 读数（缺失/脏值都算 0）。 */
function metaInt(db: DatabaseSync, key: string): number {
  try {
    const row = db.prepare('SELECT v FROM corpus_meta WHERE k=?').get(key) as { v?: string } | undefined
    const value = Number.parseInt(String(row?.v ?? '0'), 10)
    return Number.isNaN(value) ? 0 : value
  } catch {
    return 0
  }
}

/** `now - max(last_ingested_at)`：日报用它抓"假增量"。 */
function lagSeconds(db: DatabaseSync): number | null {
  const row = db.prepare('SELECT MAX(last_ingested_at) AS v FROM corpus_source').get() as { v?: string | null } | undefined
  const stamp = row?.v ?? null
  if (stamp === null || stamp === '') return null
  const parsed = parseUtc(stamp)
  if (parsed === null) return null
  return Math.trunc(Math.max(0, (Date.now() - parsed) / 1000))
}

/** 库体积分项（失败返回空对象：读体积失败不是业务失败）。 */
function dbBytes(db: DatabaseSync): Record<string, number> {
  try {
    const pageSize = db.prepare('PRAGMA page_size').get() as { page_size?: number } | undefined
    const pageCount = db.prepare('PRAGMA page_count').get() as { page_count?: number } | undefined
    const total = Number(pageSize?.page_size ?? 0) * Number(pageCount?.page_count ?? 0)
    return { total, segments: tableBytes(db, 'corpus_segment'), fts: tableBytes(db, 'corpus_fts') }
  } catch {
    return {}
  }
}

/** 一张表占的页字节（`dbstat` 不可用 → 0）。 */
function tableBytes(db: DatabaseSync, table: string): number {
  try {
    const row = db.prepare('SELECT SUM(pgsize) AS n FROM dbstat WHERE name LIKE ?').get(`${table}%`) as
      | { n?: number }
      | undefined
    return Number(row?.n ?? 0)
  } catch {
    return 0
  }
}

/** 台账 → 四档作用域（`scopes_from_tombstones` 的同形复刻）。 */
function tombstoneScopes(db: DatabaseSync): {
  sessions: Set<string>
  hosts: Set<string>
  pointers: { host: string; sid: string; loc: string; part: number }[]
  ranges: { host: string | null; start: string; end: string }[]
} {
  const rows = db.prepare('SELECT * FROM corpus_tombstone ORDER BY created_at, scope_key').all() as Record<string, unknown>[]
  const scopes = {
    sessions: new Set<string>(),
    hosts: new Set<string>(),
    pointers: [] as { host: string; sid: string; loc: string; part: number }[],
    ranges: [] as { host: string | null; start: string; end: string }[],
  }
  const isUtc = (value: string): boolean => /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/u.test(value)
  for (const row of rows) {
    const scope = String(row.scope_type ?? '')
    const key = String(row.scope_key ?? '')
    if (scope === 'session') {
      const index = key.indexOf(':')
      scopes.sessions.add(index >= 0 ? `${key.slice(0, index)}\u0000${key.slice(index + 1)}` : `${key}\u0000`)
    } else if (scope === 'host') {
      scopes.hosts.add(key)
    } else if (scope === 'pointer') {
      let parsed: { kind: string; host: string; sid: string; loc: string; part: number }
      try {
        parsed = parsePointer(key)
      } catch {
        continue
      }
      if (parsed.kind !== 'log') continue
      scopes.pointers.push({ host: parsed.host, sid: parsed.sid, loc: parsed.loc, part: parsed.part })
    } else if (scope === 'range') {
      const colon = key.indexOf(':')
      const host = colon >= 0 ? key.slice(0, colon) : key
      const window = colon >= 0 ? key.slice(colon + 1) : ''
      const dots = window.indexOf('..')
      const start = dots >= 0 ? window.slice(0, dots) : window
      const end = dots >= 0 ? window.slice(dots + 2) : ''
      if (isUtc(start) && isUtc(end)) scopes.ranges.push({ host: host === '' || host === '*' ? null : host, start, end })
    }
  }
  return scopes
}

/** 这段对话是否被台账删过（`*:<sid>` 也算——跨宿主语义）。 */
function isSessionForgotten(scopes: ReturnType<typeof tombstoneScopes>, host: string, sessionId: string): boolean {
  if (scopes.hosts.has(host)) return true
  return scopes.sessions.has(`${host}\u0000${sessionId}`) || scopes.sessions.has(`*\u0000${sessionId}`)
}

/** 采集读数（C-P0-9）：待采/已采/跳过/degraded/orphan 与文件数对账。 */
export function corpusStats(dataDir: string, host?: string | null): CorpusStatsResult {
  const db = openCorpusReadOnly(dataDir)
  if (db === null) {
    return {
      as_of: utcStamp(new Date()),
      scope: host !== null && host !== undefined && host !== '' ? { host } : {},
      segments: 0,
      sessions: 0,
      by_host: {},
      by_project: {},
      by_status: {},
      degraded: [],
      orphan: [],
      unmapped_cwds: [],
      lag_sec: null,
      db_bytes: {},
      db_missing: true,
      forgotten_sessions: 0,
      alarm: null,
      window_days: 0,
      skipped_by_window: 0,
      tombstones: { session: 0, host: 0, pointer: 0, range: 0 },
      stall: { zero_streak: 0, alarm: null },
    }
  }
  try {
    const scopes = tombstoneScopes(db)
    const liveRows = db
      .prepare('SELECT host, session_id, COUNT(*) AS n FROM corpus_segment GROUP BY host, session_id')
      .all() as { host: string; session_id: string; n: number }[]
    const liveCounts = new Map<string, number>()
    for (const row of liveRows) {
      const key = `${row.host}\u0000${row.session_id}`
      // 段级台账（pointer/range）命中的段不算"活着"（与生产同一口径）。
      let live = Number(row.n)
      if (live > 0 && (scopes.pointers.length > 0 || scopes.ranges.length > 0)) {
        live = countLiveSegments(db, scopes, row.host, row.session_id, live)
      }
      liveCounts.set(key, live)
    }
    const byHost: Record<string, { sources: number; segments_kept: number; sessions: number; segments: number }> = {}
    const sourceRows = db
      .prepare('SELECT host, COUNT(*) AS files, COALESCE(SUM(segments_kept),0) AS segs FROM corpus_source GROUP BY host')
      .all() as { host: string; files: number; segs: number }[]
    for (const row of sourceRows) {
      byHost[row.host] = { sources: Number(row.files), segments_kept: Number(row.segs), sessions: 0, segments: 0 }
    }
    let keptSessions = 0
    let forgottenSeen = 0
    const byProjectRaw: Record<string, Map<string, { project: string; sessions: number; segments: number }>> = {}
    const sessionRows = db
      .prepare('SELECT host, session_id, project, segments FROM corpus_session ORDER BY host, session_id')
      .all() as { host: string; session_id: string; project: string; segments: number }[]
    for (const row of sessionRows) {
      if (isSessionForgotten(scopes, row.host, row.session_id)) {
        forgottenSeen += 1
        continue
      }
      keptSessions += 1
      const key = `${row.host}\u0000${row.session_id}`
      const segments = liveCounts.get(key) ?? 0
      const slot = (byHost[row.host] ??= { sources: 0, segments_kept: 0, sessions: 0, segments: 0 })
      slot.sessions += 1
      slot.segments += segments
      const bucket = (byProjectRaw[row.host] ??= new Map())
      const item = bucket.get(String(row.project)) ?? { project: String(row.project), sessions: 0, segments: 0 }
      item.sessions += 1
      item.segments += segments
      bucket.set(String(row.project), item)
    }
    const byProject: Record<string, { project: string; sessions: number; segments: number }[]> = {}
    for (const [hostKey, bucket] of Object.entries(byProjectRaw)) {
      byProject[hostKey] = [...bucket.keys()].sort().map((key) => bucket.get(key) as { project: string; sessions: number; segments: number })
    }
    let totalSegments = 0
    for (const value of liveCounts.values()) totalSegments += value
    const byStatus: Record<string, Record<string, number>> = {}
    const statusRows = db
      .prepare('SELECT host, status, COUNT(*) AS n FROM corpus_source GROUP BY host, status')
      .all() as { host: string; status: string; n: number }[]
    for (const row of statusRows) {
      const slot = (byStatus[row.host] ??= {})
      slot[row.status] = Number(row.n)
    }
    const degraded = (db.prepare('SELECT * FROM corpus_degraded ORDER BY last_seen_at DESC').all() as Record<string, unknown>[]).map(
      (row) => ({ ...row, path: safeField(row.path) }),
    )
    const orphan = (
      db.prepare('SELECT host, session_id, path, status FROM corpus_source ORDER BY host, session_id').all() as Record<
        string,
        unknown
      >[]
    )
      .filter((row) => String(row.status ?? '') === 'orphan')
      .map((row) => ({ host: String(row.host), session_id: String(row.session_id), path: safeField(row.path) }))
    const unmapped = (
      db
        .prepare(
          "SELECT workspace, COUNT(*) AS n FROM corpus_session WHERE project='' AND workspace<>'' GROUP BY workspace ORDER BY n DESC LIMIT 10",
        )
        .all() as { workspace: string; n: number }[]
    ).map((row) => ({ workspace: safeField(row.workspace), sessions: Number(row.n) }))
    const alarmRow = db.prepare("SELECT v FROM corpus_meta WHERE k='stall.alarm'").get() as { v?: string } | undefined
    const alarm = alarmRow?.v === undefined || alarmRow.v === '' ? null : String(alarmRow.v)
    const streak = metaInt(db, 'stall.zero_streak')
    return {
      as_of: utcStamp(new Date()),
      scope: host !== null && host !== undefined && host !== '' ? { host } : {},
      segments: totalSegments,
      sessions: keptSessions,
      by_host: byHost,
      by_project: byProject,
      by_status: byStatus,
      degraded,
      orphan,
      unmapped_cwds: unmapped,
      window_days: metaInt(db, 'ingest.window_days'),
      skipped_by_window: metaInt(db, 'ingest.skipped_by_window'),
      lag_sec: lagSeconds(db),
      db_bytes: dbBytes(db),
      forgotten_sessions: forgottenSeen,
      tombstones: {
        session: scopes.sessions.size,
        host: scopes.hosts.size,
        pointer: scopes.pointers.length,
        range: scopes.ranges.length,
      },
      alarm,
      stall: { zero_streak: streak, alarm },
    }
  } finally {
    db.close()
  }
}

/** 台账命中的段要扣掉：数"这段会话还有几段是真活着"。 */
function countLiveSegments(
  db: DatabaseSync,
  scopes: ReturnType<typeof tombstoneScopes>,
  host: string,
  sessionId: string,
  fallback: number,
): number {
  const row = db
    .prepare('SELECT COUNT(*) AS n FROM corpus_segment WHERE host=? AND session_id=?')
    .get(host, sessionId) as { n?: number } | undefined
  const total = Number(row?.n ?? fallback)
  if (scopes.pointers.length === 0 && scopes.ranges.length === 0) return total
  const clauses: string[] = []
  const args: (string | number)[] = [host, sessionId]
  for (const pointer of scopes.pointers) {
    if (pointer.host !== host || pointer.sid !== sessionId) continue
    clauses.push('(loc=? AND part=?)')
    args.push(pointer.loc, pointer.part)
  }
  for (const range of scopes.ranges) {
    if (range.host !== null && range.host !== host) continue
    clauses.push('(occurred_at>=? AND occurred_at<=?)')
    args.push(range.start, range.end)
  }
  if (clauses.length === 0) return total
  const hit = db
    .prepare(`SELECT COUNT(*) AS n FROM corpus_segment WHERE host=? AND session_id=? AND (${clauses.join(' OR ')})`)
    .get(...args) as { n?: number } | undefined
  return Math.max(0, total - Number(hit?.n ?? 0))
}

// ---------------------------------------------------------------- CLI 渲染（与 run_cli 逐行一致）

/** `fish corpus search <q>` 的回执。 */
export function renderCorpusSearch(result: CorpusSearchResult): string {
  const lines = [
    `命中 ${result.meta.total_matched} 条 / 返回 ${result.meta.returned} 条（ranker=${result.meta.ranker}${result.meta.truncated ? '，已截断' : ''}）`,
  ]
  for (const hit of result.hits) {
    const title = hit.session_title !== '' ? hit.session_title : hit.session_id.slice(0, 8)
    lines.push(`- ${hit.pointer}  score=${hit.score.toFixed(3)}  ${title}  ${hit.snippet.slice(0, 60).replace(/\n/gu, ' ')}`)
    lines.push(`    理由：${hit.why.join('；')}`)
  }
  if (result.hits.length === 0) lines.push('  （没有命中：换个词，或先跑 `fish corpus ingest`）')
  for (const item of result.meta.degraded) lines.push(`  ⚠ ${item.reason}：${item.note}`)
  return lines.join('\n')
}

/** `fish corpus read <pointer>` 的回执（门牌错误也走这里，退 2）。 */
export function renderCorpusRead(result: CorpusReadResult, maxChars: number): string {
  const lines = [
    result.pointer,
    `as_of=${result.as_of}｜${result.occurred_at}｜${result.role}｜${result.project !== '' ? result.project : '未归属'}｜${result.session_title !== '' ? result.session_title : result.sid8}`,
    '',
    result.text,
    '',
  ]
  if (result.truncated) lines.push(`（已截断到 ${maxChars} 字；库内 ${result.chars} 字）`)
  if (result.source.missing) lines.push(`（${result.source.note}）`)
  return lines.join('\n')
}

/** `fish corpus read` 的门牌错误回执（`run_cli` 的 `PointerError` 分支）。 */
export function renderPointerError(error: PointerError): string {
  const lines = [`门牌错误（${error.code}）：${error.message}`]
  for (const candidate of error.candidates) lines.push(`  候选：${candidate}`)
  return lines.join('\n')
}

/** `fish corpus stats` 的回执：生产是 **JSON**（`run_command` 的 else 分支）。 */
export function renderCorpusStats(result: CorpusStatsResult): string {
  return JSON.stringify(result, null, 2)
}

/** 语料回执的固定三行头（MCP 面用；CLI 面不带——两处口径不同，如实分开）。 */
export function corpusHeader(third: string, now: Date = new Date()): string[] {
  return [CORPUS_SOURCE_LINE, `as_of=${isoLocal(now)}`, third]
}
