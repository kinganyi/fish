/**
 * `/fish <一级> [二级...] [--k=v...]`：**`fish.py` 命令面在 DSH 里的等价实现**。
 *
 * 一级命令名就是宿主里注册的那一个 `/fish` 的子命令（35 个），二级起进 `rawInput`
 * 原样透传（`ctx.commands` 只给 `rawInput` + `attachments`，没有参数 schema）。
 *
 * 四档归属的执行点都在这里（档位由 `domain/cli.ts` 的裁决表决定）：
 *
 *   · `ts`                → 直接调 T01–T03 的领域层（不开 Python、不碰库的写口）
 *   · `proxy-readonly`    → 在**只读快照副本**上跑 Python（`ports/python.ts`）
 *   · `unsupported-shadow`→ 明确拒绝 + 给替代（**不静默成功**）
 *   · `python-only`       → 只给指引，不执行
 *
 * 为什么拒绝也要走命令面（而不是干脆不注册）："没实现"和"这里没有这个功能"是两件事。
 * 前者需要一条能被人读到的说明 + 一个可执行的下一步；静默不注册只会让人以为
 * 大肥鱼没有这条命令。
 *
 * @module dsh-fish/surfaces/cli
 */

import type { CommandResult } from '@deepseek-ai/dsh-commands'

import { openReadOnly } from '../core/db.ts'
import { FishError, toFishError } from '../core/errors.ts'
import {
  findCliAnnotation,
  formatReplacement,
  renderCliGuidance,
  renderCliHelpText,
  renderCliRefusal,
  resolveCliRoute,
  type CliAnnotation,
} from '../domain/cli.ts'
import {
  KbError,
  kbOpen,
  kbSearch,
  kbStats,
  kbTree,
  kbVaultGet,
  kbVaultList,
  renderKbError,
  renderKbOpen,
  renderKbSearch,
  renderKbStats,
  renderKbTree,
  renderKbVaultList,
} from '../domain/kb.ts'
import {
  corpusRead,
  corpusSearch,
  corpusStats,
  PointerError,
  READ_DEFAULT_MAX_CHARS,
  renderCorpusRead,
  renderCorpusSearch,
  renderCorpusStats,
  renderPointerError,
} from '../domain/corpus.ts'
import {
  parseAsOf,
  projectStatusPayload,
  projectStatusRows,
  renderAliasList,
  renderProjectStatus,
  renderWorkspaceList,
} from '../domain/project.ts'
import { healthcheckReadings, renderHealthcheck } from '../domain/health.ts'
import { renderWhoami, resolveIdentity } from '../domain/identity.ts'
import { renderRecall } from '../domain/memory.ts'
import { runFishCliOnSnapshot } from '../ports/python.ts'
import type { CommandDeps, ParsedCommandInput } from './commands.ts'
import { finalizeOutput } from './output.ts'

/** 一条 `ts` 命令接受的开关（多一个就响亮失败：拼错的开关等于什么都没做）。 */
const TS_FLAGS: Readonly<Record<string, readonly string[]>> = {
  recall: ['project', 'code', 'tags', 'limit', 'scope', 'include-superseded', 'token'],
  whoami: ['token', 'session-key', 'all'],
  healthcheck: [],
}

/** 写类开关：连只读命令都不许带（`--apply` 一出现就说明调用方想写）。 */
const WRITE_FLAGS = ['apply', 'write', 'force', 'overwrite'] as const

/** 取字符串开关；`true`（只写了开关名）按空串处理。 */
function flagString(flags: ParsedCommandInput['flags'], name: string): string | undefined {
  const value = flags[name]
  if (value === undefined) return undefined
  return value === true ? '' : value
}

/** 取整数开关（非法值响亮失败）。 */
function flagInt(flags: ParsedCommandInput['flags'], name: string): number | undefined {
  const raw = flagString(flags, name)
  if (raw === undefined) return undefined
  if (raw === '') throw new FishError('COMMAND_USAGE', `--${name} 需要一个整数`)
  const parsed = Number.parseInt(raw, 10)
  if (Number.isNaN(parsed)) throw new FishError('COMMAND_USAGE', `--${name} 需要整数，收到 ${JSON.stringify(raw)}`)
  return parsed
}

/** 逗号/空白分隔的列表开关。 */
function flagList(flags: ParsedCommandInput['flags'], name: string): string[] | undefined {
  const raw = flagString(flags, name)
  if (raw === undefined) return undefined
  const items = raw.split(/[,\s]+/).filter((item) => item !== '')
  return items.length > 0 ? items : undefined
}

