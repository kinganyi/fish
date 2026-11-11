/**
 * `/fish` 斜杠命令的**总入口**（影子期：命令面是唯一等价面）。
 *
 * 三个面共用同一个命令名，各管一段：
 *   · `shadow status|contract|spec|probe|presence|recall|whoami|health` —— T01–T03 的契约与只读能力面
 *   · `hosts probe|health|dry-run` —— T04 的跨宿主管理面（只读三件，**不做 apply**）
 *   · `<一级命令> [二级...] [--k=v...]` —— T04 的 **76 条 fish CLI 叶子**（四档归属见 `cli.ts`）
 *
 * 为什么影子期必须有命令面：插件不能给 `dsh` 二进制加子命令（宿主硬边界），而影子期
 * 又不能注册工具、不能注入上下文，于是"能被人看见"的只剩命令。它承担三件事：
 * 证明插件真的装上了、把契约门禁的读数摊开、给只读能力一个**人手可复现**的入口。
 *
 * 边界（写进回显、不许沉默）：
 *   · 不注册模型可见工具；· 不向模型上下文注入任何内容；· 不写 fish 库；· 不改宿主配置。
 *
 * @module dsh-fish/surfaces/commands
 */

import type { Context } from '@deepseek-ai/cordis'
import type { CommandResult } from '@deepseek-ai/dsh-commands'

import type { StartupGateResult } from '../core/contract.ts'
import type { FishLogger } from '../core/logger.ts'
import type { FishPaths } from '../core/paths.ts'
import { businessTableNames, objectCounts, openReadOnly } from '../core/db.ts'
import { toFishError } from '../core/errors.ts'
import { NATIVE_COMMANDS } from '../domain/cli.ts'
import { healthcheckReadings, renderHealthcheck } from '../domain/health.ts'
import { renderWhoami, resolveIdentity } from '../domain/identity.ts'
import { renderRecall } from '../domain/memory.ts'
import { readPresencePanel, renderPresencePanel } from '../domain/presence.ts'
import { verifyCutVectors } from '../layout/cut.ts'
import type { SpecBundle } from '../ports/spec.ts'
import { handleCliInput } from './cli.ts'
import { handleHostsCommand, HOSTS_USAGE } from './hosts.ts'
import { finalizeOutput, OUTPUT_LINE_LIMIT } from './output.ts'

/** 截断器的转出（`index.ts` 与验收脚本都从这里取同一份实现）。 */
export { finalizeOutput }

/** 命令名（不带斜杠）。 */
export const COMMAND_NAME = 'fish'

/** 命令依赖：全部是**已经算好的只读事实**，命令本身不再碰网络/写库。 */
export interface CommandDeps {
  readonly paths: FishPaths
  readonly spec: SpecBundle
  readonly gate: StartupGateResult
  readonly logger: FishLogger
  readonly config: Readonly<{ mode: string; budgetTokens: number; commandEnabled: boolean }>
}

const USAGE = `用法：/fish <子命令>
  shadow status     影子期状态（契约门禁读数 / 只读自证 / 边界声明）
  shadow contract   契约摘要（版本 / 宿主最低版本 / 能力块归属）
  shadow spec       spec 文件清单与摘要
  shadow probe      只读不变量自检（切词向量 + 表计数）
  shadow presence   面板数据源（谁在场 + 最近几条动态；两条只读 SELECT）
  shadow recall <q> 检索记忆（query/code/project 至少给一个；无关键词时按 project 列出）
                   选项：--project P  --code C  --tags a,b  --limit N  --scope all|title|body
                         --include-superseded
  shadow whoami     身份回执（token / session_key；证据级别只降不升）
                   选项：--token T  --session-key K  --all
  shadow health     自检读数（事件 / 活跃 / 休眠 / 作废 / 断链 / FTS 落后 / owner 无效）
  hosts probe       跨宿主 hooks 只读探针（配置文件 / fish 条目 / matcher / 脚本路径）
  hosts health      跨宿主 hooks 心跳健康度（每宿主 × 4 事件，>2h 视为陈旧）
  hosts dry-run     安装预览：目标文件"会变成什么"的统一 diff（只输出，不写盘）
  <fish 命令>       76 条 fish CLI 叶子全部映射到这里，例如：
                    /fish healthcheck   /fish recall 协作约定   /fish list
                    /fish board checkin …（写命令：明确拒绝 + 给替代）
                    /fish kb vault list  /fish events --limit 20
                    输入 /fish <命令> --help 看单条；输入 /fish <容器> 看子命令

影子期纪律（三条同时生效）：不注册模型工具 / 不注入上下文 / 不写 fish 库、不改宿主配置。`

