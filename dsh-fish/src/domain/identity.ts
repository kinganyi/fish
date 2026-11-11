/**
 * 身份层（只读）：凭证校验 / 证据分级 / `whoami` 回执。
 *
 * 依据（逐条对齐 Python）：
 *   · `lookup_session_token` / `validate_session_token`（fish.py:5191 / 5214）
 *   · `credential_state_line` / `identity_source_line`（fish.py:5264 / 5248）
 *   · `load_citizen` / `find_session_citizen` / `resolve_owner`（fish.py:5329 / 5315 / 4447）
 *   · `related_citizens` / `related_citizens_lines`（fish.py:4581 / 4650）
 *   · MCP `tool_whoami` 的**逐行文案**（mcp_server.py:1088），含 `IDENTITY_HINTS`（mcp_server.py:78）
 *
 * ## 两条不许越的线
 *
 * 1. **有效性判定只有一处**：`validateSessionToken`。吊销/过期/不存在都在它里面判完，
 *    调用方只读结果、不自己拼 SQL——两处实现必然漂，而"什么算失效"是凭证体系里最不该漂的。
 * 2. **证据级别只降不升**（`clampEvidence`）：凭证行里的 `evidence` 是**天花板**。
 *    影子期输入来自参数（token / session_key），会话键本身不是持有点——拿它声称
 *    `machine` 就是把自报当机器事实。`session_key` 单独使用时一律降为 `self-reported`，
 *    与 Python 侧 `build_session_identity` 的"env → machine、自报 → self-reported、
 *    都没有 → 匿名"同一套排序（fish.py:4999）。
 *
 * @module dsh-fish/domain/identity
 */

import { createHash } from 'node:crypto'
import path from 'node:path'
import type { DatabaseSync } from 'node:sqlite'

import { getConfigString } from './config.ts'

/** 证据级别（与 `SESSION_EVIDENCE_LEVELS` 同序）。 */
export const EVIDENCE_LEVELS = ['machine', 'inherited', 'self-reported'] as const
export type EvidenceLevel = (typeof EVIDENCE_LEVELS)[number]

/** 证据级别的高低：数字越大越可信。**只用来降级，不用来升级**。 */
const EVIDENCE_RANK: Readonly<Record<EvidenceLevel, number>> = {
  machine: 3,
  inherited: 2,
  'self-reported': 1,
}

/** 身份解析状态（对齐 mcp_server 的 `IDENTITY_*` 常量）。 */
export const IDENTITY_STATUS = {
  VALID: 'valid',
  NONE: 'none',
  INVALID: 'invalid',
  REVOKED: 'revoked',
  EXPIRED: 'expired',
} as const
export type IdentityStatus = (typeof IDENTITY_STATUS)[keyof typeof IDENTITY_STATUS]

/** 一次身份解析的结果。 */
export interface Identity {
  readonly status: IdentityStatus
  readonly citizenId: string | null
  readonly sessionKey: string | null
  /** 凭证行里的证据级别（可能是 `null`：没凭证 / 凭证坏）。 */
  readonly evidence: EvidenceLevel | null
}

/** 纯匿名身份（没带 token）。 */
export const IDENTITY_ANONYMOUS: Identity = {
  status: IDENTITY_STATUS.NONE,
  citizenId: null,
  sessionKey: null,
  evidence: null,
}

/** 三档身份来源文案（`IDENTITY_SOURCE_*`，MCP 与 CLI 共用）。 */
export const IDENTITY_SOURCE_TOKEN = '身份来源：token（session_tokens）'
export const IDENTITY_SOURCE_REGISTER = '身份来源：register 登记（不参与归属）'
export const IDENTITY_SOURCE_UNATTRIBUTED = '身份来源：未归属'

/** 凭证状态的固定文案（见 `credentialStateLine` 的五档）。 */
export const CREDENTIAL_NO_TOKEN = '凭证状态：未提供凭证（无凭证写入的归属会记成 unattributed）'
export const CREDENTIAL_UNKNOWN =
  '凭证状态：未知（这条 token 不在库中——明文只在签发时输出一次，丢了就重签：同一个 session_name → 同一个公民，只换凭证）'

