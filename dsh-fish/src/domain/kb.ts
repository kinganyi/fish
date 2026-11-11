/**
 * 知识库只读面（`p0/fish_kb.py` 的 tree / open / search / stats / vault list）。
 *
 * ## 影子期的三条硬边界（与生产语义逐条对齐，不是"简化版"）
 *
 *   1. **只读不索引**：知识库内容永不进 `memory_fts` / `recall` / 注入包；`search` 是
 *      "按需扫描"，回执必须写明"未建索引"——它不是第二个 recall。
 *   2. **忽略规则是硬边界**：`.obsidian/**`、`.trash/**`、`90-模板/**` 是**强制项**，
 *      判定层每次合并（`effectiveGlobs`），删不掉。判定对象是"路径本身 + 每一级祖先目录"，
 *      且先做逐段归一（大小写 + 尾随点/空格）再匹配——Windows 会吃掉组件尾随的空格，
 *      不归一会让 `.obsidian /app.json` 绕过禁读。
 *   3. **敏感面默认拒读**：命中内置名单（凭证/密钥/.env…）只给"文件名 + 大小 + 建议"，
 *      正文要显式二次授权（`allowSensitive`）。判定同时看**字面串**与**解析后落点**。
 *
 * ## 与生产的一处**有意差异**（影形期纪律，登记在 NOTES-T05 §5）
 *
 * 生产 `kb_tree` / `kb_open` / `kb_search` 每次读都会往 `kb_access` 追一条审计
 * （`p0/fish_kb.py:1643`、`:1826`、`:2083`，词表见 `KB_ACTIONS`）。影子期
 * **一个字节都不写**，所以本模块**没有** `kbAccessLog` 的实现——不是漏了，是不能有。
 * 代价：DSH 里的读取不会出现在"未读/读次数"里；收益：读命令永远不可能改库。
 *
 * ## 为什么不复用 node:sqlite 的 `readOnly` 之外的东西
 *
 * `kb_access` 的查询走调用方传进来的只读连接（`core/db.ts` 的 `openReadOnly`，
 * 带 `query_only=1`）；文件读取一律 `readFileSync`。整个模块没有写 API。
 *
 * @module dsh-fish/domain/kb
 */

import { createHash } from 'node:crypto'
import { existsSync, lstatSync, readdirSync, readFileSync, realpathSync, statSync } from 'node:fs'
import path from 'node:path'
import type { DatabaseSync } from 'node:sqlite'

import { isoLocal } from '../core/time.ts'
import { pyText } from '../core/pytext.ts'
import { codePointLength, countTokens, sliceCodePoints } from './token.ts'

// ---------------------------------------------------------------- 常量（逐条对齐 fish_kb.py）

/** 强制忽略项（`KB_MANDATORY_DENY`）：判定层每次合并，用户删不掉。 */
export const KB_MANDATORY_DENY = ['.obsidian/**', '.trash/**', '90-模板/**'] as const

/** 敏感名单（`KB_SENSITIVE_PATTERNS`）：命中即"要显式二次授权"才能读正文。 */
export const KB_SENSITIVE_PATTERNS = [
  '*凭证*',
  '*credential*',
  '*secret*',
  '*密码*',
  '*.pem',
  '*.key',
  'id_rsa*',
  '.env*',
] as const

/** 上表里唯一需要负向断言的那条（`密码学` 是正常笔记）。 */
const KB_PASSWORD_PATTERN = '*密码*'
const KB_PASSWORD_RE = /密码(?!学)/u

/** 媒体扩展名黑名单（改名成 `.md` 也拦得住——魔数双判的第二重）。 */
export const KB_MEDIA_EXTS: ReadonlySet<string> = new Set([
  '.mp4', '.mov', '.mkv', '.avi', '.webm', '.png', '.jpg', '.jpeg', '.webp',
  '.gif', '.bmp', '.tif', '.tiff', '.svgz', '.mp3', '.wav', '.flac', '.m4a',
  '.ogg', '.zip', '.7z', '.rar', '.gz', '.tar', '.pdf', '.docx', '.xlsx',
  '.pptx', '.bin', '.exe', '.dll', '.so', '.dylib', '.iso',
])

/** 首 8 KB 的魔数表（`(签名, 偏移, 名字)`）。 */
const KB_MAGIC_SIGNATURES: readonly { readonly signature: Buffer; readonly offset: number; readonly name: string }[] = [
  { signature: Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), offset: 0, name: 'png' },
  { signature: Buffer.from([0xff, 0xd8, 0xff]), offset: 0, name: 'jpeg' },
  { signature: Buffer.from('GIF8', 'ascii'), offset: 0, name: 'gif' },
  { signature: Buffer.from('%PDF', 'ascii'), offset: 0, name: 'pdf' },
  { signature: Buffer.from([0x50, 0x4b, 0x03, 0x04]), offset: 0, name: 'zip' },
  { signature: Buffer.from([0x37, 0x7a, 0xbc, 0xaf, 0x27, 0x1c]), offset: 0, name: '7z' },
  { signature: Buffer.from([0x52, 0x61, 0x72, 0x21, 0x1a, 0x07]), offset: 0, name: 'rar' },
  { signature: Buffer.from('ID3', 'ascii'), offset: 0, name: 'mp3' },
  { signature: Buffer.from('fLaC', 'ascii'), offset: 0, name: 'flac' },
  { signature: Buffer.from('OggS', 'ascii'), offset: 0, name: 'ogg' },
  { signature: Buffer.from([0x7f, 0x45, 0x4c, 0x46]), offset: 0, name: 'elf' },
  { signature: Buffer.from('MZ', 'ascii'), offset: 0, name: 'exe' },
  { signature: Buffer.from('SQLite format 3\u0000', 'latin1'), offset: 0, name: 'sqlite' },
  { signature: Buffer.from('WEBP', 'ascii'), offset: 8, name: 'webp' },
  { signature: Buffer.from('ftyp', 'ascii'), offset: 4, name: 'mp4' },
]

/** 单文件上限（Q11：2 MiB，可按 vault 配）。 */
export const KB_DEFAULT_MAX_OPEN_BYTES = 2097152
/** 检索时单文件上限（超过跳过并计数）。 */
export const KB_SEARCH_MAX_FILE_BYTES = 2097152
/** tree 每个目录最多列 20 项。 */
export const KB_MAX_ITEMS_PER_DIR = 20
/** tree 深度上限（更深请显式给 path）。 */
export const KB_TREE_MAX_DEPTH = 4
/** search 结果条数上限。 */
export const KB_MAX_LIMIT = 30

/** tree 的深度口径说明（**权威定义在这里**，与生产同一句话）。 */
const KB_TREE_DEPTH_RULE =
  '> 深度口径（唯一权威）：depth=N = **显示到第 N 层目录**，起点为第 0 层；' +
  '每个被显示的目录都列出它的直接 `.md` 文件' +
  '（相对库根的完整路径，可直接喂给 `kb open`）'

// ---------------------------------------------------------------- 错误

/** 知识库域错误（`code` 与生产 `KBError` 的词表逐字对齐，回执按生产格式渲染）。 */
export class KbError extends Error {
  readonly code: string
  readonly hint: string | undefined

  constructor(code: string, message: string, hint?: string) {
    super(message)
    this.name = 'KbError'
    this.code = code
    this.hint = hint
  }

  /** 与 Python `KBError.__str__` 同形（含"下一步"两行）。 */
  format(): string {
    return this.hint === undefined ? this.message : `${this.message}\n  下一步：${this.hint}`
  }
}

// ---------------------------------------------------------------- 行 / 结果类型

/** `kb_vault` 一行（`ignore_globs` 已解析成 list；K1：永不返回裸行）。 */
export interface KbVaultRow {
  readonly vaultId: string
  readonly name: string
  readonly path: string
  readonly mode: 'ro' | 'rw'
  readonly ignoreGlobs: readonly string[]
  readonly maxOpenBytes: number
  readonly isDefault: boolean
  readonly lastScanAt: string | null
  readonly lastScanFiles: number | null
  readonly lastScanNotes: number | null
  readonly lastScanBytes: number | null
  readonly note: string | null
  readonly status: string
}