/** 影子期纪律：这三条永远为真，回显时必须出现（不许靠记忆）。 */
const SHADOW_DISCIPLINE = [
  '不注册模型可见工具（与 mcp__fish__* 同层会抛错）',
  '不注入模型上下文（hooks.json 4 条仍在注入，避免双份）',
  '不写 fish 库（只读 mode=ro + PRAGMA query_only）',
] as const

/** 命令输出超过这个行数就落盘（实现与常量都在 `output.ts`，这里只做转出）。 */
export { OUTPUT_LINE_LIMIT }

/** 解析后的命令参数（只认 `--flag value` / `--flag=v` / `--flag` 三种形态）。 */
export interface ParsedCommandInput {
  readonly positional: readonly string[]
  readonly flags: Readonly<Record<string, string | true>>
}

/**
 * 解析子命令之后的参数。
 *
 * 三种形态都认（T04 起必须认 `--k=v`：CLI 等价的透传口径是"原样交给 python"，
 * 而 `fish.py` 的开关都是 `--key value` / `--key`；宿主里人更常写 `--k=v`）：
 *   · `--flag value` · `--flag=value` · `--flag`
 *
 * 为什么手写而不是引一个解析库：命令面只有十来个开关，而库会把"未知开关"变成
 * 静默忽略或一大段 usage——这里宁可对未知开关**响亮报错**（拼错的开关等于什么都没做）。
 */
export function parseCommandInput(input: string): ParsedCommandInput {
  const parts = input.split(/\s+/).filter((part) => part !== '')
  const positional: string[] = []
  const flags: Record<string, string | true> = {}
  for (let i = 0; i < parts.length; i += 1) {
    const part = parts[i] as string
    if (part.startsWith('--')) {
      const body = part.slice(2)
      if (body === '') throw new Error('空的开关名：`--`')
      const eq = body.indexOf('=')
      if (eq >= 0) {
        // `--k=v` 形态：值里可能还有 `=`（如 `--note=a=b`），只切第一个。
        const name = body.slice(0, eq)
        if (name === '') throw new Error(`开关名为空：${JSON.stringify(part)}`)
        flags[name] = body.slice(eq + 1)
        continue
      }
      const next = parts[i + 1]
      if (next !== undefined && !next.startsWith('--')) {
        flags[body] = next
        i += 1
      } else {
        flags[body] = true
      }
      continue
    }
    positional.push(part)
  }
  return { positional, flags }
}

/** 取字符串开关；`true`（只写了开关名）按空串处理，缺省返回 `undefined`。 */
function flagString(flags: ParsedCommandInput['flags'], name: string): string | undefined {
  const value = flags[name]
  if (value === undefined) return undefined
  return value === true ? '' : value
}

/** 取整数开关（非法值响亮失败：`--limit 十个` 不许静默变成默认值）。 */
function flagInt(flags: ParsedCommandInput['flags'], name: string): number | undefined {
  const raw = flagString(flags, name)
  if (raw === undefined) return undefined
  if (raw === '') throw new Error(`--${name} 需要一个整数`)
  const parsed = Number.parseInt(raw, 10)
  if (Number.isNaN(parsed)) throw new Error(`--${name} 需要整数，收到 ${JSON.stringify(raw)}`)
  return parsed
}

