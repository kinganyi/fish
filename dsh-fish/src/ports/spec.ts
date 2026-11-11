/**
 * spec 加载器：**契约层是唯一真源，代码只是它的读者**。
 *
 * 为什么加载要带校验而不是 `JSON.parse` 了事：spec 是人工也会碰的文件，缺字段的
 * spec 会让下游拿到 `undefined` 并一路走成"空结果"。所以这里对每个文件做结构校验，
 * 失败即抛 `SpecError`（带"跑生成脚本"的下一步）。
 *
 * @module dsh-fish/ports/spec
 */

import path from 'node:path'

import { fileSha256, readSpecJson, type ContractFile } from '../core/contract.ts'
import { SpecError } from '../core/errors.ts'
import type { SchemaFingerprint } from '../core/fingerprint.ts'
import { CLI_IMPLS, CLI_KINDS, type CliMapFile } from '../domain/cli.ts'
import type { CutSpec } from '../layout/cut.ts'

/** 一个事件类型（来自 `p0/fish.py` 的 `append_event` 调用点）。 */
export interface EventTypeSpec {
  readonly type: string
  /** 必填 payload 键（调用点里出现且无条件写出的字段）。 */
  readonly requiredPayload: readonly string[]
  /** 全部 payload 键（含可选）。 */
  readonly payloadKeys: readonly string[]
  /** 来源行号（`fish.py:1234`）——让人能一眼回到代码。 */
  readonly sourceLine: number
}

/** `spec/events.json`。 */
export interface EventsSpec {
  readonly specVersion: string
  readonly source: Readonly<Record<string, unknown>>
  readonly declaredTypeCount: number
  readonly types: readonly EventTypeSpec[]
}

/**
 * `writeClass` 的**取值域**（唯一真源）。
 *
 * 为什么留一个运行时数组、而不是只写一个联合类型：生成器要给每条标注做校验、
 * `scripts/check-spec.mjs` 要给产物做校验——两处都读这一份。各写一份必然漂开，
 * 而漂开的表现是"spec 上写着 `readwrite`，下游却查不到这个类别"。
 *
 * 语义：`none` 只读 / `audit` 只追加审计日志 / `state` 改状态 / `append` 只追加不覆盖 /
 * `transaction` 多写必须在一个事务里。影子期 TS 只允许 `none` 与 `audit`（且必须走 Python）。
 */
export const WRITE_CLASSES = ['none', 'audit', 'state', 'append', 'transaction'] as const

/** 写入类别。 */
export type WriteClass = (typeof WRITE_CLASSES)[number]

/** `idempotent` 的取值域（同上：生成器与验收判据共用一份）。 */
export const IDEMPOTENT_VALUES = ['yes', 'no', 'mode-dependent'] as const

/** 幂等性：同一个入参再来一次，库里的状态会不会变。 */
export type Idempotent = (typeof IDEMPOTENT_VALUES)[number]

/** 一个工具的契约（来自 `p0/mcp_server.py`）。 */
export interface ToolSpec {
  readonly name: string
  readonly title: string
  readonly description: string
  readonly inputSchema: Readonly<Record<string, unknown>>
  readonly writeClass: WriteClass
  readonly idempotent: Idempotent
  /** 实现归属：影子期恒为 `python`。 */
  readonly impl: 'python'
  /** Python 侧实现符号（排障用）。 */
  readonly pythonSymbol: string
}

/** `spec/tools.json`。 */
export interface ToolsSpec {
  readonly specVersion: string
  readonly source: Readonly<Record<string, unknown>>
  readonly declaredToolCount: number
  readonly tools: readonly ToolSpec[]
}

/** 注入包的一段。`l0` 永远在、不可关；其余 8 段可被 `sections` 选择。 */
export interface InjectionSectionSpec {
  readonly name: string
  readonly kind: 'l0' | 'section'
  /** 装配顺序（L0 = 0，其余从 1 起，按 body 拼接顺序）。 */
  readonly order: number
  /** 命中哪一类来源（SQL/聚合函数名），供 T02 逐段换心。 */
  readonly source: string
  /** 该段自己的预算键（可为空数组 = 吃全局预算）。 */
  readonly budgetKeys: readonly string[]
  /** 裁剪优先级：数字越大越先被丢（`1` = 最后被丢）。 */
  readonly trimRank: number
}