/** `kb_vault_get` 的 SELECT 列序（与 `_KB_VAULT_COLS` 一致）。 */
const KB_VAULT_SELECT =
  'SELECT vault_id, name, path, path_key, mode, ignore_globs, max_open_bytes, is_default, ' +
  'last_scan_at, last_scan_files, last_scan_notes, last_scan_bytes, note, status, created_at, ' +
  'updated_at FROM kb_vault'

/** 一颗统计树的节点（`_kb_tree_dir` 的产物）。 */
export interface KbTreeNode {
  readonly rel: string
  readonly notes: number
  readonly attachments: number
  readonly files: number
  readonly bytes: number
  readonly hiddenDirs: number
  readonly partial: boolean
  readonly mtime: number
  readonly children: readonly KbTreeChild[]
  readonly more: number
  readonly dirs: readonly KbTreeNode[]
}

/** 目录项（文件与子目录共用一种形状，`isDir` 区分）。 */
export interface KbTreeChild {
  readonly rel: string
  readonly name: string
  readonly isDir: boolean
  readonly notes: number
  readonly attachments: number
  readonly bytes: number
  readonly mtime: number
  readonly notScanned: boolean
}

/** `kb_tree` 的返回（字段名与生产 `--json` 一致）。 */
export interface KbTreeResult {
  readonly vault: KbVaultBrief
  readonly asOf: string
  readonly root: string
  readonly depth: number
  readonly ignoreGlobs: readonly string[]
  readonly dirs: readonly KbTreeNode[]
  readonly summary: KbTreeNode | null
  readonly scan: KbScanInfo
  readonly text: string
  readonly chars: number
  readonly tokens: number
  readonly truncated: boolean
  readonly truncatedReasons: readonly string[]
}

/** vault 摘要（回执头用）。 */
export interface KbVaultBrief {
  readonly name: string
  readonly id: string
  readonly mode: string
  readonly path: string
}

/** 扫描读数（`scan`）。 */
export interface KbScanInfo {
  readonly files: number | null
  readonly bytes: number | null
  readonly ms: number
  readonly stale: boolean
  readonly partial: boolean
  readonly hiddenDirs: number
}

/** `kb_open` 的返回。 */
export interface KbOpenResult {
  readonly rel: string
  readonly abs: string
  readonly bytes: number
  readonly sha256: string
  readonly encoding: string
  readonly eol: 'lf' | 'crlf'
  readonly truncated: boolean
  readonly sensitive: boolean
  readonly shown: number
  readonly limit: number
  readonly charsShown: number
  readonly charsTotal: number
  readonly statements: readonly string[]
  readonly text: string
}

/** 一条检索命中。 */
export interface KbSearchHit {
  readonly rel: string
  readonly abs: string
  readonly occurrences: number
  readonly lines: readonly { readonly lineno: number; readonly text: string }[]
  readonly mtime: number
}

/** `kb_search` 的返回。 */
export interface KbSearchResult {
  readonly vault: KbVaultBrief
  readonly asOf: string
  readonly queryHash: string
  readonly qlen: number
  readonly limit: number
  readonly scanned: {
    readonly files: number
    readonly bytes: number
    readonly ms: number
    readonly skippedLarge: number
    readonly skippedBinary: number
    readonly skippedEncoding: number
    readonly skippedSensitive: number
  }
  readonly hits: readonly KbSearchHit[]
  readonly total: number
  readonly shown: number
  readonly text: string
  readonly chars: number
  readonly tokens: number
  readonly truncated: boolean
  readonly truncatedReasons: readonly string[]
}

/** 单库规模读数（`kb_stats.vaults[]`）。 */
export interface KbVaultStats {
  readonly name: string
  readonly vaultId: string
  readonly mode: string
  readonly path: string
  readonly files: number | null
  readonly notes: number | null
  readonly bytes: number | null
  readonly countsSource: 'live' | 'last_scan'
  readonly online: boolean
  readonly openedPaths: number
  readonly unreadNotes: number | null
  readonly reads: number
  readonly writes: number
  readonly rollbacks: number
  readonly lastScanAt: string | null
}

/** `kb_stats` 的返回。 */
export interface KbStatsResult {
  readonly asOf: string
  readonly vaults: readonly KbVaultStats[]
  readonly accessRows: number
  readonly recent: readonly {
    readonly id: number
    readonly ts: string
    readonly path: string
    readonly action: string
    readonly result: string
    readonly citizenId: string | null
  }[]
  readonly text: string
}

// ---------------------------------------------------------------- 小工具

/** `pyText` 在本模块内的再导出（回执渲染大量使用；口径定义在 `core/pytext.ts`）。 */
export { pyText }

/** Windows 上 `normcase` = 小写（其它平台不改）。 */
function normCase(text: string): string {
  return process.platform === 'win32' ? text.toLowerCase() : text
}

/** 忽略判定的单段归一：大小写 + 尾随点/空格（只用于判定，不改落点）。 */
export function judgeComponent(part: string): string {
  return normCase(String(part ?? '').replace(/[. ]+$/u, ''))
}

/**
 * Python `fnmatch.fnmatchcase` 的等价实现。
 *
 * `*` 匹配任意串（**包括 `/`**——生产就是拿 `fnmatch` 比整条路径的，不是 `pathlib.match`）；
 * `?` 匹配一个字符；`[seq]` 是字符类。大小写敏感（归一由调用方先做）。
 */
export function fnmatchCase(name: string, pattern: string): boolean {
  let source = ''
  for (let i = 0; i < pattern.length; i += 1) {
    const ch = pattern[i] as string
    if (ch === '*') {
      source += '.*'
    } else if (ch === '?') {
      source += '.'
    } else if (ch === '[') {
      let j = i + 1
      if (j < pattern.length && (pattern[j] === '!' || pattern[j] === '^')) j += 1
      if (j < pattern.length && pattern[j] === ']') j += 1
      while (j < pattern.length && pattern[j] !== ']') j += 1
      if (j >= pattern.length) {
        source += '\\['
      } else {
        const stuff = pattern.slice(i + 1, j)
        const body = stuff.startsWith('!') ? `^${stuff.slice(1)}` : stuff
        source += `[${body.replace(/\\/gu, '\\\\')}]`
        i = j
      }
    } else {
      source += ch.replace(/[.*+?^${}()|[\]\\]/gu, '\\$&')
    }
  }
  return new RegExp(`^${source}$`, 'u').test(name)
}

/** **生效**忽略规则 = 强制项 ∪ 行内规则（去重、保序）。 */
export function effectiveGlobs(globs: readonly string[] | null | undefined): string[] {
  const out: string[] = [...KB_MANDATORY_DENY]
  for (const raw of globs ?? []) {
    const text = String(raw ?? '')
    if (text !== '' && !out.includes(text)) out.push(text)
  }
  return out
}

/**
 * 命中忽略规则 → 返回**命中的那条规则**（没命中 → `null`）。
 *
 * 匹配对象 = "路径本身 + 每一级祖先目录（补 `/`）"：`.trash/**` 要能覆盖
 * `.trash/a/b.md`，"任意深度的 _media 目录"这类规则也要能覆盖；而 `.obsidian/**` 既要挡住里面的文件，
 * 也要挡住**这个目录本身**（否则 tree 会列出空目录节点）。
 */
export function kbIgnored(relPosix: string | null | undefined, globs: readonly string[]): string | null {
  const raw = String(relPosix ?? '').replace(/\\/gu, '/')
  const trimmed = raw.replace(/^\/+|\/+$/gu, '')
  if (trimmed === '') return null
  const parts = trimmed
    .split('/')
    .filter((part) => part !== '')
    .map((part) => judgeComponent(part))
  if (parts.length === 0) return null
  const rel = parts.join('/')
  const candidates: string[] = [rel, `${rel}/`]
  for (let i = 1; i < parts.length; i += 1) candidates.push(`${parts.slice(0, i).join('/')}/`)
  for (const pattern of effectiveGlobs(globs)) {
    const rawPattern = String(pattern ?? '')
    if (rawPattern === '') continue
    const patNorm = rawPattern
      .replace(/\\/gu, '/')
      .split('/')
      .filter((part) => part !== '')
      .map((part) => judgeComponent(part))
      .join('/')
    for (const candidate of candidates) {
      if (fnmatchCase(candidate, patNorm) || fnmatchCase(rel, patNorm)) return rawPattern
    }
  }
  return null
}