/** 逗号/空白分隔的列表开关。 */
function flagList(flags: ParsedCommandInput['flags'], name: string): string[] | undefined {
  const raw = flagString(flags, name)
  if (raw === undefined) return undefined
  const items = raw.split(/[,\s]+/).filter((item) => item !== '')
  return items.length > 0 ? items : undefined
}

/** 把门禁读数渲染成短表（Markdown，命令结果支持文本）。 */
function renderGate(gate: StartupGateResult): string {
  return [
    `契约版本：${gate.contractVersion}（插件支持 ${gate.supportedContractVersion}）`,
    `宿主版本：${gate.hostVersion ?? '未探测'}（最低 ${gate.minHostVersion}，${gate.hostVersionSatisfied ? '已满足' : '未校验'}）`,
    `schema 指纹：${gate.fingerprintVerified ? '匹配' : '不匹配'} ${gate.fingerprint.slice(0, 16)}…（业务 ${gate.businessFingerprint.slice(0, 12)}…）`,
    `spec 自摘要：${gate.specDigestsVerified ? `通过（${gate.specDigestCount} 个文件）` : '未通过'}`,
    `对象计数：${Object.entries(gate.objectCounts)
      .map(([type, n]) => `${type}=${n}`)
      .join(' ')}｜业务表 ${gate.businessTableCount} 张`,
  ].join('\n')
}

/** `shadow status` 文本。 */
export function renderShadowStatus(deps: CommandDeps): string {
  const db = openReadOnly(deps.paths.dbPath)
  let counts: Record<string, number>
  try {
    counts = objectCounts(db)
  } finally {
    db.close()
  }
  return [
    `dsh-fish · 模式 ${deps.config.mode}`,
    `库：${deps.paths.dbPath}（只读）`,
    `spec：${deps.paths.specDir}`,
    renderGate(deps.gate),
    `实时对象计数：${Object.entries(counts)
      .map(([type, n]) => `${type}=${n}`)
      .join(' ')}`,
    '',
    '影子期纪律（三条同时生效）：',
    ...SHADOW_DISCIPLINE.map((line, i) => `  ${i + 1}. ${line}`),
  ].join('\n')
}

/** `shadow contract` 文本。 */
export function renderContract(deps: CommandDeps): string {
  const { contract } = deps.spec
  const byImpl = { python: 0, shadow: 0, ts: 0 }
  for (const entry of contract.implMatrix) byImpl[entry.impl] += 1
  return [
    `contractVersion：${contract.contractVersion}`,
    `minHostVersion：${contract.minHostVersion}`,
    `能力块：${contract.capabilities.length} 个｜impl python=${byImpl.python} shadow=${byImpl.shadow} ts=${byImpl.ts}`,
    `spec 登记文件：${Object.keys(contract.specFiles).length} 个`,
    ...contract.implMatrix.map((entry) => `  ${entry.id} ${entry.name} → ${entry.impl}`),
  ].join('\n')
}

/** `shadow spec` 文本。 */
export function renderSpecInventory(deps: CommandDeps): string {
  const lines = [`spec 目录：${deps.paths.specDir}`]
  const digests = deps.spec.fileDigests
  for (const [relative, digest] of Object.entries(digests).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))) {
    lines.push(`  ${relative}  ${digest.slice(0, 16)}…`)
  }
  const registered = Object.keys(deps.spec.contract.specFiles).sort()
  const actual = Object.keys(digests).sort()
  const missing = registered.filter((name) => !actual.includes(name))
  const extra = actual.filter((name) => !registered.includes(name))
  lines.push(
    `摘要登记：登记 ${registered.length} / 实际 ${actual.length}` +
      (missing.length === 0 && extra.length === 0 ? '（一致）' : `（登记缺失 ${missing.length}，未登记 ${extra.length}）`),
  )
  return lines.join('\n')
}

