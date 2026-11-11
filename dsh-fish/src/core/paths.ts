/**
 * 路径解析：**一处决定"库在哪、spec 在哪"**。
 *
 * 为什么不把路径散在各模块：影子期最容易出的事故是"读到了另一个库"（工作副本 / 备份 /
 * 语料库），而这类事故的表现是"数据看起来少了几条"，不报错。所以路径只有这一个来源，
 * 且默认值必须能自证（`specDir` 必须含 `contract.json`，否则宁可抛错）。
 *
 * 优先级：显式配置 > 环境变量 > 包内相对位置。
 *
 * @module dsh-fish/core/paths
 */

import { existsSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { FishError } from './errors.ts'

/** 解析结果：所有绝对路径，且都经过存在性校验。 */
export interface FishPaths {
  /** 包根目录（含 package.json / spec / lib）。 */
  readonly packageRoot: string
  /** 契约层目录（含 contract.json）。 */
  readonly specDir: string
  /** fish 家目录（含 data/ 与 blobs/）；影子期默认指向 `p0/`。 */
  readonly fishHome: string
  /** fish 数据目录（含 index.sqlite）。 */
  readonly dataDir: string
  /** 主库文件（影子期只以 `mode=ro` 打开）。 */
  readonly dbPath: string
}

/** 调用方覆盖项：空串/`undefined` 一律视为"未指定"。 */
export interface FishPathOverrides {
  readonly specDir?: string
  readonly fishHome?: string
  readonly dbPath?: string
}

const PACKAGE_NAME = 'dsh-fish'

/**
 * 从当前模块向上找包根（含 `package.json` 且 `name === 'dsh-fish'`）。
 * 同时适用于 tsx 直跑 `src/` 与构建后跑 `lib/` 两种布局——靠"向上找"而不是"数 ../../"，
 * 因为数层级这种方式在加一层目录后必然静默指错。
 */
export function findPackageRoot(startUrl: string = import.meta.url): string {
  let dir = path.dirname(fileURLToPath(startUrl))
  for (let i = 0; i < 8; i += 1) {
    const manifest = path.join(dir, 'package.json')
    if (existsSync(manifest)) {
      try {
        const parsed: unknown = JSON.parse(readFileSync(manifest, 'utf8'))
        if (typeof parsed === 'object' && parsed !== null && (parsed as { name?: unknown }).name === PACKAGE_NAME) {
          return dir
        }
      } catch {
        // package.json 存在但不可解析：继续向上，不在这里报错（上层会有更准确的错误）。
      }
    }
    const parent = path.dirname(dir)
    if (parent === dir) break
    dir = parent
  }
  throw new FishError('SPEC_INVALID', `找不到 ${PACKAGE_NAME} 的包根目录`, {
    hint: '从包内启动插件，或显式设置 specDir / fishHome 配置项',
    details: { startUrl },
  })
}

/** 环境变量覆盖（显式配置优先于它）。 */
function fromEnv(name: string): string {
  const raw = process.env[name]
  return typeof raw === 'string' && raw.trim() !== '' ? path.resolve(raw.trim()) : ''
}

/**
 * 解析并校验全部路径。
 * @throws FishError `SPEC_MISSING` —— specDir 里没有 `contract.json`
 */
export function resolvePaths(overrides: FishPathOverrides = {}, startUrl: string = import.meta.url): FishPaths {
  const packageRoot = findPackageRoot(startUrl)

  const specDir = firstNonEmpty([
    overrides.specDir,
    fromEnv('DSH_FISH_SPEC_DIR'),
    path.join(packageRoot, 'spec'),
  ])
  if (!existsSync(path.join(specDir, 'contract.json'))) {
    throw new FishError('SPEC_MISSING', `契约层缺少 contract.json：${specDir}`, {
      hint: '先跑 `node scripts/gen-spec.mjs` 生成 spec/（生成脚本会校验产物齐全）',
      details: { specDir },
    })
  }

  // 默认 fishHome = 包根的兄弟目录 `p0`（本仓布局：大肥鱼生命系统/{p0,dsh-fish}）。
  const fishHome = firstNonEmpty([
    overrides.fishHome,
    fromEnv('FISH_HOME'),
    path.resolve(packageRoot, '..', 'p0'),
  ])
  const dataDir = path.join(fishHome, 'data')
  const dbPath = firstNonEmpty([overrides.dbPath, fromEnv('FISH_DB'), path.join(dataDir, 'index.sqlite')])

  if (!existsSync(dbPath)) {
    throw new FishError('DB_UNAVAILABLE', `找不到 fish 主库：${dbPath}`, {
      hint: '显式设置 dbPath / FISH_DB，或确认 fishHome 指向含 data/index.sqlite 的目录',
      details: { dbPath, fishHome },
    })
  }

  return { packageRoot, specDir, fishHome, dataDir, dbPath }
}

/** 取第一个非空字符串（`trim()` 后非空），返回绝对路径。 */
function firstNonEmpty(candidates: readonly (string | undefined)[]): string {
  for (const candidate of candidates) {
    if (typeof candidate === 'string' && candidate.trim() !== '') return path.resolve(candidate.trim())
  }
  return ''
}