/** 路径命中内置敏感名单（**逐段**判，大小写不敏感）。 */
export function kbIsSensitiveName(rel: string | null | undefined): boolean {
  const parts = String(rel ?? '')
    .replace(/\\/gu, '/')
    .split('/')
    .filter((part) => part !== '' && part !== '.' && part !== '..')
  for (const part of parts) {
    const name = part.toLowerCase()
    for (const pattern of KB_SENSITIVE_PATTERNS) {
      if (pattern === KB_PASSWORD_PATTERN) continue
      if (fnmatchCase(name, pattern.toLowerCase())) return true
    }
    if (KB_PASSWORD_RE.test(name)) return true
  }
  return false
}

const KB_DEVICE_NAMES: ReadonlySet<string> = new Set([
  'con', 'prn', 'aux', 'nul',
  ...Array.from({ length: 9 }, (_v, i) => `com${i + 1}`),
  ...Array.from({ length: 9 }, (_v, i) => `lpt${i + 1}`),
])

/** `CON` / `NUL` / `COM1` / `CON.md` 全都是设备名（Windows 只看主干名）。 */
export function kbIsDeviceName(part: string): boolean {
  const stem = String(part ?? '')
    .split('.')[0]
    ?.trim()
    .toLowerCase()
  return KB_DEVICE_NAMES.has(stem ?? '')
}

/** 判断一个路径分量是不是链接/联接点（遍历面：不列、不下降）。 */
function isReparsePoint(absPath: string): boolean {
  try {
    return lstatSync(absPath).isSymbolicLink()
  } catch {
    return false
  }
}

/** 逐级走到 target，返回第一个 reparse point 的路径（没有 → `null`）。 */
function firstReparse(realRoot: string, target: string): string | null {
  const relative = path.relative(realRoot, target)
  if (relative === '') return null
  if (relative.startsWith('..') || path.isAbsolute(relative)) return String(target)
  const parts = relative.split(path.sep).filter((part) => part !== '')
  let current = realRoot
  for (const part of parts) {
    current = path.join(current, part)
    if (!existsSync(current)) return null
    if (isReparsePoint(current)) return current
  }
  return null
}

// ---------------------------------------------------------------- 路径解析（§5.1）

/** `kb_resolve_target` 的产物。 */
export interface KbTarget {
  readonly abs: string
  readonly rel: string
  readonly realRoot: string
  readonly resolvedRel: string
  readonly sensitive: boolean
}

/**
 * 把"vault 内相对路径"解析成绝对路径，途中做全部拒绝。
 *
 * 拒绝清单（判 `code` 不判文案）：`..` / 绝对 / 盘符 / UNC / `\\?\` / 冒号(ADS) /
 * 设备名 / NUL / 忽略目录 / 越界 / 链接 / 不存在。
 */
export function kbResolveTarget(vault: KbVaultRow, rel: string | null | undefined, forWrite = false): KbTarget {
  const root = vault.path
  const rawText = rel === null || rel === undefined ? '' : String(rel)
  if (rawText.includes('\u0000')) {
    throw new KbError('path_invalid', '路径含 NUL 字符（\\x00），拒绝处理', '路径里不可能有 NUL；检查调用方是不是把二进制/拼接串当路径了')
  }
  const raw = rawText.trim()
  const realRoot = realpathSync(root)
  if (raw === '') {
    if (forWrite) throw new KbError('path_invalid', '写路径不能为空', '给 vault 内相对路径，如 20-PPT/x.md')
    return { abs: realRoot, rel: '', realRoot, resolvedRel: '', sensitive: false }
  }
  if (raw.startsWith('/') || raw.startsWith('\\')) {
    if (raw.startsWith('\\\\?\\') || raw.startsWith('\\\\.\\')) {
      throw new KbError('path_invalid', `不接受设备路径（\\\\?\\ …）：${raw}`, '给 vault 内相对路径，用 / 分隔')
    }
    if (raw.startsWith('\\\\') || raw.startsWith('//')) {
      throw new KbError('path_invalid', `不接受 UNC 路径：${raw}`, '给 vault 内相对路径，用 / 分隔')
    }
    throw new KbError('path_invalid', `不接受绝对路径：${raw}`, '给 vault 内相对路径，用 / 分隔')
  }
  if (/^[A-Za-z]:/u.test(raw)) {
    throw new KbError('path_invalid', `不接受盘符：${raw}`, '给 vault 内相对路径，如 20-PPT/x.md')
  }
  const parts = raw
    .replace(/\\/gu, '/')
    .split('/')
    .filter((part) => part !== '' && part !== '.')
  if (parts.some((part) => part === '..')) {
    throw new KbError('path_invalid', `路径含 \`..\` 穿越：${raw}`, 'vault 内相对路径不允许向上跳')
  }
  for (const part of parts) {
    if (part.includes(':')) {
      throw new KbError(
        'path_invalid',
        `路径含冒号（Windows 备用数据流 ADS）：${part}`,
        '文件名里不能有 `:`；ADS 形态（x.md:hidden）一律拒绝',
      )
    }
    if (kbIsDeviceName(part)) {
      throw new KbError('path_invalid', `拒绝 Windows 设备名：${part}`, 'CON/NUL/COM1 这类名字指向设备，不是库里的文件')
    }
  }
  const relPosix = parts.join('/')
  if (relPosix === '') {
    throw new KbError('path_invalid', `路径归一后为空：${raw}`, '给 vault 内相对路径，如 20-PPT/x.md')
  }
  const hit = kbIgnored(relPosix, vault.ignoreGlobs)
  if (hit !== null) {
    throw new KbError(
      'ignored',
      `该路径按约定忽略：${relPosix}（规则 ${hit}）`,
      '换个路径；忽略规则用 `fish kb vault add --ignore` 配置',
    )
  }
  const lexical = path.join(realRoot, ...parts)
  const target = existsSync(lexical) ? realpathSync(lexical) : path.resolve(lexical)
  const relative = path.relative(realRoot, target)
  const inside = relative !== '' ? !relative.startsWith('..') && !path.isAbsolute(relative) : true
  if (!inside) {
    throw new KbError('outside_vault', `越出知识库根：${relPosix}`, '只允许 vault 内的相对路径')
  }
  const resolvedRel = relative.split(path.sep).join('/')
  const resolvedHit = kbIgnored(resolvedRel, vault.ignoreGlobs)
  if (resolvedHit !== null) {
    throw new KbError(
      'ignored',
      `该路径解析后落在忽略目录：${relPosix}` +
        (resolvedRel !== relPosix ? `（实际落点 ${resolvedRel}）` : '') +
        `（规则 ${resolvedHit}）`,
      '换个路径；忽略规则（含内置强制项）对解析后的真实落点同样生效',
    )
  }
  const sensitive = kbIsSensitiveName(resolvedRel) || kbIsSensitiveName(relPosix)
  const bad = firstReparse(realRoot, lexical)
  if (bad !== null) {
    throw new KbError('reparse_point', `路径含链接/联接点：${bad}（已拒绝）`, 'Obsidian vault 内不应有 junction；换成真实目录')
  }
  if (!existsSync(target)) {
    if (!forWrite) {
      throw new KbError('not_found', `知识库里没有：${relPosix}`, 'fish kb tree 看目录，或用完整相对路径')
    }
    const parent = path.dirname(lexical)
    if (parent !== realRoot && !existsSync(parent)) {
      throw new KbError('not_found', `父目录不存在：${parent}`, 'P0 只写已存在目录内（不自动建目录）')
    }
    const parentBad = firstReparse(realRoot, parent)
    if (parentBad !== null) {
      throw new KbError('reparse_point', `父目录链含链接/联接点：${parentBad}（已拒绝）`)
    }
  }
  return { abs: target, rel: relPosix, realRoot, resolvedRel, sensitive }
}

