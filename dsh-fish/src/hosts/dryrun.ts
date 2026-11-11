/**
 * hooks 安装 **dry-run**：只输出"目标文件会变成什么"，一个字都不写。
 *
 * ## 事实源是生产树的安装器，不是本模块
 *
 * 目标配置、合并、序列化（缩进/行尾/BOM）、以及"别人的事件会不会被删"全部由
 * `scripts/probe/dump-hooks-plan.py` 现场问 `p0/install_hooks.py` 算出来；
 * 本模块只做两件本地的事：**渲染 diff** + **自证没写**。
 *
 * 为什么值钱：`merge_hooks` 的语义是"只覆盖/新增 fish 负责的四个事件，其它事件一个字节
 * 不动"。预览若说"只会多 4 条"、真装下去却删了别人的 `Stop`，那是这套安装器最该防的事故——
 * 而"预览与执行不是同一份实现"正是那种事故的温床。所以这里**不重写合并逻辑**。
 *
 * ## 幂等
 *
 * 输出里**不含时间戳**（用两侧 sha256 + 计划摘要代替"什么时候算的"）：同一份配置连跑两次
 * 必须逐字节相同，否则"diff 预览"就无法作为判据（判据 5）。
 *
 * @module dsh-fish/hosts/dryrun
 */

import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import path from 'node:path'

import { FishError } from '../core/errors.ts'
import { resolvePythonBin } from '../ports/python.ts'

/** 计划的默认超时：Python 冷启动 + 读两份配置，15s 足够。 */
export const HOOKS_PLAN_TIMEOUT_MS = 15000

/** 单个宿主的 diff 最多输出这么多行（再多就落盘，只回显路径）。 */
export const DIFF_LINE_LIMIT = 80

/** Python 探针返回的单个宿主计划（字段名与 `dump-hooks-plan.py` 一致）。 */
export interface HostPlanPayload {
  readonly host: string
  readonly path: string
  readonly exists: boolean
  readonly originalBytes: number
  readonly originalSha256: string | null
  readonly plannedBytes: number
  readonly plannedSha256: string
  readonly changed: boolean
  readonly shape: 'hooks' | 'bare'
  readonly preserved: readonly string[]
  readonly dropped: readonly string[]
  readonly lineEndings: 'CRLF' | 'LF'
  readonly bom: boolean
  readonly diffLines: readonly string[]
  readonly problems: readonly string[]
  readonly matchers: readonly { event: string; index: number; matcher: unknown; kind: string; reason: string }[]
  /** 结构化事实（供"TS 读数 vs Python 读数"对拍；不比对自然语言文案）。 */
  readonly facts: {
    readonly declaredEvents: readonly string[]
    readonly fishEntries: readonly { event: string; script: string; matcher: unknown }[]
    readonly fishEntryCount: number
    readonly collectorCount: number
    readonly preToolGroups: number
    readonly requiredEventsPresent: readonly string[]
  }
  readonly originalText: string
  readonly plannedText: string
  readonly targetUnchanged: boolean
  readonly afterSha256: string | null
}

/** Python 探针的完整产出。 */
export interface HooksPlanPayload {
  readonly python: string
  readonly fishHome: string
  readonly installer: { readonly path: string; readonly sha256_12: string }
  readonly hosts: readonly HostPlanPayload[]
  readonly writeAttempts: number
}

/** 渲染后的一个宿主计划（多了本地算的 diff 与自证读数）。 */
export interface HostDryRunResult {
  readonly host: string
  readonly path: string
  readonly exists: boolean
  readonly beforeSha256: string | null
  readonly afterSha256: string | null
  readonly plannedSha256: string
  readonly changed: boolean
  readonly shape: string
  readonly preserved: readonly string[]
  readonly dropped: readonly string[]
  readonly lineEndings: string
  readonly bom: boolean
  /** 统一 diff（`-` 现有 / `+` 计划）；没变化时是空串。 */
  readonly diff: string
  readonly diffLines: number
  readonly problems: readonly string[]
  readonly pythonDiffLines: readonly string[]
  /** 本地复核：算完计划后目标文件 sha256 与之前相同。 */
  readonly targetUnchanged: boolean
}