/** 把 flags 还原成命令行参数（`--k=v` / `--k`）。 */
export function flagsToArgv(flags: ParsedCommandInput['flags']): string[] {
  const argv: string[] = []
  for (const key of Object.keys(flags).sort()) {
    const value = flags[key]
    if (value === undefined) continue
    argv.push(value === true ? `--${key}` : `--${key}=${value}`)
  }
  return argv
}

/** 断言"这条只读命令没带写开关"。 */
function assertNoWriteFlags(flags: ParsedCommandInput['flags'], path: string): void {
  const hit = WRITE_FLAGS.filter((name) => flags[name] !== undefined)
  if (hit.length > 0) {
    throw new FishError(
      'SHADOW_READ_ONLY',
      `/fish ${path} 是只读命令，不接受写开关：${hit.map((name) => `--${name}`).join(' ')}`,
      { hint: '写形态请在终端里跑 `fish …`（影子期插件不写任何库/文件）' },
    )
  }
}

/** 断言"这条 ts 命令只用了它支持的开关"。 */
function assertKnownFlags(flags: ParsedCommandInput['flags'], path: string, allowed: readonly string[]): void {
  const unknown = Object.keys(flags).filter((name) => !allowed.includes(name))
  if (unknown.length > 0) {
    throw new FishError(
      'COMMAND_USAGE',
      `/fish ${path} 不认识这些开关：${unknown.map((name) => `--${name}`).join(' ')}`,
      { hint: `可用开关：${allowed.map((name) => `--${name}`).join(' / ') || '（无）'}` },
    )
  }
}

/** 一条只读命令的执行体：返回文本，或显式声明"这是一次错误回执"。 */
type ReadOnlyOutcome = string | { readonly text: string; readonly kind: 'success' | 'error' }

/** 只读能力的登记项（T05）：声明它接受哪些开关 + 执行体。 */
interface ReadOnlyHandler {
  readonly flags: readonly string[]
  readonly run: (context: {
    readonly rest: readonly string[]
    readonly flags: ParsedCommandInput['flags']
    readonly deps: CommandDeps
  }) => ReadOnlyOutcome
}

/** `--json` 未实现时的说明（不假装成功，也不假装这个开关不存在）。 */
function jsonUnsupported(path: string): FishError {
  return new FishError(
    'COMMAND_USAGE',
    `/fish ${path} 的 --json 形态影子期未实现（只做了与终端逐字节同形的文本形态）`,
    { hint: `要看 JSON 请在终端里跑 \`fish ${path} --json\`` },
  )
}

/** 读 flag 的布尔开关（`--flag` 即可，`--flag=false` 也算显式给了值）。 */
function flagBool(flags: ParsedCommandInput['flags'], name: string): boolean {
  const value = flags[name]
  if (value === undefined) return false
  if (value === true) return true
  return value.trim().toLowerCase() === 'true' || value.trim() === '1'
}

/** 必填位置参数（缺了就响亮报用法错误，不静默当空串）。 */
function requirePositional(rest: readonly string[], path: string, what: string): string {
  const value = rest.join(' ').trim()
  if (value === '') {
    throw new FishError('COMMAND_USAGE', `/fish ${path} 缺少${what}`, { hint: `用法：/fish ${path} <${what}>` })
  }
  return value
}

/**
 * T05 的只读能力表：**知识库 / 语料 / 项目坐标**三族。
 *
 * 为什么这些命令进 `ts` 而不是 `proxy-readonly`：它们的依赖不在主库快照里——
 * 知识库读的是 fish-home 里的文件树、语料读的是独立的 `corpus.sqlite`。
 * 转发只会在副本上得到"库不存在"的假空（T04 §5.2 的同一结论），所以只在 TS 侧实现。
 */