// ---------------------------------------------------------------- 类型 / 编码（§5.3）

/** `kb_detect_text` 的返回。 */
export interface KbTextDetection {
  readonly isText: boolean
  readonly encoding: string | null
  readonly media: boolean
  readonly reason: string | null
}

/** 判"这是不是可以读的文本"（扩展名 + 首 8 KB 魔数双判）。 */
export function kbDetectText(data: Buffer, ext = ''): KbTextDetection {
  const suffix = String(ext ?? '').toLowerCase()
  if (suffix !== '' && KB_MEDIA_EXTS.has(suffix)) {
    return { isText: false, encoding: null, media: true, reason: `extension:${suffix}` }
  }
  const head = data.subarray(0, 8192)
  for (const { signature, offset, name } of KB_MAGIC_SIGNATURES) {
    const slice = head.subarray(offset, offset + signature.length)
    if (slice.length === signature.length && slice.equals(signature)) {
      return { isText: false, encoding: null, media: true, reason: `magic:${name}` }
    }
  }
  if (head.includes(0)) {
    return { isText: false, encoding: null, media: true, reason: 'nul_byte' }
  }
  let encoding: string | null = null
  // BOM 按**字节**判，不按解码结果判：Node 的 `TextDecoder('utf-8')` 默认会**剥掉** BOM
  // （`ignoreBOM=false`），于是"解码后首字符是不是 U+FEFF"永远为假——照抄 Python 的
  // 那种写法会把 `utf-8-sig` 静默报成 `utf-8`（回执里的一格读数就错了）。
  const hasBom = data.length >= 3 && data[0] === 0xef && data[1] === 0xbb && data[2] === 0xbf
  try {
    new TextDecoder('utf-8', { fatal: true }).decode(data)
    encoding = hasBom ? 'utf-8-sig' : 'utf-8'
  } catch {
    // 与 Python 的第二档同效：BOM 先剥掉再严格解 UTF-8（BOM 本身是合法 UTF-8，第一档
    // 已经能解出 `utf-8-sig`，所以这一档只兜"BOM + 非 UTF-8"的怪文件）。
    try {
      const withoutBom = hasBom ? data.subarray(3) : data
      new TextDecoder('utf-8', { fatal: true }).decode(withoutBom)
      encoding = 'utf-8-sig'
    } catch {
      try {
        new TextDecoder('gb18030', { fatal: true }).decode(data)
        encoding = 'gb18030'
      } catch {
        encoding = null
      }
    }
  }
  if (encoding === null) {
    return { isText: false, encoding: null, media: false, reason: 'bad_encoding' }
  }
  const sample = new TextDecoder(encoding === 'utf-8-sig' ? 'utf-8' : encoding, { fatal: false }).decode(head)
  let controls = 0
  for (const ch of sample) {
    const code = ch.codePointAt(0) ?? 0
    if (code < 32 && ch !== '\t' && ch !== '\n' && ch !== '\r') controls += 1
  }
  if (sample !== '' && controls / codePointLength(sample) > 0.05) {
    return { isText: false, encoding: null, media: true, reason: 'binary_ratio' }
  }
  return { isText: true, encoding, media: false, reason: null }
}

/** 判换行风格（按**字节**判，解码前就能用；只取前 64 KB）。 */
export function kbDetectEol(data: Buffer): 'lf' | 'crlf' {
  const head = data.subarray(0, 65536)
  let crlf = 0
  let lf = 0
  for (let i = 0; i < head.length; i += 1) {
    if (head[i] === 0x0a) {
      lf += 1
      if (i > 0 && head[i - 1] === 0x0d) crlf += 1
    }
  }
  return crlf > lf - crlf ? 'crlf' : 'lf'
}

// ---------------------------------------------------------------- 遍历（只 stat 不 open）

/** 遍历项（`_kb_walk` 的产物）。 */
export interface KbWalkEntry {
  readonly rel: string
  readonly abs: string
  readonly isDir: boolean
  readonly size: number
  readonly mtime: number
}

/** 从 `startAbs` 起遍历 vault（跳过忽略项、跳过链接），产出 `{rel,abs,isDir,size,mtime}`。 */
export function* kbWalk(vault: KbVaultRow, startAbs: string): Generator<KbWalkEntry> {
  const root = realpathSync(vault.path)
  const stack: string[] = [startAbs]
  while (stack.length > 0) {
    const current = stack.pop() as string
    let names: string[]
    try {
      names = readdirSync(current).sort()
    } catch {
      continue
    }
    for (const name of names) {
      const absPath = path.join(current, name)
      const relative = path.relative(root, absPath)
      if (relative.startsWith('..') || path.isAbsolute(relative)) continue
      if (isReparsePoint(absPath)) continue
      let isDir = false
      try {
        isDir = lstatSync(absPath).isDirectory()
      } catch {
        isDir = false
      }
      const rel = relative.split(path.sep).join('/')
      if (kbIgnored(isDir ? `${rel}/` : rel, vault.ignoreGlobs) !== null) continue
      let size = 0
      let mtime = 0
      if (!isDir) {
        try {
          const stat = lstatSync(absPath)
          size = stat.size
          mtime = stat.mtimeMs / 1000
        } catch {
          size = 0
          mtime = 0
        }
      }
      yield { rel, abs: absPath, isDir, size, mtime }
      if (isDir) stack.push(absPath)
    }
  }
}

// ---------------------------------------------------------------- 预算（双上限）

/** `_kb_fit_text` 的产物。 */
export interface KbFitResult {
  readonly text: string
  readonly chars: number
  readonly tokens: number
  readonly truncated: boolean
  readonly truncatedReasons: readonly string[]
}

const KB_FIT_MARKER_RE = /\n?…（(?:已按字符预算截断|已截断)）\s*$/u

/** 按**双上限**截断（先字符、后 token），并说明截断原因。 */
export function kbFitText(lines: readonly string[], maxChars: number, maxTokens: number): KbFitResult {
  const charsCap = Number.isFinite(maxChars) ? Math.trunc(maxChars) : 0
  const tokenCap = Number.isFinite(maxTokens) ? Math.trunc(maxTokens) : 0
  let text = lines.join('\n')
  const reasons: string[] = []
  if (charsCap !== 0 && codePointLength(text) > charsCap) {
    text = `${sliceCodePoints(text, 0, Math.max(charsCap - 20, 0))}\n…（已按字符预算截断）`
    reasons.push('chars')
  }
  if (tokenCap !== 0) {
    const marker = '\n…（已截断）'
    if (countTokens(text) > tokenCap) {
      const budget = tokenCap - countTokens(marker)
      if (budget <= 0) {
        text = ''
      } else {
        const kept = text.split('\n')
        while (kept.length > 0 && countTokens(kept.join('\n')) > budget) kept.pop()
        text = kept.length > 0 ? `${kept.join('\n')}${marker}` : ''
      }
      reasons.push('tokens')
    }
  }
  if (charsCap !== 0 && codePointLength(text) > charsCap) {
    text = sliceCodePoints(text, 0, charsCap)
  }
  return { text, chars: codePointLength(text), tokens: countTokens(text), truncated: reasons.length > 0, truncatedReasons: reasons }
}

/** 两个上限**同时**成立？ */
function kbWithinBudget(text: string, maxChars: number, maxTokens: number): boolean {
  if (maxChars !== 0 && codePointLength(text) > maxChars) return false
  if (maxTokens !== 0 && countTokens(text) > maxTokens) return false
  return true
}