/** `spec/injection.json`。 */
export interface InjectionSpec {
  readonly specVersion: string
  readonly source: Readonly<Record<string, unknown>>
  /** 全局预算键与默认值。 */
  readonly budget: Readonly<Record<string, number>>
  /** 包尾两行的构成（截断声明 + as-of）。 */
  readonly tail: Readonly<Record<string, unknown>>
  readonly sections: readonly InjectionSectionSpec[]
  /** 裁剪顺序（文本形式，按实际发生顺序）。 */
  readonly trimOrder: readonly string[]
}

/** 一次加载拿到的全部契约数据。 */
export interface SpecBundle {
  readonly specDir: string
  readonly contract: ContractFile
  readonly schemaFingerprint: SchemaFingerprint
  readonly events: EventsSpec
  readonly tools: ToolsSpec
  readonly injection: InjectionSpec
  readonly cut: CutSpec
  /**
   * 命令等价表（T04）。**产物来自生成器**（枚举 × 人工裁决交叉校验），
   * 这里的用途是让 `/fish shadow spec` 能看到它、让自摘要覆盖它。
   */
  readonly cliMap: CliMapFile
  /** 每个文件的实际摘要（用于回显；门禁比对的是 contract.json 里的登记值）。 */
  readonly fileDigests: Readonly<Record<string, string>>
}

/** 必需字段校验的小工具：失败抛 `SpecError` 并指出哪个文件哪个字段。 */
function requireField(condition: boolean, file: string, field: string): void {
  if (!condition) {
    throw new SpecError('SPEC_INVALID', `${file} 缺少或非法字段：${field}`, {
      hint: '跑 `node scripts/gen-spec.mjs` 重新生成 spec/（不要手工补字段）',
    })
  }
}

/** 校验 events.json。 */
function validateEvents(raw: EventsSpec, file: string): EventsSpec {
  requireField(typeof raw.specVersion === 'string', file, 'specVersion')
  requireField(Array.isArray(raw.types), file, 'types[]')
  requireField(typeof raw.declaredTypeCount === 'number', file, 'declaredTypeCount')
  if (raw.types.length !== raw.declaredTypeCount) {
    throw new SpecError('SPEC_INVALID', `${file}: declaredTypeCount=${raw.declaredTypeCount} 与实际 ${raw.types.length} 条不符`, {
      hint: '重新运行生成脚本；手改条数是最容易被漏掉的半更新',
    })
  }
  const seen = new Set<string>()
  for (const type of raw.types) {
    requireField(typeof type.type === 'string' && type.type !== '', file, 'types[].type')
    requireField(Array.isArray(type.requiredPayload), file, `types[${type.type}].requiredPayload`)
    requireField(Array.isArray(type.payloadKeys), file, `types[${type.type}].payloadKeys`)
    if (seen.has(type.type)) {
      throw new SpecError('SPEC_INVALID', `${file}: 事件类型重复 ${type.type}`)
    }
    seen.add(type.type)
  }
  return raw
}

/** 校验 tools.json。 */
function validateTools(raw: ToolsSpec, file: string): ToolsSpec {
  requireField(typeof raw.specVersion === 'string', file, 'specVersion')
  requireField(Array.isArray(raw.tools), file, 'tools[]')
  if (raw.tools.length !== raw.declaredToolCount) {
    throw new SpecError('SPEC_INVALID', `${file}: declaredToolCount=${raw.declaredToolCount} 与实际 ${raw.tools.length} 条不符`)
  }
  const seen = new Set<string>()
  for (const tool of raw.tools) {
    requireField(typeof tool.name === 'string' && tool.name !== '', file, 'tools[].name')
    requireField(typeof tool.description === 'string', file, `tools[${tool.name}].description`)
    requireField(tool.impl === 'python', file, `tools[${tool.name}].impl（影子期必须是 python）`)
    if (seen.has(tool.name)) throw new SpecError('SPEC_INVALID', `${file}: 工具名重复 ${tool.name}`)
    seen.add(tool.name)
  }
  return raw
}

