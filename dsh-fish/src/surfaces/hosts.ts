/**
 * `/fish hosts …`：**跨宿主管理面的只读三件**（probe / health / dry-run）。
 *
 * 三条铁律写在这里，也写进每一条回显：
 *   1. **不做 apply**——换心期才做，且需用户逐次确认。本模块没有任何写文件的调用
 *      （除了命令面自己的输出截断日志，落在本仓 `.tmp/`，不碰宿主配置）。
 *   2. **不改别宿主任何非 fish 段落**——dry-run 只输出 diff，合并逻辑由生产树自己算。
 *   3. **失败要响亮**——读不出配置就说"读不出来 + 下一步"，不返回空表让人误读成"没配"。
 *
 * @module dsh-fish/surfaces/hosts
 */

import type { CommandResult } from '@deepseek-ai/dsh-commands'

import { toFishError } from '../core/errors.ts'
import { checkHostHealth, heartbeatDir, renderHostHealth } from '../hosts/health.ts'
import { probeAllHosts, renderHostProbe } from '../hosts/probe.ts'
import { resolveHostSpecs, type HostSpec } from '../hosts/registry.ts'
import { renderHostsDryRun, runHooksDryRun } from '../hosts/dryrun.ts'
import type { CommandDeps } from './commands.ts'
import { finalizeOutput } from './output.ts'

/** `/fish hosts` 的子命令（顺序即回显顺序）。 */
export const HOSTS_SUBCOMMANDS = ['probe', 'health', 'dry-run'] as const

/** `hosts` 的用法文本。 */
export const HOSTS_USAGE = `用法：/fish hosts <子命令>
  probe     只读探针：每个宿主的配置文件 / fish 条目 / matcher / 脚本路径（不写任何文件）
  health    心跳健康度：每宿主 × 4 事件的最近心跳与新鲜度（>2h 视为陈旧）
  dry-run   安装预览：目标文件"会变成什么"的统一 diff（**只输出，不写盘**）

边界：不做 apply（换心期才做）；不改别宿主任何非 fish 段落；不改 hooks.json 一个字节。`

/** 解析后的 flags（与 commands.ts 的解析器同形，这里只用到字符串/布尔两种）。 */
export type HostsFlags = Readonly<Record<string, string | true>>

/** 取宿主登记项（可被测试注入）。 */
export function hostsFor(deps: CommandDeps): readonly HostSpec[] {
  return resolveHostSpecs()
}

/** 只允许这几个开关：多一个就说明调用方以为这里有别的能力（响亮失败胜过静默忽略）。 */
const ALLOWED_FLAGS = new Set(['help'])

/** 校验开关白名单。 */
function assertFlags(flags: HostsFlags, subcommand: string): void {
  const unknown = Object.keys(flags).filter((name) => !ALLOWED_FLAGS.has(name))
  if (unknown.length > 0) {
    throw new Error(
      `/fish hosts ${subcommand} 不认识这些开关：${unknown.map((name) => `--${name}`).join(' ')}（只支持 --help）`,
    )
  }
}

/** `probe`：只读探针。 */
export function renderHostsProbe(deps: CommandDeps): string {
  const specs = hostsFor(deps)
  const results = probeAllHosts(specs, deps.paths.fishHome)
  const failed = results.filter((result) => result.problems.length > 0)
  const header =
    `只读探针：${results.length} 个宿主｜有问题的 ${failed.length} 个｜fish-home=${deps.paths.fishHome}` +
    '\n（本命令只 readFileSync/statSync；没有创建、没有改名、没有写入任何宿主配置）'
  return `${header}\n${renderHostProbe(results)}`
}

/** `health`：心跳健康度。 */
export function renderHostsHealth(deps: CommandDeps, now: number = Date.now()): string {
  const specs = hostsFor(deps)
  const probes = probeAllHosts(specs, deps.paths.fishHome)
  const configured: Record<string, readonly string[]> = {}
  for (const probe of probes) configured[probe.id] = probe.requiredEventsPresent
  const health = checkHostHealth(deps.paths.dataDir, specs, configured, now)
  const dir = heartbeatDir(deps.paths.dataDir)
  const caveat =
    '\n注意：本会话自身的工具调用也会触发 hook 并刷新心跳——dsh 侧的"新鲜"不能单独当作' +
    '"用户正在用"的证据；判活靠"配置有 + 心跳有 + 未陈旧"三者同时成立。'
  return `${renderHostHealth(health, dir)}${caveat}`
}

/** `dry-run`：安装预览（只输出 diff）。 */
export function renderHostsDryRunCommand(deps: CommandDeps): string {
  const specs = hostsFor(deps)
  const plan = runHooksDryRun(deps.paths.fishHome, {
    packageRoot: deps.paths.packageRoot,
    hostConfigPaths: specs.map((spec) => spec.configPath),
  })
  return renderHostsDryRun(plan)
}

/**
 * `/fish hosts …` 的入口（纯函数，便于单测与判据脚本直接调）。
 * @param rest 子命令之后的 token（第一个是 `probe|health|dry-run`）
 */
export function handleHostsCommand(rest: readonly string[], flags: HostsFlags, deps: CommandDeps): CommandResult {
  const sub = (rest[0] ?? '').toLowerCase()
  try {
    if (sub === '' || sub === 'help' || flags.help === true) {
      return { kind: 'success', text: HOSTS_USAGE }
    }
    assertFlags(flags, sub)
    if (sub === 'probe') return withTruncation(renderHostsProbe(deps), 'hosts-probe', deps)
    if (sub === 'health') return withTruncation(renderHostsHealth(deps), 'hosts-health', deps)
    if (sub === 'dry-run' || sub === 'dryrun') return withTruncation(renderHostsDryRunCommand(deps), 'hosts-dry-run', deps)
    return {
      kind: 'error',
      text: `未知的 hosts 子命令：${sub}（可用 ${HOSTS_SUBCOMMANDS.join(' / ')}）\n\n${HOSTS_USAGE}`,
    }
  } catch (error) {
    const fishError = toFishError(error)
    deps.logger.error('hosts 子命令失败', { sub, code: fishError.code })
    return { kind: 'error', text: fishError.format() }
  }
}

/** 统一走命令面的截断（超 60 行落 `.tmp/` 并回显路径）。 */
function withTruncation(text: string, label: string, deps: CommandDeps): CommandResult {
  const { text: finalText } = finalizeOutput(text, label, deps.paths.packageRoot)
  return { kind: 'success', text: finalText }
}