/** 按双预算截断，并把 `tail` **钉在最后一行**（截断了就告诉你怎么深入）。 */
function kbFitTextWithTail(
  lines: readonly string[],
  tail: string,
  maxChars: number,
  maxTokens: number,
  shortTail = '',
): KbFitResult {
  const base = kbFitText(lines, maxChars, maxTokens)
  const tailText = String(tail ?? '').trim()
  if (tailText === '' || !base.truncated) return base
  const short = String(shortTail ?? '').trim()
  const assemble = (body: string, sentence: string): string => (body !== '' ? `${body}\n${sentence}` : sentence)
  let text = String(base.text ?? '').replace(KB_FIT_MARKER_RE, '')
  let sentence = tailText
  while (!kbWithinBudget(assemble(text, sentence), maxChars, maxTokens)) {
    const kept = text !== '' ? text.split('\n') : []
    if (kept.length > 0) {
      text = kept.slice(0, -1).join('\n')
      continue
    }
    if (short !== '' && sentence !== short) {
      sentence = short
      continue
    }
    sentence = kbFitText([short !== '' ? short : tailText], maxChars, maxTokens).text
    break
  }
  const candidate = assemble(text, sentence)
  return {
    text: candidate,
    chars: codePointLength(candidate),
    tokens: countTokens(candidate),
    truncated: true,
    truncatedReasons: [...base.truncatedReasons],
  }
}

// ---------------------------------------------------------------- vault 读点

/** `_kb_vault_dict`：行 → `KbVaultRow`（`ignore_globs` 解析成 list）。 */
function toVaultRow(row: Record<string, unknown>): KbVaultRow {
  const rawGlobs = row.ignore_globs
  let globs: string[] = []
  if (Array.isArray(rawGlobs)) {
    globs = rawGlobs.map((item) => String(item))
  } else {
    const text = String(rawGlobs ?? '').trim()
    if (text !== '') {
      let parsed: unknown
      try {
        parsed = JSON.parse(text)
      } catch {
        throw new KbError('bad_usage', `ignore_globs 不是合法 JSON：${text.slice(0, 60)}`)
      }
      if (!Array.isArray(parsed)) {
        throw new KbError('bad_usage', `ignore_globs 必须是 JSON 数组，收到 ${typeof parsed}`)
      }
      globs = parsed.map((item) => String(item))
    }
  }
  const mode = String(row.mode ?? 'ro')
  return {
    vaultId: String(row.vault_id ?? ''),
    name: String(row.name ?? ''),
    path: String(row.path ?? ''),
    mode: mode === 'rw' ? 'rw' : 'ro',
    ignoreGlobs: globs,
    maxOpenBytes: Number(row.max_open_bytes ?? KB_DEFAULT_MAX_OPEN_BYTES),
    isDefault: Boolean(row.is_default),
    lastScanAt: row.last_scan_at === null || row.last_scan_at === undefined ? null : String(row.last_scan_at),
    lastScanFiles: row.last_scan_files === null || row.last_scan_files === undefined ? null : Number(row.last_scan_files),
    lastScanNotes: row.last_scan_notes === null || row.last_scan_notes === undefined ? null : Number(row.last_scan_notes),
    lastScanBytes: row.last_scan_bytes === null || row.last_scan_bytes === undefined ? null : Number(row.last_scan_bytes),
    note: row.note === null || row.note === undefined ? null : String(row.note),
    status: String(row.status ?? 'active'),
  }
}

/** 全部 active vault（默认库在前，其次按名字）。 */
export function kbVaultList(db: DatabaseSync): KbVaultRow[] {
  const rows = db
    .prepare(`${KB_VAULT_SELECT} WHERE status='active' ORDER BY is_default DESC, name`)
    .all() as Record<string, unknown>[]
  return rows.map((row) => toVaultRow(row))
}

/** 按 name / vault_id / 缺省（默认库）取一条；取不到 → `KbError('vault_missing')`。 */
export function kbVaultGet(db: DatabaseSync, key?: string | null): KbVaultRow {
  const text = String(key ?? '').trim()
  if (text === '') {
    const row = db.prepare(`${KB_VAULT_SELECT} WHERE is_default=1 AND status='active'`).get() as
      | Record<string, unknown>
      | undefined
    if (row === undefined) {
      throw new KbError(
        'vault_missing',
        '还没有默认知识库',
        'fish kb vault add --name <名字> --path <绝对路径> --default',
      )
    }
    return toVaultRow(row)
  }
  const row = db
    .prepare(`${KB_VAULT_SELECT} WHERE (vault_id=? OR name=?) ORDER BY is_default DESC LIMIT 1`)
    .get(text, text) as Record<string, unknown> | undefined
  if (row === undefined) {
    throw new KbError('vault_missing', `没有这个知识库：${text}`, 'fish kb vault list 看已登记的库')
  }
  const vault = toVaultRow(row)
  if (vault.status !== 'active') {
    throw new KbError('vault_missing', `知识库已停用：${text}`, 'fish kb vault add 重新登记，或换一个库')
  }
  return vault
}

/** vault 摘要（回执头用）。 */
function vaultBrief(vault: KbVaultRow): KbVaultBrief {
  return { name: vault.name, id: vault.vaultId, mode: vault.mode, path: vault.path }
}

/** `_kb_fmt_mtime`：epoch 秒 → 本地 ISO；空/坏 → `—`。 */
function formatMtime(ts: number | null | undefined): string {
  if (ts === null || ts === undefined || ts === 0 || Number.isNaN(ts)) return '—'
  return isoLocal(new Date(ts * 1000))
}

/** `_kb_vault_line`（vault list 的一行）。 */
function vaultLine(vault: KbVaultRow): string {
  const flags: string[] = []
  if (vault.isDefault) flags.push('default')
  if (vault.mode === 'rw') flags.push('rw')
  const tail = flags.length > 0 ? `｜${flags.join('、')}` : ''
  const scan = vault.lastScanAt ?? '未扫描'
  return `${vault.name}\t\`${vault.path}\`\tmode=${vault.mode}\t最近扫描=${scan}${tail}\tvault_id=${vault.vaultId}`
}

// ---------------------------------------------------------------- tree（§4.1）

/** 统计层：递归汇总一个目录（只数不渲染）。 */
function treeDir(vault: KbVaultRow, absDir: string, level: number, maxDepth: number, globs: readonly string[]): KbTreeNode {
  const root = realpathSync(vault.path)
  let names: string[] = []
  try {
    names = readdirSync(absDir).sort()
  } catch {
    names = []
  }
  let notes = 0
  let attachments = 0
  let files = 0
  let totalBytes = 0
  let mtime = 0
  const children: KbTreeChild[] = []
  const subdirs: KbTreeNode[] = []
  let hiddenDirs = 0
  for (const name of names) {
    const absPath = path.join(absDir, name)
    const relative = path.relative(root, absPath)
    if (relative.startsWith('..') || path.isAbsolute(relative)) continue
    if (isReparsePoint(absPath)) continue
    let isDir = false
    try {
      isDir = lstatSync(absPath).isDirectory()
    } catch {
      isDir = false
    }
    const rel = relative.split(path.sep).join('/')
    if (kbIgnored(isDir ? `${rel}/` : rel, globs) !== null) continue
    if (isDir) {
      if (level >= maxDepth) {
        hiddenDirs += 1
        children.push({ rel, name, isDir: true, notes: 0, attachments: 0, bytes: 0, mtime: 0, notScanned: true })
        continue
      }
      const child = treeDir(vault, absPath, level + 1, maxDepth, globs)
      notes += child.notes
      attachments += child.attachments
      files += child.files
      totalBytes += child.bytes
      hiddenDirs += child.hiddenDirs
      mtime = Math.max(mtime, child.mtime)
      children.push({
        rel,
        name,
        isDir: true,
        notes: child.notes,
        attachments: child.attachments,
        bytes: child.bytes,
        mtime: child.mtime,
        notScanned: false,
      })
      subdirs.push(child)
      continue
    }
    let size = 0
    let itemMtime = 0
    try {
      const stat = lstatSync(absPath)
      size = stat.size
      itemMtime = stat.mtimeMs / 1000
    } catch {
      size = 0
      itemMtime = 0
    }
    files += 1
    totalBytes += size
    mtime = Math.max(mtime, itemMtime)
    const isNote = name.toLowerCase().endsWith('.md')
    if (isNote) notes += 1
    else attachments += 1
    children.push({
      rel,
      name,
      isDir: false,
      notes: 0,
      attachments: isNote ? 0 : 1,
      bytes: size,
      mtime: itemMtime,
      notScanned: false,
    })
  }
  children.sort((left, right) => (right.mtime !== left.mtime ? right.mtime - left.mtime : left.rel < right.rel ? -1 : left.rel > right.rel ? 1 : 0))
  subdirs.sort((left, right) => (left.rel < right.rel ? -1 : left.rel > right.rel ? 1 : 0))
  const relOfDir = absDir === root ? '' : path.relative(root, absDir).split(path.sep).join('/')
  return {
    rel: relOfDir,
    notes,
    attachments,
    files,
    bytes: totalBytes,
    hiddenDirs,
    partial: hiddenDirs > 0,
    mtime,
    children: children.slice(0, KB_MAX_ITEMS_PER_DIR),
    more: Math.max(0, children.length - KB_MAX_ITEMS_PER_DIR),
    dirs: subdirs,
  }
}

