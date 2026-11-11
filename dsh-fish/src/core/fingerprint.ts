/**
 * schema 指纹：把"库结构长什么样"变成一个可复算的字符串。
 *
 * 为什么影子期第一件事就是它：TS 侧只读、不迁移，但**读错结构**和"结构变了但没人告诉
 * 插件"是同一种坏——后者会让投影查询静默返回空集。指纹是唯一能在启动一瞬间说
 * "你读的库不是我以为的那个库"的手段。
 *
 * 规范化规则（必须可复算，写进产物里自证）：
 *   1. 只取 `sqlite_master.sql` 非空的条目（自动索引没有 SQL，本来就不该进指纹）；
 *   2. SQL 文本：删行尾 `;`、连续空白折叠成单个空格、首尾去空白；
 *   3. 按 `(type, name)` 排序（顺序无关 >> 顺序相关，否则导出顺序变化会造假差异）；
 *   4. 对 `[{type,name,table,sql}]` 的紧凑 JSON 求 sha256。
 * 另给一份 `businessFingerprint`：排除 FTS 影子表（`memory_fts_*`）与 `sqlite_sequence`
 * ——它们是 SQLite 的实现细节，重建/优化时可能变化，但**业务结构没变**。
 *
 * @module dsh-fish/core/fingerprint
 */

import { createHash } from 'node:crypto'
import type { DatabaseSync } from 'node:sqlite'

import { DatabaseError } from './errors.ts'

/** 一个 schema 对象（表 / 视图 / 索引 / 触发器）。 */
export interface SchemaObject {
  readonly type: string
  readonly name: string
  readonly table: string
  /** 规范化后的 DDL 文本。 */
  readonly sql: string
  /** 该对象的 sha256（规范化 JSON 的前 16 位，够用且好读）。 */
  readonly hash: string
}

/** 完整指纹产物（同时是 `spec/schema.fingerprint.json` 的形状）。 */
export interface SchemaFingerprint {
  /** 恒为 `sha256`：写进产物，防止将来换算法后拿旧产物误判。 */
  readonly algorithm: 'sha256'
  /** 规范化规则清单（产物自证：读的人不用猜怎么复算）。 */
  readonly normalization: readonly string[]
  /** 指纹来源表（恒为 `sqlite_master`）。 */
  readonly source: 'sqlite_master'
  /** 各类对象计数。 */
  readonly counts: Readonly<Record<string, number>>
  /** 全量指纹（含 FTS 影子表）。 */
  readonly fingerprint: string
  /** 业务指纹（排除 FTS 影子表与 sqlite_sequence）。 */
  readonly businessFingerprint: string
  /** 逐对象明细（顺序已规范化）。 */
  readonly objects: readonly SchemaObject[]
}

/** 规范化规则清单：产物的 `normalization` 字段直接引用它，避免两处措辞。 */
export const NORMALIZATION_RULES = [
  'select sqlite_master rows where sql is not null',
  'sql: strip trailing semicolons',
  'sql: collapse whitespace runs to single space',
  'sql: trim',
  'sort objects by (type, name) asc',
  'sha256 over compact json of [{type,name,table,sql}]',
  'businessFingerprint: same, excluding memory_fts_* and sqlite_sequence',
] as const

/** 影子对象：SQLite 为实现 FTS5 自动建的表，不属于业务结构。 */
export function isShadowObject(name: string): boolean {
  return name.startsWith('memory_fts_') || name === 'sqlite_sequence'
}

/** SQL 文本规范化（规则 2/3 的唯一实现）。 */
export function normalizeSql(sql: string): string {
  return sql
    .replace(/[\t\r\n]+/g, ' ')
    .replace(/ {2,}/g, ' ')
    .replace(/\s*;\s*$/g, '')
    .trim()
}

/** sha256 十六进制串。 */
function sha256(text: string): string {
  return createHash('sha256').update(text, 'utf8').digest('hex')
}

/** 对一组对象算指纹（供全量与业务两档复用）。 */
function fingerprintOf(objects: readonly SchemaObject[]): string {
  const canonical = JSON.stringify(objects.map((o) => ({ type: o.type, name: o.name, table: o.table, sql: o.sql })))
  return sha256(canonical)
}

/**
 * 从只读连接算指纹。
 * @param db 已打开的连接（调用方负责只读）
 */
export function computeSchemaFingerprint(db: DatabaseSync): SchemaFingerprint {
  let rows: { type: string; name: string; tbl_name: string; sql: string }[]
  try {
    rows = db
      .prepare(
        "SELECT type, name, tbl_name AS tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type ASC, name ASC",
      )
      .all() as { type: string; name: string; tbl_name: string; sql: string }[]
  } catch (error) {
    throw new DatabaseError('DB_UNAVAILABLE', `读取 sqlite_master 失败：${(error as Error).message}`, {
      hint: '确认路径指向 fish 的 index.sqlite，且以只读方式能打开',
      cause: error,
    })
  }

  const objects: SchemaObject[] = rows.map((row) => {
    const sql = normalizeSql(row.sql)
    return {
      type: row.type,
      name: row.name,
      table: row.tbl_name,
      sql,
      hash: sha256(`${row.type}\u0000${row.name}\u0000${row.tbl_name}\u0000${sql}`).slice(0, 16),
    }
  })

  const counts: Record<string, number> = {}
  for (const object of objects) counts[object.type] = (counts[object.type] ?? 0) + 1

  const business = objects.filter((object) => !isShadowObject(object.name))

  return {
    algorithm: 'sha256',
    normalization: NORMALIZATION_RULES,
    source: 'sqlite_master',
    counts,
    fingerprint: fingerprintOf(objects),
    businessFingerprint: fingerprintOf(business),
    objects,
  }
}

/** 两档指纹的差异摘要（启动门禁报错时给人看的那几行）。 */
export interface FingerprintDelta {
  readonly fingerprintMatched: boolean
  readonly businessMatched: boolean
  /** 只在 spec 里有（库缺）。 */
  readonly missing: readonly string[]
  /** 只在库里（spec 缺）。 */
  readonly unexpected: readonly string[]
  /** 同名但 DDL 不同。 */
  readonly changed: readonly string[]
}

/** 逐对象对比：比"两个 hash 不等"有用得多——它直接指出是哪张表变了。 */
export function diffFingerprints(expected: SchemaFingerprint, actual: SchemaFingerprint): FingerprintDelta {
  const key = (o: SchemaObject): string => `${o.type}:${o.name}`
  const expectedMap = new Map(expected.objects.map((o) => [key(o), o]))
  const actualMap = new Map(actual.objects.map((o) => [key(o), o]))

  const missing: string[] = []
  for (const k of expectedMap.keys()) if (!actualMap.has(k)) missing.push(k)

  const unexpected: string[] = []
  const changed: string[] = []
  for (const [k, actualObject] of actualMap) {
    const expectedObject = expectedMap.get(k)
    if (expectedObject === undefined) {
      unexpected.push(k)
    } else if (expectedObject.hash !== actualObject.hash) {
      changed.push(k)
    }
  }

  return {
    fingerprintMatched: expected.fingerprint === actual.fingerprint,
    businessMatched: expected.businessFingerprint === actual.businessFingerprint,
    missing: missing.sort(),
    unexpected: unexpected.sort(),
    changed: changed.sort(),
  }
}