/** `shadow probe` 文本：只读不变量自检。 */
export function renderProbe(deps: CommandDeps): string {
  const failures = verifyCutVectors(deps.spec.cut)
  const db = openReadOnly(deps.paths.dbPath)
  let counts: Record<string, number>
  let businessTables: string[] = []
  let writeRejected = false
  try {
    counts = objectCounts(db)
    businessTables = businessTableNames(db)
    try {
      db.exec('CREATE TABLE __dsh_fish_command_probe (x INTEGER)')
    } catch {
      writeRejected = true
    }
  } finally {
    db.close()
  }
  return [
    `切词向量：${deps.spec.cut.vectors.length} 组，失败 ${failures.length} 组${failures.length === 0 ? '（全部通过）' : ''}`,
    ...failures.slice(0, 5).map((f) => `  ✗ ${f.id} 首个差异位置 ${f.firstDiffAt}`),
    `只读自证：${writeRejected ? '写入被 SQLite 拒绝（正常）' : '⚠ 写入竟然成功——立刻停用插件'}`,
    `业务表：${businessTables.length} 张（期望 21）`,
    `对象计数：${Object.entries(counts)
      .map(([type, n]) => `${type}=${n}`)
      .join(' ')}`,
  ].join('\n')
}

/**
 * `shadow presence` 文本：面板数据源的人读形态。
 *
 * 为什么命令面要保留这一条：`board.ts` 被砍之后，面板需要的"谁在场 + 最近几条动态"
 * 只剩 `domain/presence.ts` 这一处来源。给一个人手可复现的入口，等于让这份数据源
 * 每天都被看见一次；没有入口的数据源，坏掉时只会安静地坏。
 */
export function renderShadowPresence(deps: CommandDeps): string {
  return renderPresencePanel(readPresencePanel(deps.paths.dbPath))
}

/** `shadow recall` 文本。 */
export function renderShadowRecall(
  positional: readonly string[],
  flags: ParsedCommandInput['flags'],
  deps: CommandDeps,
): string {
  const query = positional.join(' ')
  const token = flagString(flags, 'token') ?? (process.env.FISH_SESSION_TOKEN ?? '').trim()
  const db = openReadOnly(deps.paths.dbPath)
  try {
    const identity = resolveIdentity(db, token)
    return renderRecall(
      db,
      {
        query,
        code: flagString(flags, 'code') ?? null,
        project: flagString(flags, 'project') ?? null,
        tags: flagList(flags, 'tags') ?? null,
        scope: flagString(flags, 'scope') ?? 'all',
        limit: flagInt(flags, 'limit') ?? 10,
        includeSuperseded: flags['include-superseded'] === true,
      },
      identity,
    )
  } finally {
    db.close()
  }
}

/** `shadow whoami` 文本。 */
export function renderShadowWhoami(flags: ParsedCommandInput['flags'], deps: CommandDeps): string {
  const token = flagString(flags, 'token') ?? (process.env.FISH_SESSION_TOKEN ?? '').trim()
  const sessionKey = flagString(flags, 'session-key') ?? ''
  const db = openReadOnly(deps.paths.dbPath)
  try {
    const { text } = renderWhoami(db, { token, sessionKey, all: flags['all'] === true }, deps.paths.fishHome)
    return text
  } finally {
    db.close()
  }
}

/** `shadow health` 文本。 */
export function renderShadowHealth(deps: CommandDeps): string {
  const db = openReadOnly(deps.paths.dbPath)
  try {
    return renderHealthcheck(healthcheckReadings(db, deps.paths.dataDir))
  } finally {
    db.close()
  }
}

/**
 * 命令总入口（纯函数：便于单测）。
 *
 * 路由顺序：`shadow`（自有契约面）→ `hosts`（自有管理面）→ 其余一律当 **fish CLI**
 * （76 条叶子）。为什么自有命令要单独判：它们不来自 `fish.py`，若混进 cli-map，
 * 覆盖率判据就会在比"Python 命令树 == 映射表"时多出两条永远对不上的条目。
 *
 * @param rawInput 命令名之后的原始输入（宿主保证含前导分隔空白）
 */