/** `IDENTITY_HINTS`（mcp_server.py:78）——四种"为什么本次不能署名"的可执行下一步。 */
export const IDENTITY_HINTS: Readonly<Record<string, string>> = {
  [IDENTITY_STATUS.NONE]:
    '⚠ 本次调用未带会话凭证，归属记为“未归属”（unattributed）。\n' +
    '   拿凭证：**直接调 `session_start` 工具**（本进程内即可，不用跑 CLI）：\n' +
    '     · 已登记过 → session_start(citizen="<你的 citizen_id>", session_name="<稳定名字>")\n' +
    '     · 还没登记 → 先 register 拿 citizen_id，再按上面调\n' +
    '   拿到 token 后，之后每次调用带 token=...（明文只出现一次）。\n' +
    '   ⚠ 为什么不建议跑 CLI：CLI 的会话键与 MCP 不同，会建成另一个公民，两条通道的归属从此对不上且无法合并。',
  [IDENTITY_STATUS.INVALID]:
    '⚠ 凭证无效（session_tokens 表里没有这条 token），归属记为“未归属”（unattributed）。\n' +
    '   调 `session_start`（带同一个 session_name）换一条新凭证：公民不变，只换 token。',
  [IDENTITY_STATUS.REVOKED]:
    '⚠ 凭证已失效（被 `fish session end` 吊销），归属记为“未归属”（unattributed）。\n' +
    '   调 `session_start`（带同一个 session_name）换一条新凭证：公民不变，只换 token。',
  [IDENTITY_STATUS.EXPIRED]:
    '⚠ 凭证已失效（超过 TTL 自动过期），归属记为“未归属”（unattributed）。\n' +
    '   调 `session_start`（带同一个 session_name）换一条新凭证：公民不变，只换 token。',
}

/** 一行凭证表读取结果（对齐 `lookup_session_token` 的 dict 形状）。 */
export interface SessionTokenInfo {
  readonly token_hash: string
  readonly citizen_id: string
  readonly session_key: string
  readonly evidence: EvidenceLevel
  readonly channel: string | null
  readonly parent_token: string | null
  readonly issued_at: string | null
  readonly expires_at: string | null
  readonly revoked_at: string | null
  readonly expired: boolean
  readonly valid: boolean
}

/** 库里只存 `sha256(token)`（明文没有第二份副本）。 */
export function hashSessionToken(token: string): string {
  return createHash('sha256').update(token ?? '', 'utf8').digest('hex')
}

/** 当前 UTC 时间（`%Y-%m-%dT%H:%M:%SZ`，与库里的时间口径一致）。 */
export function nowUtc(at: Date = new Date()): string {
  return `${at.toISOString().slice(0, 19)}Z`
}

/** 解析定长 UTC 时间戳；解析不了返回 `null`（复刻 `parse_utc`，fish.py:320）。 */
export function parseUtc(value: string | null | undefined): Date | null {
  if (value === null || value === undefined || value === '') return null
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(value)) return null
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

/** `expires_at` 为空 = 永不失效；定长 UTC 的字典序就是时序（复刻 `_is_expired`）。 */
export function isExpired(expiresAt: string | null | undefined, at: string = nowUtc()): boolean {
  if (expiresAt === null || expiresAt === undefined || expiresAt === '') return false
  return expiresAt <= at
}

/** 到 `expires_at` 还剩几小时：未过期向上取整、至少 1；已过期返回 0（复刻 `remaining_token_hours`）。 */
export function remainingTokenHours(expiresAt: string, at: string = nowUtc()): number {
  if (isExpired(expiresAt, at)) return 0
  const moment = parseUtc(at)
  const expire = parseUtc(expiresAt)
  if (moment === null || expire === null) {
    throw new Error(`到期时刻无法解析：${JSON.stringify(expiresAt)}`)
  }
  const seconds = (expire.getTime() - moment.getTime()) / 1000
  return Math.max(1, Math.ceil(seconds / 3600))
}

/**
 * **证据级别只降不升**。
 *
 * @param claimed 调用方声称的级别（可空 = 没声称）
 * @param ceiling 凭证行/环境事实给出的级别（可空 = 没有任何证据）
 * @returns `effective` = 两者里**更低**的那个；`downgraded` = 是否真的降了
 */
