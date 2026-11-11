/**
 * 宿主 hooks **心跳健康度**：把"配置里写着"与"真的触发过"分开。
 *
 * 事实源是生产树的 `fish_activity.heartbeat()`：每 `(host, event)` 一个文件，
 * 路径 `<data>/_audit/hooks/heartbeat-<host>-<event>.json`，内容形如
 * `{"ts":"2026-10-27T18:30:42+08:00","ts_utc":"…","event":"PostToolUse","host":"dsh",…}`。
 *
 * ## 为什么必须看心跳而不是只看配置
 *
 * hooks 是"启动读一次"的：配置写错了**不会报错**，只会"某个事件从此不再触发"，
 * 症状是"某个功能悄悄没了"。而两种失败在配置里长得一模一样：
 *   · 事件不支持 → 宿主忽略该键，其余照常；
 *   · matcher 非法 → **整份配置被拒**，四个事件一个都不注册。
 * 所以判活只有一条路：证明 hook **真的被执行过**（时间戳出现，且是真实触发的形态）。
 *
 * ## 一条纪律：不信 mtime，看内容
 *
 * 生产实现自己写明了「看内容，不看 mtime」：`{"bad_payload": true}` 那种行是手工演练
 * 喂坏 JSON 留下的，不算"事件真的触发过"。本模块照同一口径判。
 *
 * @module dsh-fish/hosts/health
 */

import { existsSync, readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'

import { REQUIRED_EVENTS, type HostSpec } from './registry.ts'

/** 心跳目录（相对 fish 数据目录）：与 `fish_activity.HEARTBEAT_RELDIR` 同一路径。 */
export const HEARTBEAT_RELDIR = ['_audit', 'hooks'] as const

/** 陈旧阈值：2 小时（与 `03-调研` §5「hooks 活性 4/4 心跳 mtime ≤2h」同一口径）。 */
export const HEARTBEAT_STALE_MS = 2 * 60 * 60 * 1000

/** 一条心跳的读数。 */
export interface HeartbeatReading {
  readonly host: string
  readonly event: string
  readonly filePath: string
  readonly exists: boolean
  /** `ts_utc`（缺失时退化为 `ts`）。 */
  readonly ts: string | null
  /** 距今毫秒数（解析不出来给 `null`）。 */
  readonly ageMs: number | null
  readonly stale: boolean
  /** 是否只是"手工演练喂坏 JSON"留下的标记（不算真实触发）。 */
  readonly badPayload: boolean
  /** 心跳里带的**其他**事实键（session 只是 8 位哈希，不含原文）。 */
  readonly facts: Readonly<Record<string, unknown>>
  /** 读文件/解析失败的原因。 */
  readonly problem: string | null
}

/** 一次健康检查的结果。 */
export interface HostHealthResult {
  readonly asOf: string
  readonly staleThresholdMs: number
  readonly heartbeats: readonly HeartbeatReading[]
  readonly alerts: readonly string[]
  /** 参与判活的事件总数（宿主数 × 4）。 */
  readonly expectedCount: number
  readonly freshCount: number
}

/** 心跳目录的绝对路径。 */
export function heartbeatDir(dataDir: string): string {
  return path.join(dataDir, ...HEARTBEAT_RELDIR)
}

/** 心跳文件名（与 `fish_activity.heartbeat()` 一致）。 */
export function heartbeatFileName(host: string, event: string): string {
  return `heartbeat-${host}-${event}.json`
}

/** 解析 ISO 时间戳；失败返回 `null`。 */
function parseIso(raw: unknown): number | null {
  if (typeof raw !== 'string' || raw.trim() === '') return null
  const ms = Date.parse(raw.trim())
  return Number.isFinite(ms) ? ms : null
}

/** 读一条心跳（文件不存在也给一条读数——"缺失"必须能被看见，不能只是数组里少一项）。 */
export function readHeartbeat(dataDir: string, host: string, event: string, now: number): HeartbeatReading {
  const base = {
    host,
    event,
    filePath: path.join(heartbeatDir(dataDir), heartbeatFileName(host, event)),
  }
  if (!existsSync(base.filePath)) {
    return { ...base, exists: false, ts: null, ageMs: null, stale: true, badPayload: false, facts: {}, problem: null }
  }
  let payload: Record<string, unknown>
  try {
    payload = JSON.parse(readFileSync(base.filePath, 'utf8').replace(/^\uFEFF/, '')) as Record<string, unknown>
  } catch (error) {
    return {
      ...base,
      exists: true,
      ts: null,
      ageMs: null,
      stale: true,
      badPayload: false,
      facts: {},
      problem: `心跳文件不是合法 JSON：${error instanceof Error ? error.message : String(error)}`,
    }
  }
  const stamp = parseIso(payload.ts_utc) ?? parseIso(payload.ts)
  const ageMs = stamp === null ? null : Math.max(0, now - stamp)
  const badPayload = payload.bad_payload === true
  const facts: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(payload)) {
    if (key === 'ts' || key === 'ts_utc' || key === 'event' || key === 'host') continue
    facts[key] = value
  }
  return {
    ...base,
    exists: true,
    ts: typeof payload.ts_utc === 'string' ? payload.ts_utc : typeof payload.ts === 'string' ? payload.ts : null,
    ageMs,
    // 坏 payload 的标记不算"活着"：它证明的是"有人手工喂过一次"。
    stale: ageMs === null || ageMs > HEARTBEAT_STALE_MS || badPayload,
    badPayload,
    facts,
    problem: null,
  }
}

