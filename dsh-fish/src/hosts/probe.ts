/**
 * 宿主 hooks **只读探针**：看清"现在配了什么"，一个字都不写。
 *
 * ## 为什么不复用 `install_hooks --check`
 *
 * `--check` 是**安装器的判据**（会 exit 2、会按 install_hooks.py 里那一刻的目标配置比），
 * 而这里要的是**读数**：每宿主几条 fish 条目、每条挂在哪个事件、matcher 是什么、
 * 脚本路径解析到哪、文件在不在。读数与判据是两件事——把读数做成 `--check` 的字符串解析，
 * 下次安装器改一下输出格式，这里就会静默读错。
 *
 * ## 与 Python 侧的一致性
 *
 * `matcher_dialect` / `problems` 复刻的是 `p0/install_hooks.py` 的**判据语义**
 * （F1 + Round 5/N1：只认纯字面备选这一档，锚定/括号/内联 flag 一律拒绝）。
 * 复刻有漂移风险，所以 `test/hosts/dryrun-conformance.mjs` 拿 Python 现场的
 * `validate_hooks()` 结果与本模块逐条比对（差异即红）——本模块只许落后被发现，不许悄悄漂开。
 *
 * @module dsh-fish/hosts/probe
 */

import { createHash } from 'node:crypto'
import { existsSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'

import {
  COLLECTOR_SCRIPTS,
  FISH_HOOK_SCRIPTS,
  REQUIRED_EVENTS,
  WRITE_MATCHER_REQUIRED,
  type HostEvent,
  type HostSpec,
} from './registry.ts'

/** 纯字面备选：只允许 `[A-Za-z0-9_]` 与 `|`（宿主按字面比较、不编译正则）。 */
const LITERAL_ALTERNATIVES = /^[A-Za-z0-9_]+(?:\|[A-Za-z0-9_]+)*$/

/** 内联 flag（`(?i)` / `(?i:…)`）：**策略性**拒绝——两个宿主的正则语义不可假设。 */
const INLINE_FLAG = /\(\?[A-Za-z]+[:)]/

/** matcher 契约的判定结果（`kind` 只有 `literal` 或空串两种非空态）。 */
export interface MatcherVerdict {
  readonly kind: '' | 'literal'
  readonly reason: string
}

/** 一条挂着的 fish hook。 */
export interface FishHookEntry {
  /** 所在事件（配置里的键）。 */
  readonly event: string
  /** 该事件组的 matcher（没写就是 `null` = 全部触发）。 */
  readonly matcher: string | null
  /** 原始命令串。 */
  readonly command: string
  /** 命令里的脚本文件名（`fish_pretool_hook.py`）。 */
  readonly script: string | null
  /** 解析出的脚本绝对路径（拿不到就是 `null`）。 */
  readonly scriptPath: string | null
  /** 脚本文件是否真的存在。 */
  readonly scriptExists: boolean
  /** 配置里的超时（秒）。 */
  readonly timeoutSec: number | null
  /** matcher 契约判定。 */
  readonly dialect: MatcherVerdict
  /** 是否负责"采集动作流"（每宿主只应有 1 个）。 */
  readonly collector: boolean
}

/** 一个宿主的探针读数。 */
export interface HostProbeResult {
  readonly id: string
  readonly label: string
  readonly configPath: string
  readonly exists: boolean
  readonly bytes: number
  readonly sha256_12: string | null
  /** 配置形态：`hooks`（`{hooks:{…}}`）/ `bare`（顶层就是事件表）/ `missing`。 */
  readonly shape: 'hooks' | 'bare' | 'missing'
  /** 配置里出现的事件名（含非 fish 的事件）。 */
  readonly events: readonly string[]
  /** fish 负责的四个事件里，配置中存在的那些。 */
  readonly requiredEventsPresent: readonly string[]
  /** 非 fish 事件（合并语义下必须原样保留的那些）。 */
  readonly otherEvents: readonly string[]
  /** fish 条目（命令串里含 fish 的 hook 脚本）。 */
  readonly fishEntries: readonly FishHookEntry[]
  /** 读数层面的问题清单（空 = 通过安装器的同一套契约）。 */
  readonly problems: readonly string[]
  /** 这个宿主配置的特殊说明（来自登记表）。 */
  readonly note: string
}

/** 读一份配置文本（保留 BOM/CRLF 判定；读不到返回 `null`）。 */
function readConfigText(configPath: string): string | null {
  try {
    return readFileSync(configPath, 'utf8')
  } catch {
    return null
  }
}

/** 文件 sha256 前 12 位（读不到返回 `null`）。 */
function sha12(filePath: string): string | null {
  try {
    return createHash('sha256').update(readFileSync(filePath)).digest('hex').slice(0, 12)
  } catch {
    return null
  }
}

/**
 * matcher 是否满足宿主方言契约（**原串**判定，不做 strip）。
 *
 * 判据必须用原串：宿主按原串解释，`" Write|write"` 的前导空格会让字面比较永不命中，
 * 所以它也必须红（只对空值/纯空白判空）。
 */
export function matcherDialect(matcher: string | null | undefined): MatcherVerdict {
  const raw = matcher === null || matcher === undefined ? '' : String(matcher)
  if (raw.trim() === '') {
    return { kind: '', reason: 'matcher 为空（null/空串都算没写，但 PreToolUse 必须有）' }
  }
  if (raw !== raw.trim()) {
    return { kind: '', reason: `matcher 首尾含空白（${JSON.stringify(raw)}）：宿主按原串解释，字面比较会永不命中` }
  }
  if (LITERAL_ALTERNATIVES.test(raw)) {
    return { kind: 'literal', reason: '纯字面备选（宿主不编译正则）' }
  }
  if (INLINE_FLAG.test(raw)) {
    return {
      kind: '',
      reason: '含内联 flag：只认纯字面备选，正则形态一律**策略性**拒绝（两宿主语义不可假设）',
    }
  }
  return {
    kind: '',
    reason: '不是纯字面备选（只允许 [A-Za-z0-9_] 与 |）；锚定/括号等正则形态一律拒绝',
  }
}

/** matcher 是否覆盖写类工具的两种大小写。 */
export function matcherCoversWriteTools(matcher: string | null | undefined): boolean {
  if (matcherDialect(matcher).kind === '') return false
  const literals = new Set((String(matcher ?? '').match(/[A-Za-z0-9_]+/g) ?? []).map((token) => token))
  return WRITE_MATCHER_REQUIRED.every((required) => literals.has(required))
}

/** 从一条命令串里取脚本路径（引号包裹优先，其次裸 token）。 */
export function scriptPathOf(command: string): string | null {
  const quoted = command.match(/"([^"]+\.py)"/g) ?? []
  if (quoted.length > 0) {
    const first = quoted[0] as string
    return first.slice(1, -1)
  }
  const bare = command.match(/(?:^|\s)([A-Za-z]:[\\/][^\s]+\.py|\/[^\s]+\.py|[\w.-]+\.py)/)
  return bare === null ? null : (bare[1] as string)
}