export function clampEvidence(
  claimed: EvidenceLevel | null | undefined,
  ceiling: EvidenceLevel | null | undefined,
): { effective: EvidenceLevel | null; downgraded: boolean } {
  if (ceiling === null || ceiling === undefined) return { effective: null, downgraded: false }
  if (claimed === null || claimed === undefined) return { effective: ceiling, downgraded: false }
  const claimedRank = EVIDENCE_RANK[claimed] ?? 0
  const ceilingRank = EVIDENCE_RANK[ceiling] ?? 0
  if (claimedRank <= ceilingRank) return { effective: claimed, downgraded: false }
  return { effective: ceiling, downgraded: true }
}

/** 按明文查凭证行（**不判有效性**：吊销/过期也返回）；查不到返回 `null`。 */
export function lookupSessionToken(db: DatabaseSync, token: string | null | undefined): SessionTokenInfo | null {
  if (token === null || token === undefined || token === '') return null
  const row = db
    .prepare(
      'SELECT token_hash,citizen_id,session_key,evidence,channel,parent_token,issued_at,expires_at,revoked_at ' +
        'FROM session_tokens WHERE token_hash=?',
    )
    .get(hashSessionToken(token)) as Omit<SessionTokenInfo, 'expired' | 'valid'> | undefined
  if (row === undefined) return null
  const expired = isExpired(row.expires_at)
  return { ...row, expired, valid: row.revoked_at === null && !expired }
}

/**
 * **MCP 侧的唯一校验契约**：有效 → citizen_id；无效（吊销/过期/不存在）→ `null`。
 *
 * 对应的那条 SQL（fish.py:5217）：
 *   `token_hash=? AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now)`
 * 注意它**不是** SQL 里的一个 WHERE——Python 是取行后在 Python 里判，所以这里也照做：
 * 差异点在 `expires_at <= now`（含等号）与 `revoked_at <= now`，不是 `<`。
 */
export function validateSessionToken(db: DatabaseSync, token: string | null | undefined, at?: string): string | null {
  const info = lookupSessionToken(db, token)
  if (info === null) return null
  const moment = at ?? nowUtc()
  if (info.revoked_at !== null && info.revoked_at <= moment) return null
  if (info.expires_at !== null && info.expires_at <= moment) return null
  return info.citizen_id
}

/** 一行写明这次回执的身份是从哪来的（三档，MCP / CLI 共用）。 */
export function identitySourceLine(citizenId: string | null | undefined, options: { registered?: boolean } = {}): string {
  if (citizenId !== null && citizenId !== undefined && citizenId !== '') return IDENTITY_SOURCE_TOKEN
  return options.registered === true ? IDENTITY_SOURCE_REGISTER : IDENTITY_SOURCE_UNATTRIBUTED
}

/**
 * 本条凭证**处于什么状态**（一行五档）。
 *
 * 五档：没带凭证 / 不在库中 / 已吊销 / 已过期 / 永久·剩余 N 小时。
 * 判定不在这一层：吊销与到期都读 `lookupSessionToken` 的现查结果，
 * "什么算失效"永远只有 `validateSessionToken` 一个答案；这里只负责措辞。
 */
export function credentialStateLine(db: DatabaseSync, token: string | null | undefined): string {
  if (token === null || token === undefined || token === '') return CREDENTIAL_NO_TOKEN
  const info = lookupSessionToken(db, token)
  if (info === null) return CREDENTIAL_UNKNOWN
  if (info.revoked_at !== null && info.revoked_at !== '') {
    return `凭证状态：已吊销（${info.revoked_at}）——重跑 session_start 换一条`
  }
  if (info.expires_at === null || info.expires_at === '') return '凭证状态：永久（随会话 end 吊销）'
  if (parseUtc(info.expires_at) === null) {
    return (
      `凭证状态：到期时刻无法解析（${JSON.stringify(info.expires_at)}）` +
      '——这条凭证不算有效（归属会被记成 unattributed）；重跑 session_start 换一条'
    )
  }
  if (isExpired(info.expires_at)) {
    return `凭证状态：已过期（${info.expires_at}）；重跑 session_start 换一条`
  }
  try {
    return `凭证状态：剩余 ${remainingTokenHours(info.expires_at)} 小时`
  } catch {
    return `凭证状态：到期时刻无法解析（${JSON.stringify(info.expires_at)}）——重跑 session_start 换一条`
  }
}

/** 公民行（走 `citizens_all`：冷热一致，owner 永不悬空）。 */
export interface CitizenRow {
  readonly citizen_id: string
  readonly display_name: string | null
  readonly kind: string | null
  readonly role: string | null
  readonly host: string | null
  readonly status: string | null
  readonly account_id: string | null
  readonly host_session_key: string | null
  readonly last_seen_at: string | null
  readonly archived_at: string | null
}