/** 显示层：`depth` 的唯一语义定义（渲染侧独立裁剪，不信任统计层）。 */
function treeLines(
  vault: KbVaultRow,
  startRel: string,
  tree: KbTreeNode,
  depth: number,
  asOf: string,
  ignoreGlobs: readonly string[],
): string[] {
  const maxLevel = Math.max(0, Math.trunc(depth))
  const lines: string[] = [
    `# 知识库目录 · ${vault.name}`,
    '',
    `> source=知识库（未建索引）｜as_of ${asOf}`,
    `> vault=\`${vault.path}\`（mode=${vault.mode}）｜起点=\`${startRel !== '' ? startRel : '/'}\`｜深度=${depth}`,
    `> 生效忽略规则：${ignoreGlobs.length > 0 ? ignoreGlobs.join(', ') : '（无）'}`,
    KB_TREE_DEPTH_RULE,
    `> 统计面：数到第 ${depth} 层（更深的目录不显示、也不计入合计）；只统计不读正文：笔记 = .md 计数，附件 = 其余文件计数`,
    `> 每目录最多列 ${KB_MAX_ITEMS_PER_DIR} 项（超出记 more=N）；更深请加大 --depth（上限 4），或给 path=<目录> 直接进入`,
    '',
  ]
  const emit = (node: KbTreeNode, level: number): void => {
    const pad = '  '.repeat(level)
    const name = node.rel !== '' ? node.rel : vault.name
    lines.push(
      `${pad}- ${name}/  ${node.notes} 篇笔记 / ${node.attachments} 附件 / ${node.bytes} B` +
        ` / 最近修改=${formatMtime(node.mtime)}`,
    )
    for (const child of node.children) {
      if (child.isDir) continue
      if (!child.name.toLowerCase().endsWith('.md')) continue
      lines.push(`${pad}  - ${child.rel}  ${child.bytes} B  ${formatMtime(child.mtime)}`)
    }
    if (node.more !== 0) lines.push(`${pad}  - …还有 ${node.more} 项未列出（more=${node.more}）`)
    if (level >= maxLevel) return
    for (const sub of node.dirs) emit(sub, level + 1)
  }
  emit(tree, 0)
  return lines
}

/** 截断时钉在末尾的那一句（给一个能直接粘回去的目录）。 */
function treeTailHint(tree: KbTreeNode, startRel: string): string {
  for (const sub of tree.dirs) {
    if (sub.rel !== '') {
      return `> 已截断：用 \`path=${sub.rel}\` 深入（或加大 --max-chars / --max-tokens；完整读数见 --json）`
    }
  }
  if (startRel !== '') {
    return `> 已截断：用 \`path=${startRel}\` 深入某一级子目录（或加大 --max-chars / --max-tokens）`
  }
  return '> 已截断：加大 --max-chars / --max-tokens 再看（起点下没有可深入的子目录）'
}

/** 看目录（只统计、不读正文、双预算）——§4.1 / KB-2 / KB-3。 */
export function kbTree(
  vault: KbVaultRow,
  options: { readonly rel?: string; readonly depth?: number; readonly maxChars?: number; readonly maxTokens?: number; readonly now?: Date } = {},
): KbTreeResult {
  const rel = options.rel ?? ''
  const depthArg = options.depth ?? 2
  const maxChars = options.maxChars ?? 8000
  const maxTokens = options.maxTokens ?? 1200
  const now = options.now ?? new Date()
  const wantDepthRaw = Math.trunc(Number(depthArg))
  if (!Number.isFinite(wantDepthRaw)) {
    throw new KbError('bad_usage', `depth 必须是整数，收到 ${pyText(depthArg)}`)
  }
  const wantDepth = Math.max(1, Math.min(wantDepthRaw, KB_TREE_MAX_DEPTH))
  const globs = effectiveGlobs(vault.ignoreGlobs)
  if (!existsSync(vault.path) || !statSync(vault.path).isDirectory()) {
    if (vault.lastScanAt !== null) {
      const text = [
        `# 知识库目录（离线）· ${vault.name}`,
        '',
        `> 知识库不在线（最后扫描 ${vault.lastScanAt}）：路径 \`${vault.path}\` 打不开。`,
        `> 上次扫描读数：${pyText(vault.lastScanFiles)} 文件 / ${pyText(vault.lastScanNotes)} 笔记 / ${pyText(vault.lastScanBytes)} 字节。`,
        '> 下面没有任何实时内容——不是空库，是离线。',
      ].join('\n')
      const fitted = kbFitText([text], maxChars, maxTokens)
      return {
        vault: vaultBrief(vault),
        asOf: isoLocal(now),
        root: String(rel ?? '').replace(/\\/gu, '/').replace(/^\/+|\/+$/gu, ''),
        depth: wantDepth,
        ignoreGlobs: globs,
        dirs: [],
        summary: null,
        scan: { files: null, bytes: null, ms: 0, stale: true, partial: false, hiddenDirs: 0 },
        text: fitted.text,
        chars: fitted.chars,
        tokens: fitted.tokens,
        truncated: fitted.truncated,
        truncatedReasons: fitted.truncatedReasons,
      }
    }
    throw new KbError('offline', `知识库不在线：${vault.path}`, '检查盘符/目录是否改名；有历史扫描数时会降级显示')
  }
  const target = kbResolveTarget(vault, rel)
  if (!existsSync(target.abs)) {
    throw new KbError('not_found', `知识库里没有这个目录：${target.rel}`, 'fish kb tree 看根目录，再逐层进入')
  }
  const startedAt = process.hrtime.bigint()
  const tree = treeDir(vault, target.abs, 0, wantDepth, globs)
  const ms = Number((process.hrtime.bigint() - startedAt) / 1000000n)
  const asOf = isoLocal(now)
  const lines = treeLines(vault, target.rel, tree, wantDepth, asOf, globs)
  const fitted = kbFitTextWithTail(
    lines,
    treeTailHint(tree, target.rel),
    maxChars,
    maxTokens,
    '> 已截断：加大 --max-chars / --max-tokens 再看（或 --json）',
  )
  const dirs = [...tree.dirs].sort((left, right) => (left.rel < right.rel ? -1 : left.rel > right.rel ? 1 : 0))
  return {
    vault: vaultBrief(vault),
    asOf,
    root: target.rel,
    depth: wantDepth,
    dirs,
    summary: tree,
    ignoreGlobs: globs,
    scan: { files: tree.files, bytes: tree.bytes, ms, stale: false, partial: tree.partial, hiddenDirs: tree.hiddenDirs },
    text: fitted.text,
    chars: fitted.chars,
    tokens: fitted.tokens,
    truncated: fitted.truncated,
    truncatedReasons: fitted.truncatedReasons,
  }
}

// ---------------------------------------------------------------- open（§4.2）

/** 严格布尔解析（`_strict_bool`）：`"false"` 是**拒绝**，模糊写法直接报错。 */
export function parseStrictBool(value: unknown, name: string): boolean {
  if (value === null || value === undefined) return false
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') {
    if (value === 0) return false
    if (value === 1) return true
  } else if (typeof value === 'string') {
    const text = value.trim().toLowerCase()
    if (text === 'true' || text === '1') return true
    if (text === 'false' || text === '0') return false
  }
  throw new KbError(
    'bad_usage',
    `${name} 只接受 true / false（也接受 1 / 0），收到 ${pyText(value)}`,
    '布尔开关不接受模糊写法：写 true 或 false 才是明确的授权/拒绝；CLI 不带 `--allow-sensitive` 即拒绝',
  )
}

