/**
 * dsh-fish 的错误模型：**每条错误都带可执行的下一步**。
 *
 * 为什么错误类型要单独成文件：影子期的门禁错误（契约不匹配 / schema 漂移）必须
 * 响亮失败并给出修复命令，而"响亮"只有两种实现方式——抛错，或者返回错误码。
 * 宿主插件加载失败是响亮的（事件循环外的 throw），所以这里统一用 throw，并且
 * 让文案本身携带 `hint`，避免上层再拼一遍提示（拼两遍必然漂）。
 *
 * @module dsh-fish/core/errors
 */

/** 错误分类：调用方据此决定"该重试 / 该修环境 / 该改配置"。 */
export type FishErrorCode =
  | 'CONTRACT_MISMATCH'
  | 'CONTRACT_INVALID'
  | 'SPEC_MISSING'
  | 'SPEC_INVALID'
  | 'DB_UNAVAILABLE'
  | 'DB_SCHEMA_DRIFT'
  | 'HOST_VERSION_UNSUPPORTED'
  | 'SHADOW_READ_ONLY'
  | 'SHADOW_UNSUPPORTED'
  | 'COMMAND_USAGE'
  | 'INTERNAL'

/** 构造参数：`hint` 是给人看的一步修复动作，`details` 是结构化证据。 */
export interface FishErrorOptions {
  /** 一步就能执行的修复动作（命令 / 文件），例如 `fish migrate`。 */
  readonly hint?: string
  /** 结构化证据（差异清单、路径、版本号）；不可含凭证。 */
  readonly details?: unknown
  /** 原始错误（保留栈）。 */
  readonly cause?: unknown
}

/** dsh-fish 的全部可预期错误。 */
export class FishError extends Error {
  readonly code: FishErrorCode
  readonly hint: string | undefined
  readonly details: unknown

  constructor(code: FishErrorCode, message: string, options: FishErrorOptions = {}) {
    super(message, options.cause === undefined ? undefined : { cause: options.cause })
    this.name = 'FishError'
    this.code = code
    this.hint = options.hint
    this.details = options.details
  }

  /** 供日志/命令回显使用：不含凭证，可直接序列化。 */
  toJSON(): Record<string, unknown> {
    return {
      name: this.name,
      code: this.code,
      message: this.message,
      hint: this.hint,
      details: this.details,
    }
  }

  /** 面向人（斜杠命令回显）的一行/两行文本。 */
  format(): string {
    return this.hint ? `${this.code}: ${this.message}\n下一步：${this.hint}` : `${this.code}: ${this.message}`
  }
}

/** 契约门禁专用：版本 / 指纹 / 宿主版本不匹配。 */
export class ContractMismatchError extends FishError {
  constructor(message: string, options: FishErrorOptions = {}) {
    super('CONTRACT_MISMATCH', message, options)
    this.name = 'ContractMismatchError'
  }
}

/** spec 层问题（缺文件、JSON 不合法、必需字段缺失、自校验失败）。 */
export class SpecError extends FishError {
  constructor(code: Extract<FishErrorCode, 'SPEC_MISSING' | 'SPEC_INVALID'>, message: string, options: FishErrorOptions = {}) {
    super(code, message, options)
    this.name = 'SpecError'
  }
}

/** 数据库层问题（打不开、schema 漂移、只读约束被违反）。 */
export class DatabaseError extends FishError {
  constructor(code: Extract<FishErrorCode, 'DB_UNAVAILABLE' | 'DB_SCHEMA_DRIFT' | 'SHADOW_READ_ONLY'>, message: string, options: FishErrorOptions = {}) {
    super(code, message, options)
    this.name = 'DatabaseError'
  }
}

/**
 * 把任意 `unknown` 收敛成 `FishError`。
 * 为什么需要：catch 到的可能是字符串、DOMException、SQLite 的原生错误；不收敛的话
 * 上层永远只能 `String(err)`，而那会丢掉 code（=丢掉"该怎么修"）。
 */
export function toFishError(error: unknown): FishError {
  if (error instanceof FishError) return error
  if (error instanceof Error) {
    return new FishError('INTERNAL', error.message, { cause: error })
  }
  return new FishError('INTERNAL', String(error))
}
