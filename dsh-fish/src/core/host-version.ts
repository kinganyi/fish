/**
 * 宿主版本探测：契约门禁三要素之一（spec 版本 / schema 指纹 / 宿主版本）。
 *
 * 为什么"探测不到"不直接判死：插件可能被装在没有 `@deepseek-ai/dsh` 依赖的
 * 独立进程里（例如验收脚本）。此时唯一诚实的结果是 `null`，由调用方按配置决定
 * 「未知即放行」（默认，宽松）还是「未知即拒绝」（`strictHostVersion: true`，严）。
 * 这两档的区别必须**可见**——所以门禁结果里永远带 `hostVersionSource`。
 *
 * @module dsh-fish/core/host-version
 */

import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'

/** 宿主版本来源：审计时要能回答"这个版本号是怎么知道的"。 */
export type HostVersionSource = 'env' | 'package' | 'unknown'

/** 探测结果。 */
export interface HostVersionInfo {
  /** 形如 `0.2.0-rc.2`；未知时为 `null`。 */
  readonly version: string | null
  readonly source: HostVersionSource
  /** 已知时给出解析到的路径，便于排障（不含凭证）。 */
  readonly resolvedFrom?: string
}

const ENV_KEYS = ['DSH_HOST_VERSION', 'DSH_DESKTOP_VERSION'] as const

/** 探测宿主版本（env 优先，其次解析 `@deepseek-ai/dsh` 的 package.json）。 */
export function detectHostVersion(): HostVersionInfo {
  for (const key of ENV_KEYS) {
    const raw = process.env[key]
    if (typeof raw === 'string' && raw.trim() !== '') {
      return { version: raw.trim(), source: 'env', resolvedFrom: key }
    }
  }
  try {
    const require = createRequire(import.meta.url)
    const manifestPath = require.resolve('@deepseek-ai/dsh/package.json')
    const parsed: unknown = JSON.parse(readFileSync(manifestPath, 'utf8'))
    if (typeof parsed === 'object' && parsed !== null) {
      const version = (parsed as { version?: unknown }).version
      if (typeof version === 'string' && version.trim() !== '') {
        return { version: version.trim(), source: 'package', resolvedFrom: manifestPath }
      }
    }
  } catch {
    // 解析不到（未安装 / 被外置）——如实返回 unknown。
  }
  return { version: null, source: 'unknown' }
}