const READ_ONLY_COMMANDS: Readonly<Record<string, ReadOnlyHandler>> = {
  // ---------------------------------------------------------------- 知识库
  'kb vault list': {
    flags: [],
    run: ({ deps }) => {
      const db = openReadOnly(deps.paths.dbPath)
      try {
        return renderKbVaultList(kbVaultList(db))
      } finally {
        db.close()
      }
    },
  },
  'kb tree': {
    flags: ['vault', 'depth', 'max-chars', 'json'],
    run: ({ rest, flags, deps }) => {
      if (flagBool(flags, 'json')) throw jsonUnsupported('kb tree')
      const db = openReadOnly(deps.paths.dbPath)
      try {
        const vault = kbVaultGet(db, flagString(flags, 'vault') ?? null)
        return renderKbTree(
          kbTree(vault, {
            rel: rest.join(' ').trim(),
            depth: flagInt(flags, 'depth') ?? 2,
            maxChars: flagInt(flags, 'max-chars') ?? 8000,
          }),
        )
      } finally {
        db.close()
      }
    },
  },
  'kb open': {
    flags: ['vault', 'max-chars', 'allow-sensitive', 'json'],
    run: ({ rest, flags, deps }) => {
      if (flagBool(flags, 'json')) throw jsonUnsupported('kb open')
      const db = openReadOnly(deps.paths.dbPath)
      try {
        const vault = kbVaultGet(db, flagString(flags, 'vault') ?? null)
        return renderKbOpen(
          kbOpen(vault, requirePositional(rest, 'kb open', '文件名'), {
            maxChars: flagInt(flags, 'max-chars') ?? 64000,
            allowSensitive: flagBool(flags, 'allow-sensitive'),
          }),
        )
      } finally {
        db.close()
      }
    },
  },
  'kb search': {
    flags: ['vault', 'limit', 'json'],
    run: ({ rest, flags, deps }) => {
      if (flagBool(flags, 'json')) throw jsonUnsupported('kb search')
      const db = openReadOnly(deps.paths.dbPath)
      try {
        const vault = kbVaultGet(db, flagString(flags, 'vault') ?? null)
        return renderKbSearch(
          kbSearch(vault, requirePositional(rest, 'kb search', '关键词'), { limit: flagInt(flags, 'limit') ?? 10 }),
        )
      } finally {
        db.close()
      }
    },
  },
  'kb stats': {
    flags: ['vault', 'json'],
    run: ({ flags, deps }) => {
      if (flagBool(flags, 'json')) throw jsonUnsupported('kb stats')
      const db = openReadOnly(deps.paths.dbPath)
      try {
        return renderKbStats(kbStats(db, flagString(flags, 'vault') ?? null))
      } finally {
        db.close()
      }
    },
  },

  // ---------------------------------------------------------------- 语料
  'corpus search': {
    flags: ['limit', 'json'],
    run: ({ rest, flags, deps }) => {
      const query = requirePositional(rest, 'corpus search', '关键词')
      const result = corpusSearch(deps.paths.dataDir, query, { limit: flagInt(flags, 'limit') ?? 8 })
      if (flagBool(flags, 'json')) return JSON.stringify(result, null, 2)
      return renderCorpusSearch(result)
    },
  },
  'corpus read': {
    flags: ['max-chars'],
    run: ({ rest, flags, deps }) => {
      const pointer = requirePositional(rest, 'corpus read', '门牌')
      const maxChars = flagInt(flags, 'max-chars') ?? READ_DEFAULT_MAX_CHARS
      try {
        return renderCorpusRead(corpusRead(deps.paths.dataDir, pointer, { maxChars }), maxChars)
      } catch (error) {
        if (error instanceof PointerError) return { text: renderPointerError(error), kind: 'error' as const }
        throw error
      }
    },
  },
  'corpus stats': {
    flags: ['host'],
    run: ({ flags, deps }) => {
      const host = flagString(flags, 'host') ?? 'all'
      return renderCorpusStats(corpusStats(deps.paths.dataDir, host === 'all' ? null : host))
    },
  },

  // ---------------------------------------------------------------- 项目坐标
  'project status': {
    flags: ['as-of', 'include-retired', 'json', 'home', 'allow-external-home'],
    run: ({ flags, deps }) => {
      const asOf = parseAsOf(flagString(flags, 'as-of'))
      const includeRetired = flagBool(flags, 'include-retired')
      const db = openReadOnly(deps.paths.dbPath)
      try {
        const result = projectStatusRows(db, {
          dataRoot: deps.paths.dataDir,
          asOf,
          includeRetired,
          homeRoot: flagString(flags, 'home') ?? null,
        })
        if (flagBool(flags, 'json')) return JSON.stringify(projectStatusPayload(result), null, 2)
        return renderProjectStatus(result, { includeRetired })
      } finally {
        db.close()
      }
    },
  },
  'alias list': {
    flags: [],
    run: ({ deps }) => {
      const db = openReadOnly(deps.paths.dbPath)
      try {
        return renderAliasList(db)
      } finally {
        db.close()
      }
    },
  },
  'workspace list': {
    flags: [],
    run: ({ deps }) => {
      const db = openReadOnly(deps.paths.dbPath)
      try {
        return renderWorkspaceList(db)
      } finally {
        db.close()
      }
    },
  },
}

