/**
 * 结构化日志：**一条日志一个 JSON 对象**，且凭证/正文不进日志。
 *
 * 为什么自己包一层而不是直接用 `ctx.logger`：插件在三种上下文里跑（宿主进程、
 * `tsx` 直跑的脚本、纯 node 的验收脚本），三者可用的 logger 不一样。包一层之后
 * 只有一处"降级到 console"，而不是每个模块各降一次（各降一次必然有人忘）。
 *
 * 纪律：`fields` 里禁止出现 token 明文、记忆正文、绝对路径以外的大块文本。
 *
 * @module dsh-fish/core/logger
 */

import type { Context } from '@deepseek-ai/cordis'

/** 日志级别（从吵到静）。 */
export type LogLevel = 'debug' | 'info' | 'warn' | 'error'

const LEVEL_ORDER: Readonly<Record<LogLevel, number>> = { debug: 10, info: 20, warn: 30, error: 40 }

/** 结构化字段：值必须是可 JSON 序列化的标量/数组/对象。 */
export type LogFields = Readonly<Record<string, unknown>>

/** 插件内统一使用的日志器。 */
export interface FishLogger {
  debug(message: string, fields?: LogFields): void
  info(message: string, fields?: LogFields): void
  warn(message: string, fields?: LogFields): void
  error(message: string, fields?: LogFields): void
  /** 派生一个带 scope 的子日志器（scope 会写进每条日志的 `scope` 字段）。 */
  child(scope: string): FishLogger
}

/** 宿主 logger 的最小形状（不同版本可能有差异，故只依赖这四个方法）。 */
interface HostLogger {
  debug(...args: unknown[]): void
  info(...args: unknown[]): void
  warn(...args: unknown[]): void
  error(...args: unknown[]): void
}

function isHostLogger(value: unknown): value is HostLogger {
  if (typeof value !== 'object' || value === null) return false
  const candidate = value as Record<string, unknown>
  return typeof candidate.info === 'function' && typeof candidate.error === 'function'
}

/** 从 cordis 上下文尽力取出宿主 logger；取不到返回 `null`（调用方降级到 console）。 */
function pickHostLogger(ctx: Context | null): HostLogger | null {
  if (ctx === null) return null
  const candidate = (ctx as unknown as { logger?: unknown }).logger
  // cordis 的 `ctx.logger` 既可能是实例，也可能是"传名字返回实例"的函数，两种都认。
  if (typeof candidate === 'function') {
    try {
      const produced = (candidate as (name?: string) => unknown)('dsh-fish')
      if (isHostLogger(produced)) return produced
    } catch {
      /* 取不到就当没有，下一步再判断 candidate 自身是否是 logger。 */
    }
    return null
  }
  return isHostLogger(candidate) ? candidate : null
}

/** 创建日志器。`minLevel` 之下的一律丢弃（默认 `info`）。 */
export function createLogger(ctx: Context | null, options: { minLevel?: LogLevel; scope?: string } = {}): FishLogger {
  const minLevel = options.minLevel ?? 'info'
  const host = pickHostLogger(ctx)
  const scope = options.scope ?? 'dsh-fish'
  return makeLogger(host, minLevel, scope)
}

function makeLogger(host: HostLogger | null, minLevel: LogLevel, scope: string): FishLogger {
  const emit = (level: LogLevel, message: string, fields?: LogFields): void => {
    if (LEVEL_ORDER[level] < LEVEL_ORDER[minLevel]) return
    const record: Record<string, unknown> = { scope, message }
    if (fields !== undefined) record.fields = fields
    if (host !== null) {
      host[level](record)
      return
    }
    // 降级路径：单行 JSON，便于脚本用 grep 断言。
    const line = JSON.stringify({ level, ...record })
    if (level === 'error' || level === 'warn') console.error(line)
    else console.log(line)
  }
  return {
    debug: (message, fields) => emit('debug', message, fields),
    info: (message, fields) => emit('info', message, fields),
    warn: (message, fields) => emit('warn', message, fields),
    error: (message, fields) => emit('error', message, fields),
    child: (subScope: string) => makeLogger(host, minLevel, `${scope}/${subScope}`),
  }
}

/** 静默日志器（测试用：断言输出时不该被日志干扰，连 error 一起吞掉）。 */
export const SILENT_LOGGER: FishLogger = {
  debug: () => undefined,
  info: () => undefined,
  warn: () => undefined,
  error: () => undefined,
  child: () => SILENT_LOGGER,
}