/** 一次 dry-run 的结果。 */
export interface HostsDryRunResult {
  readonly fishHome: string
  readonly installerPath: string
  readonly installerSha12: string
  readonly python: string
  readonly hosts: readonly HostDryRunResult[]
  /** 写入次数（恒 0；判据直接断言这个字段）。 */
  readonly writeAttempts: number
  /** 计划本身的摘要（对同一份配置可复现；连跑两次必须相同）。 */
  readonly planSha256: string
}

/** 文件 sha256（不存在返回 `null`）。 */
function sha256Of(filePath: string): string | null {
  try {
    return createHash('sha256').update(readFileSync(filePath)).digest('hex')
  } catch {
    return null
  }
}

/** 按行切（保留行尾判定；`\r\n` 与 `\n` 都算行边界）。 */
function splitLines(text: string): string[] {
  if (text === '') return []
  const normalized = text.replace(/\r\n/g, '\n')
  const lines = normalized.split('\n')
  if (lines.length > 0 && lines[lines.length - 1] === '') lines.pop()
  return lines
}

/**
 * 行级 LCS 统一 diff（`@@ -a,b +c,d @@` 分块）。
 *
 * 为什么自己写：只要"两边一行都没变"就必须输出空 diff（而不是"整个文件都重写"），
 * 否则预览里的噪声会盖住真正的风险点（例如"其它事件会被删掉"）。
 * 文件规模（1400 行级别）跑 O(n·m) 的 LCS 是毫秒级，没必要引第三方依赖。
 */
export function unifiedDiff(beforeText: string, afterText: string, options: { readonly context?: number } = {}): string {
  const context = options.context ?? 3
  const left = splitLines(beforeText)
  const right = splitLines(afterText)
  const n = left.length
  const m = right.length
  // lcs[i][j] = left[i..] 与 right[j..] 的最长公共子序列长度
  const width = m + 1
  const lcs = new Int32Array((n + 1) * width)
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      lcs[i * width + j] =
        left[i] === right[j]
          ? (lcs[(i + 1) * width + (j + 1)] as number) + 1
          : Math.max(lcs[(i + 1) * width + j] as number, lcs[i * width + (j + 1)] as number)
    }
  }
  /** 0 = 相同，1 = 删除（仅左侧），2 = 新增（仅右侧）。 */
  const ops: { kind: 0 | 1 | 2; text: string }[] = []
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (left[i] === right[j]) {
      ops.push({ kind: 0, text: left[i] as string })
      i += 1
      j += 1
    } else if ((lcs[(i + 1) * width + j] as number) >= (lcs[i * width + (j + 1)] as number)) {
      ops.push({ kind: 1, text: left[i] as string })
      i += 1
    } else {
      ops.push({ kind: 2, text: right[j] as string })
      j += 1
    }
  }
  while (i < n) {
    ops.push({ kind: 1, text: left[i] as string })
    i += 1
  }
  while (j < m) {
    ops.push({ kind: 2, text: right[j] as string })
    j += 1
  }
  if (!ops.some((op) => op.kind !== 0)) return ''

  // 收集需要输出的行号区间（变更行 ± context）。
  const interesting = new Set<number>()
  ops.forEach((op, index) => {
    if (op.kind === 0) return
    for (let k = Math.max(0, index - context); k <= Math.min(ops.length - 1, index + context); k += 1) interesting.add(k)
  })
  const sorted = [...interesting].sort((a, b) => a - b)
  const hunks: number[][] = []
  for (const index of sorted) {
    const current = hunks[hunks.length - 1]
    if (current !== undefined && index === (current[current.length - 1] as number) + 1) current.push(index)
    else hunks.push([index])
  }

  const out: string[] = []
  for (const hunk of hunks) {
    const start = hunk[0] as number
    const end = hunk[hunk.length - 1] as number
    const slice = ops.slice(start, end + 1)
    const leftBefore = ops.slice(0, start).filter((op) => op.kind !== 2).length
    const rightBefore = ops.slice(0, start).filter((op) => op.kind !== 1).length
    const leftCount = slice.filter((op) => op.kind !== 2).length
    const rightCount = slice.filter((op) => op.kind !== 1).length
    out.push(`@@ -${leftBefore + 1},${leftCount} +${rightBefore + 1},${rightCount} @@`)
    for (const op of slice) {
      out.push(`${op.kind === 0 ? ' ' : op.kind === 1 ? '-' : '+'}${op.text}`)
    }
  }
  return out.join('\n')
}