const CITIZEN_COLUMNS =
  'citizen_id,display_name,kind,role,host,status,account_id,host_session_key,last_seen_at,archived_at'

/** 读一个公民（走 `citizens_all`）；查不到返回 `null`。 */
export function loadCitizen(db: DatabaseSync, citizenId: string | null | undefined): CitizenRow | null {
  if (citizenId === null || citizenId === undefined || citizenId === '') return null
  const row = db
    .prepare(`SELECT ${CITIZEN_COLUMNS} FROM citizens_all WHERE citizen_id=?`)
    .get(citizenId) as CitizenRow | undefined
  return row ?? null
}

/** 按宿主会话标识找**活跃**会话公民（走 `citizens_all`）。 */
export function findSessionCitizen(db: DatabaseSync, sessionKey: string | null | undefined): CitizenRow | null {
  if (sessionKey === null || sessionKey === undefined || sessionKey === '') return null
  const row = db
    .prepare(
      `SELECT ${CITIZEN_COLUMNS} FROM citizens_all WHERE host_session_key=? AND status='active' ORDER BY created_at LIMIT 1`,
    )
    .get(sessionKey) as CitizenRow | undefined
  return row ?? null
}

/** 系统主人：优先 `citizens.is_owner`；回退旧配置（`owner_citizen` → `default_citizen`）。 */
export function resolveOwner(db: DatabaseSync): string {
  const row = db.prepare("SELECT citizen_id FROM citizens WHERE is_owner=1 AND status='active'").get() as
    | { citizen_id: string }
    | undefined
  if (row !== undefined) return row.citizen_id
  return getConfigString(db, 'owner_citizen') || getConfigString(db, 'default_citizen')
}

/** 相关公民清单的关联锚（白名单：列名只能来自实现内部常量表）。 */
export const RELATED_ANCHORS = [
  ['session', 'host_session_key'],
  ['name', 'display_name'],
  ['account', 'account_id'],
] as const

/** 相关公民清单的硬上限（`whoami all=true`）。 */
export const RELATED_CITIZENS_LIMIT = 20

/** 一条相关公民（对齐 `related_citizens` 的 dict）。 */
export interface RelatedCitizen {
  readonly citizen_id: string
  readonly display_name: string | null
  readonly kind: string | null
  readonly status: string | null
  readonly host: string | null
  readonly last_seen_at: string | null
  readonly created_at: string | null
  relations: string[]
  token_count: number
}

/**
 * 与本会话上下文相关的公民（`whoami all=true` 的唯一取数口）：三条锚任一命中即算相关，
 * 按 citizen_id 去重。**不是全库清单**：没有锚就空转返回。
 */