/** 敏感文件的拒读说明（文件名 + 大小 + 建议）。 */
function sensitiveNotice(relPosix: string, size: number): string {
  return (
    `\`${relPosix}\` 是敏感文件（${size} B），默认不把正文送进模型上下文；` +
    '建议在 Obsidian 里直接打开。确需由 AI 读取，请显式二次授权'
  )
}

/** 读单文件（必须给显式相对路径）——§4.2 / KB-7 / KB-8。 */
export function kbOpen(
  vault: KbVaultRow,
  rel: string,
  options: { readonly maxChars?: number; readonly allowSensitive?: unknown; readonly now?: Date } = {},
): KbOpenResult {
  const allowSensitive = parseStrictBool(options.allowSensitive, 'allow_sensitive')
  const now = options.now ?? new Date()
  void now
  const limit = vault.maxOpenBytes > 0 ? vault.maxOpenBytes : KB_DEFAULT_MAX_OPEN_BYTES
  const target = kbResolveTarget(vault, rel)
  const cap = Math.max(1, Math.trunc(options.maxChars ?? 64000))
  if (!Number.isFinite(cap)) throw new KbError('bad_usage', `max_chars 必须是整数，收到 ${pyText(options.maxChars)}`)
  if (existsSync(target.abs) && statSync(target.abs).isDirectory()) {
    throw new KbError('bad_usage', `目标是目录，不是文件：${target.rel !== '' ? target.rel : '/'}`, '用 `fish kb tree <目录>` 看目录；open 只读单个文件')
  }
  if (!existsSync(target.abs) || !statSync(target.abs).isFile()) {
    throw new KbError('not_found', `知识库里没有这个文件：${target.rel}`, '`fish kb tree` 看目录结构，再给完整相对路径')
  }
  const size = statSync(target.abs).size
  if (size > limit) {
    throw new KbError(
      'too_large',
      `文件 ${size} B 超过单文件上限 ${limit} B`,
      '长文档请分段读：`kb open <path> --max-chars`，或直接用文件工具读',
    )
  }
  const sensitive = target.sensitive
  if (sensitive && !allowSensitive) {
    throw new KbError('sensitive', sensitiveNotice(target.rel, size), 'CLI 加 `--allow-sensitive`；MCP 传 allow_sensitive=true')
  }
  const raw = readFileSync(target.abs)
  const detected = kbDetectText(raw, path.extname(target.abs))
  if (detected.media) {
    throw new KbError('media', `这是媒体/二进制文件（${detected.reason ?? ''}），不按文本读`, '用对应的查看器打开；知识库只读文本')
  }
  if (!detected.isText || detected.encoding === null) {
    throw new KbError('bad_encoding', '不是可识别的文本编码（UTF-8 / GB18030 都失败）', '另存为 UTF-8 再读')
  }
  const text = new TextDecoder(detected.encoding === 'utf-8-sig' ? 'utf-8' : detected.encoding, { fatal: true }).decode(raw)
  const sha = createSha256(raw)
  const truncated = codePointLength(text) > cap
  const shownText = truncated ? sliceCodePoints(text, 0, cap) : text
  const shownBytes = truncated ? Buffer.byteLength(shownText, 'utf8') : raw.length
  const statements = [
    '来源=知识库（未建索引），不是 fish 记忆；不进 recall / 全文索引。',
    '本库未建索引；要检索请显式 `fish kb search`（按需扫描）。',
    (truncated ? '已截断' : '未截断') +
      `（${raw.length}/${limit} B 上限；显示 ${codePointLength(shownText)}/${codePointLength(text)} 字）`,
  ]
  if (sensitive) statements.push('本文件命中敏感名单：正文是在**显式授权**（allow_sensitive）下读出的。')
  return {
    rel: target.rel,
    abs: target.abs,
    bytes: raw.length,
    sha256: sha,
    encoding: detected.encoding,
    eol: kbDetectEol(raw),
    truncated,
    sensitive,
    shown: shownBytes,
    limit,
    charsShown: codePointLength(shownText),
    charsTotal: codePointLength(text),
    statements,
    text: shownText,
  }
}

/** sha256（只在内存里算，不落盘）。 */
function createSha256(data: Buffer): string {
  return createHash('sha256').update(data).digest('hex')
}

// ---------------------------------------------------------------- search（§4.3）

/** 在 vault 内**按需扫描**检索（不建索引）——§4.3 / KB-10 / KB-9。 */
export function kbSearch(
  vault: KbVaultRow,
  query: string,
  options: { readonly limit?: number; readonly maxChars?: number; readonly maxTokens?: number; readonly now?: Date } = {},
): KbSearchResult {
  const q = String(query ?? '').trim()
  if (q === '') throw new KbError('bad_usage', '检索词不能为空', 'fish kb search <关键词>')
  if (!existsSync(vault.path) || !statSync(vault.path).isDirectory()) {
    throw new KbError('offline', `知识库不在线：${vault.path}`, '检查盘符/目录是否改名')
  }
  const want = Math.max(1, Math.min(Math.trunc(options.limit ?? 10), KB_MAX_LIMIT))
  const maxChars = options.maxChars ?? 8000
  const maxTokens = options.maxTokens ?? 1800
  const needle = q.toLowerCase()
  const asOf = isoLocal(options.now ?? new Date())
  const startedAt = process.hrtime.bigint()
  let scannedFiles = 0
  let scannedBytes = 0
  let skippedLarge = 0
  let skippedBinary = 0
  let skippedEncoding = 0
  let skippedSensitive = 0
  const hits: KbSearchHit[] = []
  for (const entry of kbWalk(vault, realpathSync(vault.path))) {
    if (entry.isDir || !entry.rel.toLowerCase().endsWith('.md')) continue
    if (kbIsSensitiveName(entry.rel)) {
      skippedSensitive += 1
      continue
    }
    scannedFiles += 1
    if (entry.size > KB_SEARCH_MAX_FILE_BYTES) {
      skippedLarge += 1
      continue
    }
    let raw: Buffer
    try {
      raw = readFileSync(entry.abs)
    } catch {
      continue
    }
    scannedBytes += raw.length
    const detected = kbDetectText(raw, '.md')
    if (!detected.isText || detected.encoding === null) {
      if (detected.media) skippedBinary += 1
      else skippedEncoding += 1
      continue
    }
    const text = new TextDecoder(detected.encoding === 'utf-8-sig' ? 'utf-8' : detected.encoding, { fatal: false }).decode(raw)
    const matched: { lineno: number; text: string }[] = []
    let occurrences = 0
    const lines = text.split(/\r\n|\r|\n/u)
    for (let i = 0; i < lines.length; i += 1) {
      const line = lines[i] as string
      const folded = line.toLowerCase()
      if (!folded.includes(needle)) continue
      occurrences += folded.split(needle).length - 1
      let snippet = line.trim()
      if (codePointLength(snippet) > 240) {
        const position = snippet.toLowerCase().indexOf(needle)
        const start = Math.max(0, position - 60)
        snippet = `${start > 0 ? '…' : ''}${sliceCodePoints(snippet, start, start + 240)}…`
      }
      matched.push({ lineno: i + 1, text: snippet })
    }
    if (matched.length > 0) {
      hits.push({ rel: entry.rel, abs: entry.abs, occurrences, lines: matched.slice(0, 3), mtime: entry.mtime })
    }
  }
  const ms = Number((process.hrtime.bigint() - startedAt) / 1000000n)
  hits.sort((left, right) => {
    if (right.occurrences !== left.occurrences) return right.occurrences - left.occurrences
    if (right.mtime !== left.mtime) return right.mtime - left.mtime
    return left.rel < right.rel ? -1 : left.rel > right.rel ? 1 : 0
  })
  const total = hits.length
  const shown = hits.slice(0, want)
  const queryHash = sha12(q.toLowerCase())
  const lines = [
    `# 知识库检索 · ${vault.name}`,
    '',
    `> source=知识库（未建索引）｜as_of ${asOf}`,
    `> scan=${scannedFiles} files / ${scannedBytes} bytes / ${ms} ms｜命中 ${total} 文件，显示 ${shown.length} 个`,
    `> 跳过：大文件 ${skippedLarge} / 二进制 ${skippedBinary} / 编码不可读 ${skippedEncoding} / 敏感名单 ${skippedSensitive}（单文件上限 ${KB_SEARCH_MAX_FILE_BYTES} B）`,
    '> 只扫已登记 vault 的 `.md`；不建索引、不进 `memory_fts`——找 fish 记忆请用 `recall`。',
    '',
  ]
  for (const hit of shown) {
    lines.push(`- \`${hit.abs}\`（库内 \`${hit.rel}\`，命中 ${hit.occurrences} 处）`)
    for (const item of hit.lines) lines.push(`  - L${item.lineno}: ${item.text}`)
  }
  if (shown.length === 0) lines.push('- （没有命中）')
  const fitted = kbFitText(lines, maxChars, maxTokens)
  return {
    vault: vaultBrief(vault),
    asOf,
    queryHash,
    qlen: codePointLength(q),
    limit: want,
    scanned: { files: scannedFiles, bytes: scannedBytes, ms, skippedLarge, skippedBinary, skippedEncoding, skippedSensitive },
    hits: shown,
    total,
    shown: shown.length,
    text: fitted.text,
    chars: fitted.chars,
    tokens: fitted.tokens,
    truncated: fitted.truncated,
    truncatedReasons: fitted.truncatedReasons,
  }
}