/** 计划摘要：只取"计划形态"字段（不含原文），同一配置连跑两次必然相同。 */
function planDigest(payload: HooksPlanPayload): string {
  const shape = payload.hosts.map((host) => ({
    host: host.host,
    path: host.path,
    exists: host.exists,
    originalSha256: host.originalSha256,
    plannedSha256: host.plannedSha256,
    changed: host.changed,
    preserved: host.preserved,
    dropped: host.dropped,
  }))
  return createHash('sha256').update(JSON.stringify(shape)).digest('hex')
}

/**
 * 跑一次 dry-run：问 Python 要计划 → 本地渲染 diff → 自证两个目标文件没变。
 *
 * @param fishHome 生产树（含 `fish.py` / `install_hooks.py`）
 * @param options `packageRoot` 用来定位本仓的探针脚本；`hostConfigPaths` 是**本地独立**
 *   记录的目标文件（与 Python 的口径应一致），用于"跑之前/跑之后"两次 sha256 对照——
 *   自证不能只让被验证方（Python 计划）自己说"我没写"。
 * @throws FishError `SHADOW_UNSUPPORTED` —— 生产树缺安装器 / Python 失败
 * @throws FishError `SHADOW_READ_ONLY` —— 目标文件在 dry-run 前后不一致
 */