/** 校验 injection.json：L0 必须存在且不可关，段名必须唯一。 */
function validateInjection(raw: InjectionSpec, file: string): InjectionSpec {
  requireField(typeof raw.specVersion === 'string', file, 'specVersion')
  requireField(Array.isArray(raw.sections), file, 'sections[]')
  const l0 = raw.sections.filter((section) => section.kind === 'l0')
  if (l0.length !== 1) {
    throw new SpecError('SPEC_INVALID', `${file}: 必须恰好有一个 l0 段（实得 ${l0.length}）`, {
      hint: 'L0 身份段是注入包的立身之本，不可关闭、不可重复',
    })
  }
  const names = new Set<string>()
  for (const section of raw.sections) {
    requireField(typeof section.name === 'string' && section.name !== '', file, 'sections[].name')
    requireField(Number.isInteger(section.order), file, `sections[${section.name}].order`)
    if (names.has(section.name)) throw new SpecError('SPEC_INVALID', `${file}: 段名重复 ${section.name}`)
    names.add(section.name)
  }
  return raw
}

/** 校验 cut.json：规则必须与 Python 现状同档（window=2/step=1/latin-then-cjk）。 */
function validateCut(raw: CutSpec, file: string): CutSpec {
  requireField(typeof raw.specVersion === 'string', file, 'specVersion')
  requireField(Array.isArray(raw.vectors), file, 'vectors[]')
  if (raw.vectors.length < 20) {
    throw new SpecError('SPEC_INVALID', `${file}: 向量少于 20 组（实得 ${raw.vectors.length}）`, {
      hint: '向量是切词对拍的唯一判据，少于 20 组等于没有覆盖',
    })
  }
  const rules = raw.rules
  requireField(typeof rules?.latin === 'string', file, 'rules.latin')
  requireField(typeof rules?.cjk === 'string', file, 'rules.cjk')
  if (rules.cjkRun?.mode !== 'overlapping-bigram' || rules.cjkRun.window !== 2 || rules.cjkRun.step !== 1) {
    throw new SpecError(
      'SPEC_INVALID',
      `${file}: cjkRun 必须是 overlapping-bigram / window=2 / step=1（Python 现状），收到 ${JSON.stringify(rules.cjkRun)}`,
      { hint: '改这一项等于改检索行为；要改先改 Python 侧并重跑切词对拍' },
    )
  }
  if (rules.tokenOrder !== 'latin-then-cjk') {
    throw new SpecError('SPEC_INVALID', `${file}: tokenOrder 必须是 latin-then-cjk（Python 的两趟扫描顺序）`)
  }
  return raw
}

/**
 * 校验 `cli-map.json`（T04）：**覆盖率与取值域都是机器可查的**。
 *
 * 为什么这里再校验一遍（生成器已经校验过）：spec 是人工也会碰的文件，
 * 少一条命令的 spec 不会报错、只会让"命令等价"悄悄漏掉一条——那正是 A3 判据要防的事。
 * 所以加载时把"条数自洽 + 取值域 + 替代解析"三条重新验一次，失败即抛。
 */