/** 人类可读的时长。 */
export function formatAge(ageMs: number | null): string {
  if (ageMs === null) return '未知'
  const minutes = Math.round(ageMs / 60000)
  if (minutes < 60) return `${minutes} 分钟`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  if (hours < 48) return `${hours} 小时${rest === 0 ? '' : ` ${rest} 分`}`
  return `${Math.floor(hours / 24)} 天`
}

/**
 * 判活：每宿主 × 4 事件各一条读数，并对三类矛盾给告警。
 *
 * @param dataDir fish 数据目录（含 `_audit/hooks/`）
 * @param specs 宿主登记项
 * @param configured `host → 配置里存在的事件名`（来自 `probe.ts` 的读数）
 * @param now 当前时刻（毫秒；测试可注入）
 */
export function checkHostHealth(
  dataDir: string,
  specs: readonly HostSpec[],
  configured: Readonly<Record<string, readonly string[]>>,
  now: number,
): HostHealthResult {
  const heartbeats: HeartbeatReading[] = []
  const alerts: string[] = []
  for (const spec of specs) {
    const declared = configured[spec.id] ?? []
    for (const event of REQUIRED_EVENTS) {
      const reading = readHeartbeat(dataDir, spec.id, event, now)
      heartbeats.push(reading)
      const configHas = declared.includes(event)
      if (configHas && !reading.exists) {
        alerts.push(
          `[${spec.id}/${event}] **配置里有这个事件，但心跳文件从未出现**——hook 一次都没跑过` +
            '（排查顺序：宿主是否重启 / matcher 方言是否非法 / 脚本路径是否可执行）',
        )
        continue
      }
      if (!configHas && reading.exists) {
        alerts.push(`[${spec.id}/${event}] 心跳存在但配置里没有该事件——hooks 配置被改过？`)
        continue
      }
      if (configHas && reading.badPayload) {
        alerts.push(`[${spec.id}/${event}] 心跳只有 \`bad_payload\` 标记（手工演练），不算真实触发`)
        continue
      }
      if (configHas && reading.stale) {
        alerts.push(
          `[${spec.id}/${event}] 心跳陈旧：${formatAge(reading.ageMs)} 前（阈值 ${Math.round(HEARTBEAT_STALE_MS / 60000)} 分钟）` +
            `——最后一条 ${reading.ts ?? '时间戳读不出来'}`,
        )
      }
      if (reading.problem !== null) alerts.push(`[${spec.id}/${event}] ${reading.problem}`)
    }
  }
  const freshCount = heartbeats.filter((reading) => reading.exists && !reading.stale && !reading.badPayload).length
  return {
    asOf: new Date(now).toISOString(),
    staleThresholdMs: HEARTBEAT_STALE_MS,
    heartbeats,
    alerts,
    expectedCount: specs.length * REQUIRED_EVENTS.length,
    freshCount,
  }
}

/** 把健康度读数渲染成人读文本。 */
export function renderHostHealth(result: HostHealthResult, dir: string): string {
  const lines: string[] = [
    '# 跨宿主 hooks 心跳健康度（只读）',
    `心跳目录：${dir}`,
    `判活口径：每条 ≤ ${Math.round(result.staleThresholdMs / 60000)} 分钟算新鲜；\`bad_payload\` 标记不算真实触发`,
    `读数时刻：${result.asOf}（新鲜 ${result.freshCount}/${result.expectedCount}）`,
    '',
    '| host | 事件 | 心跳 | 距今 | 判定 | 事实键 |',
    '|---|---|---|---|---|---|',
  ]
  for (const reading of result.heartbeats) {
    const verdict = !reading.exists
      ? '**缺失**'
      : reading.badPayload
        ? '演练标记'
        : reading.stale
          ? '陈旧'
          : '新鲜'
    const facts = Object.entries(reading.facts)
      .map(([key, value]) => `${key}=${typeof value === 'object' ? JSON.stringify(value) : String(value)}`)
      .join(' ')
    lines.push(
      `| ${reading.host} | ${reading.event} | ${reading.exists ? '有' : '无'} | ${formatAge(reading.ageMs)} | ${verdict} | ${facts || '—'} |`,
    )
  }
  lines.push('')
  if (result.alerts.length === 0) {
    lines.push('结论：✓ 配置与心跳一致，且全部新鲜')
  } else {
    lines.push(`结论：✗ ${result.alerts.length} 条告警`)
    for (const alert of result.alerts) lines.push(`  · ${alert}`)
  }
  return lines.join('\n')
}

/** 列心跳目录里的全部文件（排障用：能看到 `warn-pending-*` 之类的旁证）。 */
export function listHeartbeatDir(dataDir: string): readonly string[] {
  const dir = heartbeatDir(dataDir)
  if (!existsSync(dir)) return []
  try {
    return readdirSync(dir).sort()
  } catch {
    return []
  }
}
