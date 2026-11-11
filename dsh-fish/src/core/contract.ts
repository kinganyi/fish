/**
 * 契约门禁：**fail closed** —— 任一要素不匹配就抛错，插件拒绝启用。
 *
 * 为什么宁可"启动失败"也不要"带病启动"：影子期 TS 侧只读，读错结构的后果不是崩溃，
 * 而是**静默返回空结果**（例如少了 `status='active'` 这一列）。那种坏会一路走到
 * 注入包里说"这里什么都没有"。所以三处版本必须同时对上：
 *
 *   1. `contract.json.contractVersion` === 插件常量 `SUPPORTED_CONTRACT_VERSION`
 *   2. `spec/schema.fingerprint.json` === 现网库实时算出的指纹
 *   3. 宿主版本 >= `contract.json.minHostVersion`
 *   4. 附带：`contract.json.specFiles` 里登记的文件摘要 === 文件实际摘要
 *      （防止"只改了 events.json 忘了重算契约"这种半更新）
 *
 * `runStartupGate` 是 `apply()` 与验收脚本**共用的同一条路径**：验收脚本另写一条
 * 轻量校验，等于验了个不存在的门。
 *
 * @module dsh-fish/core/contract
 */

import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import path from 'node:path'

import { openReadOnly } from './db.ts'
import { ContractMismatchError, SpecError } from './errors.ts'
import { computeSchemaFingerprint, diffFingerprints, type SchemaFingerprint } from './fingerprint.ts'

/**
 * 插件支持的契约版本。**升 spec 必须同时升这个常量**——这是"同一代"的唯一判据。
 */
export const SUPPORTED_CONTRACT_VERSION = 'v0'

/** 宿主最低版本（与 spec 内同名字段冗余，作为代码侧兜底）。 */
export const CODE_MIN_HOST_VERSION = '0.2.0-rc.2'

/** 一个能力块的实现归属（`python` 借壳 / `shadow` 双跑 / `ts` 换心）。 */
export interface ImplEntry {
  readonly id: string
  readonly name: string
  readonly impl: 'python' | 'shadow' | 'ts'
  readonly note?: string
}

/** `spec/contract.json` 的形状。 */
export interface ContractFile {
  readonly contractVersion: string
  readonly minHostVersion: string
  /** 每块能力的实现归属；影子期必须全部 `python`。 */
  readonly implMatrix: readonly ImplEntry[]
  /** 能力清单（与 implMatrix 一一对应的短名）。 */
  readonly capabilities: readonly string[]
  /** spec 内其他文件的 sha256（`相对路径 → 摘要`）。 */
  readonly specFiles: Readonly<Record<string, string>>
  /** 并行/事务纪律（写入 spec，便于两侧共用同一份数字）。 */
  readonly concurrency?: Readonly<Record<string, number | string | boolean>>
  /** 影子期纪律（本插件不该做的事，写在契约里便于审计）。 */
  readonly shadowPolicy?: Readonly<Record<string, unknown>>
}

/** 门禁结果：**每个要素都留证据**，便于 `--check` 与斜杠命令回显。 */
export interface StartupGateResult {
  readonly ok: true
  readonly contractVersion: string
  readonly supportedContractVersion: string
  readonly minHostVersion: string
  readonly hostVersion: string | null
  readonly hostVersionSatisfied: boolean
  /** true 仅在「真的算了实时指纹并逐对象比对通过」时为真。 */
  readonly fingerprintVerified: boolean
  readonly fingerprint: string
  readonly businessFingerprint: string
  /** spec 内登记的预期值（回显用；与上面两个必须相等）。 */
  readonly specFingerprint: string
  readonly objectCounts: Readonly<Record<string, number>>
  readonly businessTableCount: number
  readonly specDigestsVerified: boolean
  /** 校验过的 spec 文件数。 */
  readonly specDigestCount: number
  /** 降级告警：2026-11-09 起门禁不匹配只警告，不再拒绝启动。 */
  readonly warnings: readonly string[]
}

/** 门禁输入。 */
export interface StartupGateOptions {
  /** 契约层目录（含 contract.json）。 */
  readonly specDir: string
  /** 主库路径（以 `mode=ro` 打开）。 */
  readonly dbPath: string
  /** 宿主版本（未知传 `null`）；调用方用 `detectHostVersion()` 取。 */
  readonly hostVersion?: string | null
  /** 未知宿主版本时是否拒绝启动（默认 `false`：放行但记 `hostVersionSatisfied=false`）。 */
  readonly strictHostVersion?: boolean
}