export function relatedCitizens(
  db: DatabaseSync,
  options: {
    displayName?: string | null
    hostSessionKey?: string | null
    accountId?: string | null
    excludeCitizenId?: string | null
    limit?: number
  } = {},
): RelatedCitizen[] {
  const values: Record<string, string | null | undefined> = {
    session: options.hostSessionKey,
    name: options.displayName,
    account: options.accountId,
  }
  const columns = 'citizen_id,display_name,kind,status,host,last_seen_at,created_at'
  const found = new Map<string, RelatedCitizen>()
  for (const [relation, column] of RELATED_ANCHORS) {
    const raw = values[relation]
    const anchor = typeof raw === 'string' ? raw.trim() : raw
    if (anchor === null || anchor === undefined || anchor === '') continue
    const rows = db
      .prepare(
        `SELECT ${columns} FROM citizens WHERE ${column}=? ` +
          "ORDER BY CASE WHEN status='active' THEN 0 ELSE 1 END, created_at, citizen_id",
      )
      .all(anchor) as Array<Omit<RelatedCitizen, 'relations' | 'token_count'>>
    for (const row of rows) {
      let item = found.get(row.citizen_id)
      if (item === undefined) {
        item = { ...row, relations: [], token_count: 0 }
        found.set(row.citizen_id, item)
      }
      if (!item.relations.includes(relation)) item.relations.push(relation)
    }
  }
  if (options.excludeCitizenId) found.delete(options.excludeCitizenId)
  if (found.size === 0) return []
  const ids = [...found.keys()]
  const placeholders = ids.map(() => '?').join(',')
  const counts = new Map<string, number>()
  const countRows = db
    .prepare(
      'SELECT citizen_id,COUNT(*) AS n FROM session_tokens WHERE revoked_at IS NULL ' +
        `AND citizen_id IN (${placeholders}) GROUP BY citizen_id`,
    )
    .all(...ids) as Array<{ citizen_id: string; n: number }>
  for (const row of countRows) counts.set(row.citizen_id, Number(row.n))
  const order = new Map(RELATED_ANCHORS.map(([relation], index) => [relation as string, index]))
  const items = [...found.values()].sort((a, b) => {
    const activeA = a.status === 'active' ? 0 : 1
    const activeB = b.status === 'active' ? 0 : 1
    if (activeA !== activeB) return activeA - activeB
    const minA = Math.min(...a.relations.map((r) => order.get(r) ?? 99))
    const minB = Math.min(...b.relations.map((r) => order.get(r) ?? 99))
    if (minA !== minB) return minA - minB
    const createdA = a.created_at ?? ''
    const createdB = b.created_at ?? ''
    if (createdA !== createdB) return createdA < createdB ? -1 : 1
    return a.citizen_id < b.citizen_id ? -1 : a.citizen_id > b.citizen_id ? 1 : 0
  })
  for (const item of items) {
    item.relations = RELATED_ANCHORS.map(([relation]) => relation as string).filter((relation) =>
      item.relations.includes(relation),
    )
    item.token_count = counts.get(item.citizen_id) ?? 0
  }
  const cut = Math.trunc(options.limit ?? RELATED_CITIZENS_LIMIT)
  return cut > 0 ? items.slice(0, cut) : []
}

/** `whoami all=true`（MCP）与 `whoami --all`（CLI）的**唯一渲染**。 */
export function relatedCitizensLines(db: DatabaseSync, citizenId: string | null | undefined): string[] {
  const head =
    '相关公民（**不含你自己**；按 会话/同名/账号 关联，不是全库清单；' +
    `token 数 = 未吊销凭证数，最多 ${RELATED_CITIZENS_LIMIT} 条）：`
  if (citizenId === null || citizenId === undefined || citizenId === '') {
    return [
      head,
      '- （未列出）这份清单需要一条**有效 token** 当锚：没凭证就没有会话键、公民名与账号可关联——这不等于“库里没有人”。',
    ]
  }
  const me = loadCitizen(db, citizenId) ?? ({} as Partial<CitizenRow>)
  const rows = relatedCitizens(db, {
    displayName: me.display_name ?? null,
    hostSessionKey: me.host_session_key ?? null,
    accountId: me.account_id ?? null,
    excludeCitizenId: citizenId,
  })
  if (rows.length === 0) return [head, '- （没有匹配到相关公民：本会话、同名、同账号都没有别人）']
  const lines = [head]
  for (const it of rows) {
    lines.push(
      `- ${it.citizen_id}｜${it.display_name || '（无名）'}｜${it.kind || 'unknown'}｜${it.status || 'unknown'}｜` +
        `宿主 ${it.host || '未记录'}｜最近活跃 ${it.last_seen_at || '未记录'}｜` +
        `token ${it.token_count}｜关联：${it.relations.join('/')}`,
    )
  }
  if (rows.length >= RELATED_CITIZENS_LIMIT) {
    lines.push(`（已达上限 ${RELATED_CITIZENS_LIMIT} 条，可能还有更多；全库清单请用 \`fish citizen list\`）`)
  }
  return lines
}

/** 同名告警（只对**有效 token** 的 whoami 说；已退役的同名者不算）。 */
export function sameNameWarnings(db: DatabaseSync, citizenId: string | null | undefined): string[] {
  if (citizenId === null || citizenId === undefined || citizenId === '') return []
  const me = loadCitizen(db, citizenId)
  const name = (me?.display_name ?? '').trim()
  if (name === '') return []
  const rows = relatedCitizens(db, { displayName: name, excludeCitizenId: citizenId })
  const out: string[] = []
  for (const it of rows) {
    if (!it.relations.includes('name') || it.status !== 'active') continue
    out.push(
      `⚠ 检测到同名公民 ${it.citizen_id}（${it.kind || 'unknown'}，最近活跃 ${it.last_seen_at || '未记录'}）` +
        `——系统不合并；本次写入记在 ${citizenId} 名下`,
    )
  }
  return out
}