function validateCliMap(raw: CliMapFile, file: string): CliMapFile {
  requireField(typeof raw.specVersion === 'string', file, 'specVersion')
  requireField(Array.isArray(raw.entries), file, 'entries[]')
  requireField(Array.isArray(raw.groups), file, 'groups[]')
  requireField(typeof raw.counts === 'object' && raw.counts !== null, file, 'counts')
  const declared = raw.counts.leaves
  requireField(Number.isInteger(declared), file, 'counts.leaves')
  if (raw.entries.length !== declared) {
    throw new SpecError('SPEC_INVALID', `${file}: counts.leaves=${declared} 与实际 ${raw.entries.length} 条不符`, {
      hint: '跑 `node scripts/gen-spec.mjs` 重新生成（手改条数是最容易被漏掉的半更新）',
    })
  }
  const paths = new Set<string>()
  for (const entry of raw.entries) {
    requireField(typeof entry.path === 'string' && entry.path !== '', file, 'entries[].path')
    requireField(Number.isInteger(entry.level), file, `entries[${entry.path}].level`)
    requireField(typeof entry.reason === 'string' && entry.reason.trim() !== '', file, `entries[${entry.path}].reason`)
    if (!CLI_KIND_VALUES.includes(entry.kind)) {
      throw new SpecError('SPEC_INVALID', `${file}: ${entry.path} 的 kind=${String(entry.kind)} 不在取值域 ${CLI_KIND_VALUES.join('/')}`)
    }
    if (!CLI_IMPL_VALUES.includes(entry.impl)) {
      throw new SpecError('SPEC_INVALID', `${file}: ${entry.path} 的 impl=${String(entry.impl)} 不在取值域 ${CLI_IMPL_VALUES.join('/')}`)
    }
    if (entry.impl !== 'ts' && entry.replacement === null) {
      throw new SpecError('SPEC_INVALID', `${file}: ${entry.path} 是 ${entry.impl} 但没有替代命令（未实现项不许是终点）`)
    }
    if (entry.impl === 'proxy-readonly' && (entry.kind !== 'read' || entry.evidence === null)) {
      throw new SpecError(
        'SPEC_INVALID',
        `${file}: ${entry.path} 标成 proxy-readonly，但 kind=${entry.kind} / evidence=${entry.evidence === null ? '缺' : '有'}`,
        { hint: 'proxy 只允许只读命令，且必须写清只读依据（源码位置）' },
      )
    }
    if (paths.has(entry.path)) throw new SpecError('SPEC_INVALID', `${file}: 命令路径重复 ${entry.path}`)
    paths.add(entry.path)
  }
  return raw
}

/**
 * CLI 裁决的两个取值域（index.ts 与判据脚本共用）。
 *
 * 为什么从 `domain/cli.ts` 再导出一次而不是各写一份：spec 校验与命令面必须认同一份域，
 * 否则"spec 上写着 proxy-readonly、命令面不认识这个档"会在运行时才炸。
 */
export const CLI_KIND_VALUES = CLI_KINDS as readonly string[]
export const CLI_IMPL_VALUES = CLI_IMPLS as readonly string[]

/** spec 内所有可摘要文件（与生成脚本写进 contract.json 的清单一致）。 */
export const SPEC_FILES = [
  'contract.json',
  'schema.sql',
  'schema.fingerprint.json',
  'events.json',
  'tools.json',
  'injection.json',
  'layout/cut.json',
  'cli-map.json',
  // T05：语料库是独立库，它的 DDL 也是契约（两侧建库读同一份）。
  'corpus.schema.sql',
] as const

/**
 * 加载并校验整份 spec。
 * @throws SpecError 文件缺失 / JSON 非法 / 结构不合法
 */
export function loadSpec(specDir: string): SpecBundle {
  const dir = path.resolve(specDir)
  const contract = readSpecJson<ContractFile>(path.join(dir, 'contract.json'))
  const schemaFingerprint = readSpecJson<SchemaFingerprint>(path.join(dir, 'schema.fingerprint.json'))
  const events = validateEvents(readSpecJson<EventsSpec>(path.join(dir, 'events.json')), 'events.json')
  const tools = validateTools(readSpecJson<ToolsSpec>(path.join(dir, 'tools.json')), 'tools.json')
  const injection = validateInjection(readSpecJson<InjectionSpec>(path.join(dir, 'injection.json')), 'injection.json')
  const cut = validateCut(readSpecJson<CutSpec>(path.join(dir, 'layout', 'cut.json')), 'layout/cut.json')
  const cliMap = validateCliMap(readSpecJson<CliMapFile>(path.join(dir, 'cli-map.json')), 'cli-map.json')

  const fileDigests: Record<string, string> = {}
  for (const relative of SPEC_FILES) {
    try {
      fileDigests[relative] = fileSha256(path.join(dir, relative))
    } catch {
      // contract.json 自己的摘要由生成脚本登记，这里跳过缺文件（缺文件会在门禁里报）。
      continue
    }
  }

  return { specDir: dir, contract, schemaFingerprint, events, tools, injection, cut, cliMap, fileDigests }
}