export function handleFishCommand(rawInput: string, deps: CommandDeps): CommandResult {
  const normalized = rawInput.trim().replace(/\s+/g, ' ')
  try {
    const { positional, flags } = parseCommandInput(normalized)
    const first = positional[0] ?? ''
    if (normalized === '' || first === 'help' || (positional.length === 0 && flags.help === true)) {
      return { kind: 'success', text: USAGE }
    }
    if (first === 'shadow') {
      return handleShadowCommand(positional, flags, deps)
    }
    if (first === 'hosts') {
      return handleHostsCommand(positional.slice(1), flags, deps)
    }
    return handleCliInput(positional, flags, deps)
  } catch (error) {
    const fishError = toFishError(error)
    deps.logger.error('命令执行失败', { input: normalized, code: fishError.code })
    return { kind: 'error', text: fishError.format() }
  }
}

/** `shadow ...` 这一族（T01–T03 的契约与只读能力面）。 */
function handleShadowCommand(positional: readonly string[], flags: ParsedCommandInput['flags'], deps: CommandDeps): CommandResult {
  const head = positional.slice(0, 2).join(' ')
  if (flags.help === true || positional.length === 1) {
    return { kind: 'success', text: USAGE }
  }
  switch (head) {
    case 'shadow status':
      return { kind: 'success', text: renderShadowStatus(deps) }
    case 'shadow contract':
      return { kind: 'success', text: renderContract(deps) }
    case 'shadow spec':
      return { kind: 'success', text: renderSpecInventory(deps) }
    case 'shadow probe':
      return { kind: 'success', text: renderProbe(deps) }
    case 'shadow health':
      return finalizeResult(renderShadowHealth(deps), 'health', deps)
    case 'shadow presence':
      return finalizeResult(renderShadowPresence(deps), 'presence', deps)
    case 'shadow whoami':
      return finalizeResult(renderShadowWhoami(flags, deps), 'whoami', deps)
    case 'shadow recall': {
      const rest = positional.slice(2)
      return finalizeResult(renderShadowRecall(rest, flags, deps), 'recall', deps)
    }
    default:
      return {
        kind: 'error',
        text: `未知子命令：${positional.join(' ').slice(0, 80)}\n\n${USAGE}`,
      }
  }
}

/** 收敛成 `CommandResult`（截断逻辑只在这里接一次）。 */
function finalizeResult(text: string, label: string, deps: CommandDeps): CommandResult {
  const { text: finalText } = finalizeOutput(text, label, deps.paths.packageRoot)
  return { kind: 'success', text: finalText }
}

/**
 * 注册 `/fish`（全局，不绑 agent）。
 * @returns 注销函数（宿主 dispose 时调用）
 */
export function registerFishCommand(ctx: Context, deps: CommandDeps): () => void {
  if (!deps.config.commandEnabled) {
    deps.logger.warn('已通过配置关闭 /fish 命令（commandEnabled=false）')
    return () => undefined
  }
  return ctx.commands.register({
    name: COMMAND_NAME,
    description:
      'fish（大肥鱼生命系统）影子期只读命令：shadow 契约面 / hosts 跨宿主管理面（probe|health|dry-run）/' +
      ' 76 条 fish CLI 命令（写命令明确拒绝并给替代）',
    input: {
      hint: 'shadow status | hosts probe | healthcheck | recall <q> | board checkin …（76 条 CLI 命令）',
    },
    handler: (invocation) => handleFishCommand(invocation.rawInput, deps),
  })
}

/** 自有命令名（不含 `fish.py` 的 76 条），判据脚本用它算"命令面全集"。 */
export { NATIVE_COMMANDS, HOSTS_USAGE }