/** CLI 入口那一行（FB-13）：`fishHome` = 含 `fish.py` 的目录。 */
export function cliEntryHint(fishHome: string): string {
  const p0 = fishHome === '' ? '' : path.resolve(fishHome)
  const joined = path.join(p0 === '' ? '.' : p0, 'fish.py')
  return `CLI 入口：python ${joined}（或 install_cli.py --apply 生成 fish.cmd）`
}

/** 已经自己讲清身份状况的工具：**不再追加**通用提示（否则同一句话出现两遍）。 */
export const HINT_SELF_EXPLAINED = ['whoami', 'register', 'session_start'] as const

/**
 * 归属提示（MCP `call_tool` 在**这一层**统一追加，mcp_server.py:1968）。
 *
 * 为什么必须也复刻这一层：漏了它，TS 侧回执就比模型今天看到的少一整段
 * （"你的归属会记成 unattributed" + 怎么拿凭证）——而那段话是**唯一**告诉读者
 * "这次写入不算你的"的地方。追加规则与 Python 逐字一致：
 * `text.rstrip("\n") + "\n\n" + hint + "\n"`。
 */
export function appendIdentityHint(text: string, identity: Identity, toolName: string): string {
  if ((HINT_SELF_EXPLAINED as readonly string[]).includes(toolName)) return text
  const hint = IDENTITY_HINTS[identity.status]
  if (hint === undefined) return text
  return `${text.replace(/\n+$/, '')}\n\n${hint}\n`
}

/**
 * 把"本次调用带的凭证"解析成 `Identity`（**纯函数：不写任何状态**）。
 *
 * 与 MCP `_resolve_identity`（mcp_server.py:631）同口径：
 *   · 没 token → `none`；
 *   · 有 token 但库里查不到 → `invalid`；
 *   · 查得到但已吊销 / 已过期 → `revoked` / `expired`（**只为选提示文案**，判定仍只有一处）；
 *   · 有效 → `valid`，`session_key` / `evidence` 取**凭证行里的值**（不取环境变量）。
 */
export function resolveIdentity(db: DatabaseSync, token: string | null | undefined): Identity {
  if (token === null || token === undefined || token === '') return IDENTITY_ANONYMOUS
  const citizenId = validateSessionToken(db, token)
  const info = lookupSessionToken(db, token)
  if (info === null) {
    return { status: IDENTITY_STATUS.INVALID, citizenId: null, sessionKey: null, evidence: null }
  }
  if (citizenId === null) {
    const status = info.revoked_at !== null && info.revoked_at !== ''
      ? IDENTITY_STATUS.REVOKED
      : info.expired
        ? IDENTITY_STATUS.EXPIRED
        : IDENTITY_STATUS.INVALID
    return { status, citizenId: null, sessionKey: null, evidence: info.evidence }
  }
  return {
    status: IDENTITY_STATUS.VALID,
    citizenId,
    sessionKey: info.session_key,
    evidence: info.evidence,
  }
}

/**
 * 影子期的身份入参：`{ token?, sessionKey?, evidenceClaim? }`。
 * **不签发**——只读 `session_tokens`；没凭证就是没凭证。
 */
export interface WhoamiInput {
  readonly token?: string | null
  readonly sessionKey?: string | null
  /** 调用方声称的证据级别；高于凭证天花板时被**降级**（`clampEvidence`）。 */
  readonly evidenceClaim?: EvidenceLevel | null
  /** `all=true`：附相关公民清单。 */
  readonly all?: boolean
}

/** `whoami` 的结构化读数（供判据与命令面使用；文案由 `renderWhoami` 生成）。 */
export interface WhoamiReport {
  readonly status: IdentityStatus
  readonly citizen_id: string | null
  readonly display_name: string | null
  readonly kind: string | null
  readonly session_key: string | null
  /** 凭证行给出的证据级别（天花板）。 */
  readonly evidence_ceiling: EvidenceLevel | null
  /** 本次生效的证据级别（只降不升）。 */
  readonly evidence_effective: EvidenceLevel | null
  readonly evidence_downgraded: boolean
  /** 身份来源：`token` / `session_key` / `none`。 */
  readonly citizen_source: 'token' | 'session_key' | 'none'
  readonly owner_id: string
}