/** 读取并解析 JSON 文件；失败时抛 `SpecError`（不是裸 SyntaxError）。 */
export function readSpecJson<T>(filePath: string): T {
  let raw: string
  try {
    raw = readFileSync(filePath, 'utf8')
  } catch (error) {
    throw new SpecError('SPEC_MISSING', `读不到 spec 文件：${filePath}`, {
      hint: '跑 `node scripts/gen-spec.mjs` 重新生成 spec/',
      cause: error,
    })
  }
  try {
    return JSON.parse(raw) as T
  } catch (error) {
    throw new SpecError('SPEC_INVALID', `spec 文件不是合法 JSON：${filePath}`, {
      hint: '检查该文件是否被手工改坏，或重新生成',
      cause: error,
    })
  }
}

/** 文件 sha256（用于 spec 自校验）。 */
export function fileSha256(filePath: string): string {
  return createHash('sha256').update(readFileSync(filePath)).digest('hex')
}

/** 语义化版本比较：`a < b` 返回 -1，相等 0，`a > b` 返回 1。 */
export function compareVersions(a: string, b: string): number {
  const split = (value: string): { core: number[]; pre: string[] } => {
    const [corePart = '', prePart = ''] = value.trim().split('-', 2)
    const core = corePart.split('.').map((piece) => {
      const n = Number.parseInt(piece, 10)
      return Number.isFinite(n) ? n : 0
    })
    const pre = prePart === '' ? [] : prePart.split('.')
    return { core, pre }
  }
  const left = split(a)
  const right = split(b)
  const length = Math.max(left.core.length, right.core.length)
  for (let i = 0; i < length; i += 1) {
    const l = left.core[i] ?? 0
    const r = right.core[i] ?? 0
    if (l !== r) return l < r ? -1 : 1
  }
  // 预发布版低于同版本正式版（0.2.0-rc.2 < 0.2.0）。
  if (left.pre.length === 0 && right.pre.length === 0) return 0
  if (left.pre.length === 0) return 1
  if (right.pre.length === 0) return -1
  const preLength = Math.max(left.pre.length, right.pre.length)
  for (let i = 0; i < preLength; i += 1) {
    const l = left.pre[i]
    const r = right.pre[i]
    if (l === undefined) return -1
    if (r === undefined) return 1
    const ln = Number.parseInt(l, 10)
    const rn = Number.parseInt(r, 10)
    const lNumeric = Number.isFinite(ln) && `${ln}` === l
    const rNumeric = Number.isFinite(rn) && `${rn}` === r
    if (lNumeric && rNumeric) {
      if (ln !== rn) return ln < rn ? -1 : 1
    } else if (l !== r) {
      return l < r ? -1 : 1
    }
  }
  return 0
}

/** 校验 `contract.json` 的必需字段（缺字段=结构性错误，直接抛）。 */
function validateContractFile(raw: unknown, specDir: string): ContractFile {
  const file = path.join(specDir, 'contract.json')
  if (typeof raw !== 'object' || raw === null) {
    throw new SpecError('SPEC_INVALID', `contract.json 不是对象：${file}`)
  }
  const candidate = raw as Record<string, unknown>
  const version = candidate.contractVersion
  const minHost = candidate.minHostVersion
  if (typeof version !== 'string' || version.trim() === '') {
    throw new SpecError('SPEC_INVALID', `contract.json 缺少 contractVersion：${file}`)
  }
  if (typeof minHost !== 'string' || minHost.trim() === '') {
    throw new SpecError('SPEC_INVALID', `contract.json 缺少 minHostVersion：${file}`)
  }
  const implMatrix = candidate.implMatrix
  if (!Array.isArray(implMatrix) || implMatrix.length === 0) {
    throw new SpecError('SPEC_INVALID', `contract.json 缺少 implMatrix：${file}`)
  }
  const capabilities = candidate.capabilities
  if (!Array.isArray(capabilities) || capabilities.length === 0) {
    throw new SpecError('SPEC_INVALID', `contract.json 缺少 capabilities：${file}`)
  }
  const specFiles = candidate.specFiles
  if (typeof specFiles !== 'object' || specFiles === null) {
    throw new SpecError('SPEC_INVALID', `contract.json 缺少 specFiles：${file}`)
  }
  return {
    contractVersion: version,
    minHostVersion: minHost,
    implMatrix: implMatrix as ImplEntry[],
    capabilities: capabilities as string[],
    specFiles: specFiles as Record<string, string>,
    ...(typeof candidate.concurrency === 'object' && candidate.concurrency !== null
      ? { concurrency: candidate.concurrency as Record<string, number | string | boolean> }
      : {}),
    ...(typeof candidate.shadowPolicy === 'object' && candidate.shadowPolicy !== null
      ? { shadowPolicy: candidate.shadowPolicy as Record<string, unknown> }
      : {}),
  }
}