export function runHooksDryRun(
  fishHome: string,
  options: { readonly packageRoot: string; readonly timeoutMs?: number; readonly hostConfigPaths?: readonly string[] },
): HostsDryRunResult {
  const timeoutMs = options.timeoutMs ?? HOOKS_PLAN_TIMEOUT_MS
  const script = path.join(options.packageRoot, 'scripts', 'probe', 'dump-hooks-plan.py')
  // 先记一次目标文件摘要（独立于 Python 计划的一道：不靠被验证方自证）。
  const before = new Map<string, string | null>()
  for (const configPath of options.hostConfigPaths ?? []) before.set(configPath, sha256Of(configPath))
  const child = spawnSync(resolvePythonBin(), [script], {
    encoding: 'utf8',
    timeout: timeoutMs,
    maxBuffer: 16 * 1024 * 1024,
    windowsHide: true,
    env: { ...process.env, FISH_HOME: fishHome, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
  })
  if (child.error !== undefined) {
    throw new FishError('SHADOW_UNSUPPORTED', `跑 hooks 计划失败：${child.error.message}`, {
      hint: '确认 python 可用（FISH_PYTHON），或先用 `python install_hooks.py --check` 在终端核对',
      details: { script },
    })
  }
  if (child.status !== 0) {
    throw new FishError('SHADOW_UNSUPPORTED', `dump-hooks-plan.py 退出码 ${String(child.status)}`, {
      hint: '看导出的 stderr；安装器读不出配置时它会主动非 0',
      details: { stderr: (child.stderr || '').slice(0, 2000) },
    })
  }
  let payload: HooksPlanPayload
  try {
    payload = JSON.parse(child.stdout) as HooksPlanPayload
  } catch (error) {
    throw new FishError('SHADOW_UNSUPPORTED', `dump-hooks-plan.py 输出不是 JSON：${(error as Error).message}`, {
      details: { head: (child.stdout || '').slice(0, 400) },
    })
  }
  if (payload.writeAttempts !== 0) {
    throw new FishError('SHADOW_READ_ONLY', `安装器报告 writeAttempts=${payload.writeAttempts}（dry-run 必须为 0）`)
  }
  const hosts: HostDryRunResult[] = payload.hosts.map((host) => {
    const recorded = before.get(host.path)
    const afterSha = sha256Of(host.path)
    const unchanged = host.targetUnchanged && (recorded === undefined || recorded === afterSha)
    return {
      host: host.host,
      path: host.path,
      exists: host.exists,
      beforeSha256: host.originalSha256,
      afterSha256: host.afterSha256,
      plannedSha256: host.plannedSha256,
      changed: host.changed,
      shape: host.shape,
      preserved: host.preserved,
      dropped: host.dropped,
      lineEndings: host.lineEndings,
      bom: host.bom,
      diff: unifiedDiff(host.originalText, host.plannedText),
      diffLines: splitLines(unifiedDiff(host.originalText, host.plannedText)).length,
      problems: host.problems,
      pythonDiffLines: host.diffLines,
      targetUnchanged: unchanged,
    }
  })
  const touched = hosts.filter((host) => !host.targetUnchanged)
  if (touched.length > 0) {
    throw new FishError('SHADOW_READ_ONLY', `dry-run 之后目标文件变了：${touched.map((host) => host.path).join(', ')}`, {
      hint: '立刻核对（`git diff` / 备份）：dry-run 承诺零写入',
    })
  }
  return {
    fishHome,
    installerPath: payload.installer.path,
    installerSha12: payload.installer.sha256_12,
    python: payload.python,
    hosts,
    writeAttempts: 0,
    planSha256: planDigest(payload),
  }
}

/** 把结果渲染成人读文本（Markdown + 统一 diff）。 */
export function renderHostsDryRun(result: HostsDryRunResult): string {
  const lines: string[] = [
    '# hooks 安装 dry-run（**只输出，不写盘**）',
    `生产树：${result.fishHome}｜安装器：${result.installerPath}（sha256=${result.installerSha12}）`,
    `计划摘要：${result.planSha256.slice(0, 16)}…｜写入次数：${result.writeAttempts}`,
    '（同一份配置连跑两次，本段输出应逐字节相同；目标文件 sha256 前后相同）',
  ]
  for (const host of result.hosts) {
    lines.push('')
    lines.push(`## ${host.host} → ${host.path}`)
    lines.push(
      `现状：${host.exists ? '存在' : '**不存在（会新建）**'}｜形态=${host.shape}｜行尾=${host.lineEndings}${host.bom ? '｜BOM=有' : ''}`,
    )
    lines.push(`sha256：现状 ${host.beforeSha256?.slice(0, 16) ?? '—'} → 计划 ${host.plannedSha256.slice(0, 16)}`)
    lines.push(`变更：${host.changed ? '**会改**' : '无（已与目标一致）'}`)
    if (host.dropped.length > 0) {
      lines.push(`⚠ 将被**删除**的其它事件：${host.dropped.join(', ')}（合并模式不会发生，仅 --force 才会）`)
    }
    lines.push(`其它事件（原样保留）：${host.preserved.join(', ') || '（无）'}`)
    if (host.pythonDiffLines.length > 0) {
      lines.push('安装器判据：')
      for (const line of host.pythonDiffLines) lines.push(`  ${line}`)
    }
    if (host.problems.length > 0) {
      lines.push(`安装器报告的问题（${host.problems.length}）：`)
      for (const problem of host.problems) lines.push(`  · ${problem}`)
    }
    lines.push('')
    if (host.diff === '') {
      lines.push('diff：（空——现状已等于目标配置）')
    } else {
      const diffLines = host.diff.split('\n')
      const shown = diffLines.slice(0, DIFF_LINE_LIMIT)
      lines.push('```diff')
      for (const line of shown) lines.push(line)
      if (diffLines.length > shown.length) {
        lines.push(`…（diff 共 ${diffLines.length} 行，此处只显示前 ${DIFF_LINE_LIMIT} 行）`)
      }
      lines.push('```')
    }
    lines.push(`目标文件未变：${host.targetUnchanged ? '✓' : '✗'}（sha256 ${host.afterSha256?.slice(0, 16) ?? '—'}）`)
  }
  lines.push('')
  lines.push('边界：本命令**不实现 apply**（换心期才做，且需用户逐次确认）。')
  return lines.join('\n')
}