/**
 * 跑一条 T05 的只读命令。
 *
 * 错误分三类，各自有自己的回执形态（**不能混**）：
 *   · `KbError` → `错误[<code>]：<message>`（与终端 `fish kb …` 同形，含"下一步"）；
 *   · `PointerError` → `门牌错误（<code>）：…`（生产 `run_cli` 的同一段）；
 *   · 其它 `FishError`（开关拼错 / 缺少参数 / `--json` 未实现）→ 常规错误格式。
 */
function runReadOnlyCommand(
  annotation: CliAnnotation,
  handler: ReadOnlyHandler,
  rest: readonly string[],
  flags: ParsedCommandInput['flags'],
  deps: CommandDeps,
): CommandResult {
  assertKnownFlags(flags, annotation.path, handler.flags)
  assertNoWriteFlags(flags, annotation.path)
  try {
    const outcome = handler.run({ rest, flags, deps })
    if (typeof outcome === 'string') return { kind: 'success', text: outcome }
    return { kind: outcome.kind, text: outcome.text }
  } catch (error) {
    if (error instanceof KbError) return { kind: 'error', text: renderKbError(error) }
    if (error instanceof PointerError) return { kind: 'error', text: renderPointerError(error) }
    throw error
  }
}

/** `ts` 档的执行：全部复用 T01–T03 的领域层（同一份实现，不另写一份）。 */
function runTsCommand(annotation: CliAnnotation, rest: readonly string[], flags: ParsedCommandInput['flags'], deps: CommandDeps): CommandResult {
  const readOnly = READ_ONLY_COMMANDS[annotation.path]
  if (readOnly !== undefined) return runReadOnlyCommand(annotation, readOnly, rest, flags, deps)
  const allowed = TS_FLAGS[annotation.path] ?? []
  assertKnownFlags(flags, annotation.path, allowed)
  assertNoWriteFlags(flags, annotation.path)
  if (annotation.path === 'healthcheck') {
    const db = openReadOnly(deps.paths.dbPath)
    try {
      return {
        kind: 'success',
        text: renderHealthcheck(healthcheckReadings(db, deps.paths.dataDir)),
      }
    } finally {
      db.close()
    }
  }
  const token = flagString(flags, 'token') ?? (process.env.FISH_SESSION_TOKEN ?? '').trim()
  const db = openReadOnly(deps.paths.dbPath)
  try {
    const identity = resolveIdentity(db, token)
    if (annotation.path === 'whoami') {
      const { text } = renderWhoami(
        db,
        { token, sessionKey: flagString(flags, 'session-key') ?? '', all: flags['all'] === true },
        deps.paths.fishHome,
      )
      return { kind: 'success', text }
    }
    if (annotation.path === 'recall') {
      return {
        kind: 'success',
        text: renderRecall(
          db,
          {
            query: rest.join(' '),
            code: flagString(flags, 'code') ?? null,
            project: flagString(flags, 'project') ?? null,
            tags: flagList(flags, 'tags') ?? null,
            scope: flagString(flags, 'scope') ?? 'all',
            limit: flagInt(flags, 'limit') ?? 10,
            includeSuperseded: flags['include-superseded'] === true,
          },
          identity,
        ),
      }
    }
    // 走到这里 = 裁决表把一条 `ts` 命令交下来了，但实现层没有它的分支。
    // 第二批减重删掉了 `inject` 的 TS 复刻，它的裁决已改成 `python-only`（不会再进来）；
    // 真的漏了一条就在这里响亮失败，而不是把上一段的文本当成它返回。
    throw new FishError('SHADOW_UNSUPPORTED', `fish ${annotation.path} 的 ts 实现不在本仓（裁决表与实现层不同步）`, {
      hint: '在 src/domain/cli.ts 把这条改成 python-only 并写清理由，或补上实现',
    })
  } finally {
    db.close()
  }
}