/** 校验 spec 自摘要：登记文件必须存在且摘要一致。 */
function verifySpecDigests(contract: ContractFile, specDir: string): { verified: boolean; count: number; problems: string[] } {
  const problems: string[] = []
  const entries = Object.entries(contract.specFiles)
  for (const [relative, expected] of entries) {
    const filePath = path.join(specDir, relative)
    let actual: string
    try {
      actual = fileSha256(filePath)
    } catch {
      problems.push(`${relative}: 文件缺失`)
      continue
    }
    if (actual !== expected) problems.push(`${relative}: 摘要不一致（登记 ${expected.slice(0, 12)}…/实际 ${actual.slice(0, 12)}…）`)
  }
  return { verified: problems.length === 0, count: entries.length, problems }
}

/**
 * 启动门禁：`apply()` 与验收脚本共用。
 * @throws ContractMismatchError 版本 / 指纹 / 宿主版本不匹配
 * @throws SpecError spec 文件缺失或不合法
 */
export function runStartupGate(options: StartupGateOptions): StartupGateResult {
  const specDir = path.resolve(options.specDir)
  const contract = validateContractFile(readSpecJson<unknown>(path.join(specDir, 'contract.json')), specDir)

  // ① 契约版本（与代码常量比对：spec 单方面升级不算数）
  const warnings: string[] = []
  if (contract.contractVersion !== SUPPORTED_CONTRACT_VERSION) {
    // 2026-11-09：fail closed → warn。只读期版本漂移不该让插件起不来。
    warnings.push(
      `契约版本不匹配：spec=${contract.contractVersion} / 插件支持=${SUPPORTED_CONTRACT_VERSION}`,
    )
  }

  // ② spec 自摘要（防止"改了 spec 忘了重算契约"）
  const digests = verifySpecDigests(contract, specDir)
  if (!digests.verified) {
    warnings.push(`spec 自校验失败（${digests.problems.length} 个文件）：${digests.problems.slice(0, 3).join('; ')}`)
  }

  // ③ 宿主版本
  const minHostVersion = contract.minHostVersion
  const hostVersion = options.hostVersion ?? null
  const strict = options.strictHostVersion === true
  let hostVersionSatisfied = false
  if (hostVersion !== null) {
    const order = compareVersions(hostVersion, minHostVersion)
    if (order < 0) {
      warnings.push(`宿主版本过低：${hostVersion} < ${minHostVersion}（继续运行，可能遇到 API 差异）`)
    } else {
      hostVersionSatisfied = true
    }
  } else if (strict) {
    throw new ContractMismatchError('探测不到宿主版本，且 strictHostVersion=true', {
      hint: '设置环境变量 DSH_HOST_VERSION，或把 strictHostVersion 关掉（默认关）',
      details: { minHostVersion },
    })
  }

  // ④ schema 指纹：**这里是唯一实现**。删掉它判据必红——因为结果里 committed 字段不再为真。
  const expected = readSpecJson<SchemaFingerprint>(path.join(specDir, 'schema.fingerprint.json'))
  if (expected.algorithm !== 'sha256' || typeof expected.fingerprint !== 'string') {
    throw new SpecError('SPEC_INVALID', 'schema.fingerprint.json 结构不合法（algorithm/fingerprint）', {
      hint: '跑 `node scripts/gen-spec.mjs` 重新生成',
      details: { algorithm: expected.algorithm },
    })
  }
  const db = openReadOnly(options.dbPath)
  let actual: SchemaFingerprint
  let objectCounts: Record<string, number>
  try {
    actual = computeSchemaFingerprint(db)
    objectCounts = { ...actual.counts }
  } finally {
    db.close()
  }
  const delta = diffFingerprints(expected, actual)
  if (!delta.fingerprintMatched) {
    warnings.push(
      `schema 指纹不匹配（missing ${delta.missing.length} / unexpected ${delta.unexpected.length} / changed ${delta.changed.length}）——继续运行，只读字段可能失效`,
    )
  }
  // 业务表口径：21 张 = 全部表 − FTS 索引本体(memory_fts) − 影子表(memory_fts_*) − sqlite_sequence。
  const businessTableCount = expected.objects.filter(
    (object) =>
      object.type === 'table' &&
      object.name !== 'memory_fts' &&
      !object.name.startsWith('memory_fts_') &&
      object.name !== 'sqlite_sequence',
  ).length

  return {
    ok: true,
    contractVersion: contract.contractVersion,
    supportedContractVersion: SUPPORTED_CONTRACT_VERSION,
    minHostVersion,
    hostVersion,
    hostVersionSatisfied,
    fingerprintVerified: delta.fingerprintMatched && delta.businessMatched,
    fingerprint: actual.fingerprint,
    businessFingerprint: actual.businessFingerprint,
    specFingerprint: expected.fingerprint,
    objectCounts,
    businessTableCount,
    specDigestsVerified: digests.verified,
    specDigestCount: digests.count,
    warnings,
  }
}