/** sha256 前 12 位（`query_hash` 与写路径的 `write_id` 前缀同口径）。 */
function sha12(text: string): string {
  return createHash('sha256').update(text, 'utf8').digest('hex').slice(0, 12)
}

// ---------------------------------------------------------------- stats（§3.2）

/** 在线就现扫，离线就回退 `last_scan_*`（`source` 标明来源，不冒充实时）。 */
function scanCounts(vault: KbVaultRow): {
  files: number | null
  notes: number | null
  bytes: number | null
  source: 'live' | 'last_scan'
  online: boolean
} {
  if (!existsSync(vault.path) || !statSync(vault.path).isDirectory()) {
    return { files: vault.lastScanFiles, notes: vault.lastScanNotes, bytes: vault.lastScanBytes, source: 'last_scan', online: false }
  }
  let files = 0
  let notes = 0
  let total = 0
  for (const entry of kbWalk(vault, realpathSync(vault.path))) {
    if (entry.isDir) continue
    files += 1
    total += entry.size
    if (entry.rel.toLowerCase().endsWith('.md')) notes += 1
  }
  return { files, notes, bytes: total, source: 'live', online: true }
}

/** 规模 / 未读 / 最近变更 / 每库读写计数 / `kb_access` 总行数。 */
export function kbStats(db: DatabaseSync, key?: string | null, now: Date = new Date()): KbStatsResult {
  const vaults = String(key ?? '').trim() !== '' ? [kbVaultGet(db, key)] : kbVaultList(db)
  const asOf = isoLocal(now)
  const out: KbVaultStats[] = []
  for (const vault of vaults) {
    const counts = scanCounts(vault)
    const scalar = (sql: string): number => {
      const row = db.prepare(sql).get(vault.vaultId) as { n?: number } | undefined
      return Number(row?.n ?? 0)
    }
    const opened = scalar(
      "SELECT COUNT(DISTINCT path) AS n FROM kb_access WHERE vault_id=? AND action='open' AND result='ok'",
    )
    const writes = scalar(
      "SELECT COUNT(*) AS n FROM kb_access WHERE vault_id=? AND action IN ('create','append','update') AND result='ok'",
    )
    const rollbacks = scalar("SELECT COUNT(*) AS n FROM kb_access WHERE vault_id=? AND action='rollback'")
    const reads = scalar(
      "SELECT COUNT(*) AS n FROM kb_access WHERE vault_id=? AND action IN ('open','tree','search','search_hit')",
    )
    const unread = counts.notes === null ? null : Math.max(0, counts.notes - opened)
    out.push({
      name: vault.name,
      vaultId: vault.vaultId,
      mode: vault.mode,
      path: vault.path,
      files: counts.files,
      notes: counts.notes,
      bytes: counts.bytes,
      countsSource: counts.source,
      online: counts.online,
      openedPaths: opened,
      unreadNotes: unread,
      reads,
      writes,
      rollbacks,
      lastScanAt: vault.lastScanAt,
    })
  }
  const accessRow = db.prepare('SELECT COUNT(*) AS n FROM kb_access').get() as { n?: number } | undefined
  const accessRows = Number(accessRow?.n ?? 0)
  const recent = (
    db
      .prepare('SELECT id, ts, path, action, result, citizen_id FROM kb_access ORDER BY id DESC LIMIT 10')
      .all() as Record<string, unknown>[]
  ).map((row) => ({
    id: Number(row.id),
    ts: String(row.ts ?? ''),
    path: String(row.path ?? ''),
    action: String(row.action ?? ''),
    result: String(row.result ?? ''),
    citizenId: row.citizen_id === null || row.citizen_id === undefined ? null : String(row.citizen_id),
  }))
  const lines = ['# 知识库规模（stats）', '', `> as_of ${asOf}`, `> kb_access 总行数：${accessRows}`]
  for (const item of out) {
    lines.push(
      '',
      `## ${item.name}（mode=${item.mode}）`,
      `- 路径：${item.path}`,
      `- 规模：${pyText(item.files)} 文件 / ${pyText(item.notes)} 笔记 / ${pyText(item.bytes)} 字节（来源：${item.countsSource}）`,
      `- 未读（估算）：${pyText(item.unreadNotes)} 篇（无 open 记录）`,
      `- 读 ${item.reads} 次 / 写 ${item.writes} 次 / 回滚 ${item.rollbacks} 次`,
    )
  }
  lines.push('', '## 最近变更', '')
  if (recent.length > 0) {
    for (const item of recent) lines.push(`- ${item.ts} ${item.action}(${item.result}) \`${item.path}\` by ${pyText(item.citizenId)}`)
  } else {
    lines.push('- （还没有 kb_access 记录）')
  }
  return { asOf, vaults: out, accessRows, recent, text: lines.join('\n') }
}

// ---------------------------------------------------------------- CLI 渲染（与生产逐行一致）

/** `fish kb vault list` 的回执。 */
export function renderKbVaultList(vaults: readonly KbVaultRow[]): string {
  if (vaults.length === 0) {
    return '还没有登记任何知识库。登记入口：fish kb vault add --name <n> --path <绝对路径> [--mode ro|rw] [--default]'
  }
  return [...vaults.map((vault) => vaultLine(vault)), `共 ${vaults.length} 个知识库`].join('\n')
}

/** `fish kb tree` 的回执（正文 + 一行合计）。 */
export function renderKbTree(result: KbTreeResult): string {
  const scan = result.scan
  return [
    result.text,
    '',
    `合计：${pyText(scan.files)} 文件 / ${pyText(scan.bytes)} 字节 / ${scan.ms} ms` +
      (scan.stale ? '（离线：上次扫描数）' : '') +
      `｜chars=${result.chars} tokens=${result.tokens} truncated=${pyText(result.truncated)}`,
  ].join('\n')
}

/** `fish kb open` 的回执（三句声明 + 正文）。 */
export function renderKbOpen(result: KbOpenResult): string {
  return [...result.statements.map((line) => `> ${line}`), '', result.text].join('\n')
}

/** `fish kb search` 的回执。 */
export function renderKbSearch(result: KbSearchResult): string {
  return result.text
}

/** `fish kb stats` 的回执。 */
export function renderKbStats(result: KbStatsResult): string {
  return result.text
}

/** 知识库域错误的回执（与生产 `错误[<code>]：<message>` 同形）。 */
export function renderKbError(error: KbError): string {
  return `错误[${error.code}]：${error.format()}`
}