/** `proxy-readonly` 档：只读快照上的 Python 转发。 */
function runProxyCommand(annotation: CliAnnotation, rest: readonly string[], flags: ParsedCommandInput['flags'], deps: CommandDeps): CommandResult {
  assertNoWriteFlags(flags, annotation.path)
  const args = [...annotation.path.split(' '), ...rest, ...flagsToArgv(flags)]
  const result = runFishCliOnSnapshot({ fishHome: deps.paths.fishHome, dbPath: deps.paths.dbPath, args })
  if (!result.ok) {
    const detail = [result.failure, result.stderr.trim()].filter((line) => line !== null && line !== '').join('\n')
    return {
      kind: 'error',
      text: [
        `/fish ${annotation.path} 执行失败`,
        `  原因：${detail === '' ? '未知（fish.py 无输出）' : detail}`,
        `  argv：${result.argv.join(' ')}`,
        `  快照：${result.snapshot === null ? '未建立' : `${result.snapshot.dir}（${result.snapshot.bytes} B，as-of ${result.snapshot.asOf}）`}`,
        '  说明：命令在**只读快照副本**上执行，生产库没有参与本次调用。',
      ].join('\n'),
    }
  }
  const snapshot = result.snapshot
  const header = [
    `fish ${annotation.path} —— 只读转发（快照 as-of ${snapshot?.asOf ?? '未知'}）`,
    `生产库 sha256：${result.productionShaBefore ?? '读不到'} → ${result.productionShaAfter ?? '读不到'}` +
      `${result.productionShaBefore !== null && result.productionShaBefore === result.productionShaAfter ? '（未变）' : '（有变化：可能来自其它写者）'}`,
    snapshot === null ? '快照：未建立（异常路径）' : `快照：${snapshot.files.join(' + ')}，${snapshot.bytes} B，integrity=${snapshot.integrity}`,
    '（Python 侧会跑 SCHEMA/migrate 与审计追加——它们全部落在这次调用独有的临时副本上，退出即删）',
  ].join('\n')
  const body = result.stdout.trimEnd()
  return { kind: 'success', text: `${header}\n${body === '' ? '（命令没有输出）' : body}` }
}

/**
 * 执行一次已经解析好的 CLI 调用（纯函数，判据脚本直接复用）。
 * @param path 命中的命令路径（叶子）
 * @param rest 路径之后的 token（原样透传）
 */
export function runFishCli(
  path: string,
  rest: readonly string[],
  flags: ParsedCommandInput['flags'],
  deps: CommandDeps,
): CommandResult {
  const annotation = findCliAnnotation(path)
  if (annotation === null) {
    return { kind: 'error', text: `未知命令：fish ${path}\n\n${renderCliHelpText()}` }
  }
  if (flags.help === true) {
    return { kind: 'success', text: renderCliHelpText(path) }
  }
  try {
    switch (annotation.impl) {
      case 'ts': {
        const result = runTsCommand(annotation, rest, flags, deps)
        // `CommandResult.text` 在宿主类型里是可选的：缺省按空串处理，不让截断器拿到 undefined。
        const { text } = finalizeOutput(result.text ?? '', `cli-${annotation.path.replace(/\s+/g, '-')}`, deps.paths.packageRoot)
        return { kind: result.kind, text }
      }
      case 'proxy-readonly': {
        const result = runProxyCommand(annotation, rest, flags, deps)
        const { text } = finalizeOutput(result.text ?? '', `cli-${annotation.path.replace(/\s+/g, '-')}`, deps.paths.packageRoot)
        return { kind: result.kind, text }
      }
      case 'unsupported-shadow':
        return { kind: 'error', text: renderCliRefusal(annotation, annotation.reason) }
      case 'python-only':
        return { kind: 'error', text: renderCliGuidance(annotation, annotation.reason) }
      default:
        return {
          kind: 'error',
          text: `命令 ${annotation.path} 的实现归属无法识别：${String(annotation.impl)}（cli-map 被人手改坏了？）`,
        }
    }
  } catch (error) {
    const fishError = toFishError(error)
    deps.logger.error('CLI 命令失败', { path: annotation.path, code: fishError.code })
    return { kind: 'error', text: fishError.format() }
  }
}

/**
 * CLI 命令面的总入口：解析 → 分发。
 * @param positional 命令 token（第一个是一级命令名）
 */
export function handleCliInput(
  positional: readonly string[],
  flags: ParsedCommandInput['flags'],
  deps: CommandDeps,
): CommandResult {
  const route = resolveCliRoute(positional)
  if (route === null) {
    const attempted = positional.slice(0, 2).join(' ')
    return {
      kind: 'error',
      text: `未知子命令：${attempted}\n\n${renderCliHelpText()}`,
    }
  }
  if (route.annotation === null) {
    // 命中的是容器（`kb vault` / `kb`）：给子命令列表，并说明剩下的 token 该怎么给。
    return { kind: 'success', text: renderCliHelpText(route.path) }
  }
  const rest = route.rest
  if (rest.length > 0) {
    // 位置参数原样交给实现（`recall 关键词`、`neighbors <code>`）；但写命令在拒绝时不需要它。
    return runFishCli(route.path, rest, flags, deps)
  }
  return runFishCli(route.path, [], flags, deps)
}

/** 供回显用：一条替代命令的人读文本（命令面与判据脚本共用同一份渲染）。 */
export { formatReplacement }