/** 事件表的形态判定（`{hooks:{…}}` / 裸事件表）。 */
function hooksOf(document: unknown): { hooks: Record<string, unknown>; shape: 'hooks' | 'bare' } {
  if (typeof document !== 'object' || document === null) return { hooks: {}, shape: 'hooks' }
  const candidate = document as Record<string, unknown>
  const nested = candidate.hooks
  if (typeof nested === 'object' && nested !== null && !Array.isArray(nested)) {
    return { hooks: nested as Record<string, unknown>, shape: 'hooks' }
  }
  const values = Object.values(candidate)
  if (values.length > 0 && values.every((value) => Array.isArray(value))) {
    return { hooks: candidate, shape: 'bare' }
  }
  return { hooks: {}, shape: 'hooks' }
}

/** 遍历配置里的所有 (event, group, hook)。 */
function* iterHooks(hooks: Record<string, unknown>): Generator<{ event: string; group: Record<string, unknown>; hook: Record<string, unknown> }> {
  for (const [event, groups] of Object.entries(hooks)) {
    if (!Array.isArray(groups)) continue
    for (const group of groups) {
      if (typeof group !== 'object' || group === null) continue
      const record = group as Record<string, unknown>
      const list = record.hooks
      if (!Array.isArray(list)) continue
      for (const hook of list) {
        if (typeof hook !== 'object' || hook === null) continue
        yield { event, group: record, hook: hook as Record<string, unknown> }
      }
    }
  }
}