/**
 * 解析一次 `whoami`（token 优先；没有 token 时按 session_key 兜底）。
 *
 * 为什么要 session_key 兜底：影子期没有 `session_start` 工具，运维现场常常只有会话键。
 * 但 session_key **不是持有点**——所以它给的证据级别只能是 `self-reported`，
 * 并且 `status` 仍是 `valid`（会话键确实对应一个活跃公民，写入归属却不是它的功劳）。
 */
export function whoami(db: DatabaseSync, input: WhoamiInput = {}): WhoamiReport {
  const token = (input.token ?? '').trim()
  const sessionKey = (input.sessionKey ?? '').trim()
  let identity = resolveIdentity(db, token === '' ? null : token)
  let citizenSource: WhoamiReport['citizen_source'] = token === '' ? 'none' : 'token'
  let ceiling = identity.evidence
  if (identity.status !== IDENTITY_STATUS.VALID && sessionKey !== '') {
    const citizen = findSessionCitizen(db, sessionKey)
    if (citizen !== null) {
      identity = {
        status: IDENTITY_STATUS.VALID,
        citizenId: citizen.citizen_id,
        sessionKey,
        evidence: 'self-reported',
      }
      citizenSource = 'session_key'
      // 会话键路径的天花板是 self-reported：这里**显式**压一次，免得将来有人从
      // token 分支顺手带上来一个 machine。
      ceiling = 'self-reported'
    }
  }
  const citizen = identity.citizenId === null ? null : loadCitizen(db, identity.citizenId)
  const clamped = clampEvidence(input.evidenceClaim ?? null, ceiling)
  return {
    status: identity.status,
    citizen_id: identity.citizenId,
    display_name: citizen?.display_name ?? null,
    kind: citizen?.kind ?? null,
    session_key: identity.sessionKey,
    evidence_ceiling: ceiling,
    evidence_effective: clamped.effective,
    evidence_downgraded: clamped.downgraded,
    citizen_source: citizenSource,
    owner_id: resolveOwner(db),
  }
}

/**
 * `whoami` 的**完整回执文本**（逐行对齐 MCP `tool_whoami`，mcp_server.py:1088）。
 *
 * @param fishHome 含 `fish.py` 的目录（CLI 入口行要用；由调用方给，模块不猜）
 */
export function renderWhoami(db: DatabaseSync, input: WhoamiInput, fishHome: string): { text: string; report: WhoamiReport } {
  const report = whoami(db, input)
  const token = (input.token ?? '').trim()
  const lines: string[] = []
  if (report.status === IDENTITY_STATUS.VALID) {
    const name = report.display_name ?? report.citizen_id ?? ''
    const kind = report.kind ?? 'unknown'
    lines.push(`登记身份：${name}（公民 ${report.citizen_id ?? ''}，形态 ${kind}）`)
    lines.push(
      `凭证：会话 ${report.session_key ?? ''}｜证据 ${report.evidence_ceiling ?? ''}` +
        '（来自 session_tokens 表，本次调用现查，不存进程内存）',
    )
    lines.push('说明：这是你在 fish 的账户名（宿主级），与你的宿主身份不冲突。')
  } else {
    lines.push('身份未核：本次调用没有可用的会话凭证，无法回答“我是谁”。')
    lines.push(
      '（fish 不再保留进程内身份：一个 MCP 进程被所有会话共享，“记住最后一个 register 的人”正是 2026-10-23 串号事故的根因。）',
    )
    lines.push(IDENTITY_HINTS[report.status] ?? '')
  }
  lines.push(credentialStateLine(db, token === '' ? null : token))
  lines.push(identitySourceLine(report.status === IDENTITY_STATUS.VALID ? report.citizen_id : null))
  lines.push(...sameNameWarnings(db, report.status === IDENTITY_STATUS.VALID ? report.citizen_id : null))
  if (report.owner_id !== '') {
    const owner = loadCitizen(db, report.owner_id)
    if (owner !== null) {
      lines.push(`主人：${owner.display_name ?? ''}（公民 ${report.owner_id}，形态 ${owner.kind ?? ''}）`)
    }
  } else {
    lines.push('主人：未设置（fish config set owner_citizen <id>）')
  }
  if (input.all === true) {
    lines.push('')
    lines.push(...relatedCitizensLines(db, report.status === IDENTITY_STATUS.VALID ? report.citizen_id : null))
  }
  lines.push(cliEntryHint(fishHome))
  return { text: `${lines.join('\n')}\n`, report }
}