/** 一条 hook 是不是 fish 的（命令里带 fish 的 hook 脚本名）。 */
function isFishScript(scriptName: string): boolean {
  return (FISH_HOOK_SCRIPTS as readonly string[]).includes(scriptName) || scriptName.startsWith('fish_')
}

/**
 * 探一个宿主（**只读**：只 `readFileSync`/`statSync`）。
 * @param spec 宿主登记项
 * @param fishHome fish 家目录（判"脚本文件在不在"用；install_hooks 用 `folder` 参数表达同一件事）
 */
export function probeHost(spec: HostSpec, fishHome: string): HostProbeResult {
  const text = readConfigText(spec.configPath)
  if (text === null) {
    return {
      id: spec.id,
      label: spec.label,
      configPath: spec.configPath,
      exists: false,
      bytes: 0,
      sha256_12: null,
      shape: 'missing',
      events: [],
      requiredEventsPresent: [],
      otherEvents: [],
      fishEntries: [],
      problems: [
        `配置文件不存在：${spec.configPath}`,
        `（需要安装时跑 \`python install_hooks.py --apply --host ${spec.id}\`，然后**重启宿主**）`,
      ],
      note: spec.note,
    }
  }
  let document: unknown = null
  let parseError: string | null = null
  try {
    document = JSON.parse(text.replace(/^\uFEFF/, ''))
  } catch (error) {
    parseError = error instanceof Error ? error.message : String(error)
  }
  const { hooks, shape } = hooksOf(document)
  const events = Object.keys(hooks)
  const entries: FishHookEntry[] = []
  for (const { event, group, hook } of iterHooks(hooks)) {
    const command = typeof hook.command === 'string' ? hook.command : ''
    const scriptPath = command === '' ? null : scriptPathOf(command)
    const script = scriptPath === null ? null : scriptPath.split(/[\\/]/).pop() ?? null
    if (script === null || !isFishScript(script)) continue
    const matcher = typeof group.matcher === 'string' ? group.matcher : null
    entries.push({
      event,
      matcher,
      command,
      script,
      scriptPath,
      scriptExists: scriptPath !== null && existsSync(scriptPath),
      timeoutSec: typeof hook.timeout === 'number' ? hook.timeout : null,
      dialect: matcherDialect(matcher),
      collector: (COLLECTOR_SCRIPTS as readonly string[]).includes(script),
    })
  }

  const problems: string[] = []
  if (parseError !== null) problems.push(`配置不是合法 JSON：${parseError}`)
  for (const event of REQUIRED_EVENTS) {
    if (!events.includes(event)) problems.push(`缺事件 ${event}`)
  }
  const collectors = entries.filter((entry) => entry.collector && (REQUIRED_EVENTS as readonly string[]).includes(entry.event))
  if (collectors.length !== 1) {
    const detail = collectors.length === 0 ? '无' : collectors.map((entry) => `${entry.event}:${entry.script ?? '?'}`).join('、')
    problems.push(`采集器数量应为 1，实际 ${collectors.length}（${detail}）`)
  }
  const preTool = entries.filter((entry) => entry.event === 'PreToolUse')
  if (preTool.length !== 1) {
    problems.push(`PreToolUse 应有 1 组 fish hook，实际 ${preTool.length}`)
  } else {
    const entry = preTool[0] as FishHookEntry
    if (entry.dialect.kind === '') {
      problems.push(`PreToolUse matcher 不满足宿主方言契约（${entry.dialect.reason}）：${JSON.stringify(entry.matcher)}`)
    } else if (!matcherCoversWriteTools(entry.matcher)) {
      problems.push(`PreToolUse matcher 未覆盖写类工具大小写：${JSON.stringify(entry.matcher)}`)
    }
    if (entry.scriptPath === null) problems.push('PreToolUse 命令里没有 .py 脚本路径')
    else if (!entry.scriptExists) problems.push(`PreToolUse 脚本不存在：${entry.scriptPath}`)
  }
  // 其余事件组如果也挂了 matcher，同样要过方言契约（WorkBuddy 的 PostToolUse 就挂了）。
  for (const entry of entries) {
    if (entry.event === 'PreToolUse') continue
    if (entry.matcher === null) continue
    if (entry.dialect.kind === '') {
      problems.push(`${entry.event} matcher 不满足宿主方言契约（${entry.dialect.reason}）：${JSON.stringify(entry.matcher)}`)
    }
  }
  for (const script of FISH_HOOK_SCRIPTS) {
    if (!existsSync(path.join(fishHome, script))) {
      problems.push(`缺脚本 ${script}（fish-home = ${fishHome}）`)
    }
  }

  const requiredEventsPresent = REQUIRED_EVENTS.filter((event) => events.includes(event)) as HostEvent[]
  return {
    id: spec.id,
    label: spec.label,
    configPath: spec.configPath,
    exists: true,
    bytes: (() => {
      try {
        return statSync(spec.configPath).size
      } catch {
        return text.length
      }
    })(),
    sha256_12: sha12(spec.configPath),
    shape,
    events,
    requiredEventsPresent: [...requiredEventsPresent],
    otherEvents: events.filter((event) => !(REQUIRED_EVENTS as readonly string[]).includes(event)),
    fishEntries: entries,
    problems,
    note: spec.note,
  }
}

/** 探全部宿主。 */
export function probeAllHosts(specs: readonly HostSpec[], fishHome: string): readonly HostProbeResult[] {
  return specs.map((spec) => probeHost(spec, fishHome))
}

/** 把探针读数渲染成人读文本（Markdown 表格，命令面直接贴）。 */
export function renderHostProbe(results: readonly HostProbeResult[]): string {
  const lines: string[] = ['# 跨宿主 hooks 只读探针（不写任何文件）']
  for (const result of results) {
    lines.push('')
    lines.push(`## ${result.label}（host=${result.id}）`)
    lines.push(`配置：${result.configPath}`)
    lines.push(
      result.exists
        ? `存在：是（${result.bytes} B，sha256=${result.sha256_12 ?? '读不到'}，形态=${result.shape}）`
        : '存在：**否**',
    )
    if (result.exists) {
      lines.push(`事件：${result.events.join(' / ') || '（无）'}`)
      lines.push(`fish 条目：${result.fishEntries.length} 条｜非 fish 事件（合并时必须保留）：${result.otherEvents.join(' / ') || '（无）'}`)
      lines.push('')
      lines.push('| 事件 | matcher | 脚本 | 脚本存在 | 超时 | 方言 |')
      lines.push('|---|---|---|---|---|---|')
      for (const entry of result.fishEntries) {
        lines.push(
          `| ${entry.event} | ${entry.matcher === null ? '（未挂）' : entry.matcher} | ${entry.script ?? '?'} | ` +
            `${entry.scriptExists ? '✓' : '✗'} | ${entry.timeoutSec === null ? '—' : `${entry.timeoutSec}s`} | ` +
            `${entry.dialect.kind === '' ? '✗' : 'literal'} |`,
        )
      }
    }
    lines.push('')
    if (result.problems.length === 0) {
      lines.push('读数结论：✓ 通过（4 事件 / 1 采集器 / matcher 方言契约 + 大小写齐备 / 6 脚本齐全）')
    } else {
      lines.push(`读数结论：✗ ${result.problems.length} 个问题`)
      for (const problem of result.problems) lines.push(`  · ${problem}`)
    }
  }
  return lines.join('\n')
}
