/**
 * 中文切词（能力块 #12 布局/检索的公共纯函数）。
 *
 * 为什么这一小块必须逐 token 等价：SQLite 的 `unicode61` 不做中文分词，fish 自己把
 * 文本切成**重叠 bigram** 再入 FTS（`p0/fish.py:3857`，ADR-005）。TS 侧若切得差一位，
 * 检索不会报错，只会**静默漏召回**——那是影子期最贵的一类坏。
 *
 * 真源是 `spec/layout/cut.json`（规则 + 由 Python 冻结的向量）；本文件只实现规则，
 * 不自己发明规则。向量对拍在 `scripts/check-spec.mjs` 与 `test/conformance/` 里跑。
 *
 * @module dsh-fish/layout/cut
 */

import { SpecError } from '../core/errors.ts'

/** 中文连续段的切分方式。 */
export interface CjkRunRules {
  /** `overlapping-bigram` = 两字滑窗（Python 现状）；`whole-run` = 整段一个 token（错的，用于变异）。 */
  readonly mode: 'overlapping-bigram' | 'whole-run'
  /** 滑窗宽度（Python 固定 2）。 */
  readonly window: number
  /** 滑窗步长（Python 固定 1）。 */
  readonly step: number
  /** 单字段落怎么处理：`as-is` = 原样成 token（Python 行为）。 */
  readonly singleCharRun: 'as-is' | 'drop'
}

/** 切词规则（全部来自 spec）。 */
export interface CutRules {
  /** 拉丁/数字/单号的字符类（Python: `[A-Za-z0-9][A-Za-z0-9\-_.]*`）。 */
  readonly latin: string
  /** 中文连续段的字符类（Python: `[\u4e00-\u9fff]+`）。 */
  readonly cjk: string
  /** 是否把拉丁 token 小写（Python: `.lower()`）。 */
  readonly lowercase: boolean
  /**
   * token 输出顺序：`latin-then-cjk` = 先全部拉丁、再全部中文（Python 的两趟 finditer）；
   * `positional` = 按出现位置交错（**不是** Python 行为，仅作对照）。
   */
  readonly tokenOrder: 'latin-then-cjk' | 'positional'
  readonly cjkRun: CjkRunRules
}

/** MATCH 表达式规则。 */
export interface CutMatchRules {
  /** 每个 token 是否加双引号（不加时 `CR-12` 会被 FTS5 解析成列名）。 */
  readonly quoteEveryToken: boolean
  /** token 之间的连接符（Python: `' AND '`）。 */
  readonly joiner: string
  /** 没有可查 token 时的返回（Python: `None`；JSON 里只能写 `null`）。 */
  readonly emptyResult: null
}

/** 一条对拍向量：输入串 → 期望 token 序列（期望值由 Python 侧冻结）。 */
export interface CutVector {
  readonly id: string
  readonly input: string
  readonly tokens: readonly string[]
  readonly note?: string
}

/** `spec/layout/cut.json` 的形状。 */
export interface CutSpec {
  readonly specVersion: string
  readonly rules: CutRules
  readonly match: CutMatchRules
  readonly vectors: readonly CutVector[]
}

/** 编译后的切词器（正则只编译一次，避免每次调用都 new RegExp）。 */
export interface CompiledCut {
  readonly rules: CutRules
  cut(text: string): string[]
  matchExpression(query: string): string | null
}

/** 与 Python 完全一致的默认规则（spec 生成时写入同一份值）。 */
export const DEFAULT_CUT_RULES: CutRules = {
  latin: '[A-Za-z0-9][A-Za-z0-9\\-_.]*',
  cjk: '[\\u4e00-\\u9fff]+',
  lowercase: true,
  tokenOrder: 'latin-then-cjk',
  cjkRun: { mode: 'overlapping-bigram', window: 2, step: 1, singleCharRun: 'as-is' },
}

/** 与 Python 完全一致的默认 MATCH 规则。 */
export const DEFAULT_MATCH_RULES: CutMatchRules = {
  quoteEveryToken: true,
  joiner: ' AND ',
  emptyResult: null,
}

/** 校验规则自洽（错规则必须响亮失败，而不是"切出另一种东西"）。 */
function validateRules(rules: CutRules): void {
  if (rules.cjkRun.mode === 'overlapping-bigram') {
    if (!Number.isInteger(rules.cjkRun.window) || rules.cjkRun.window < 2) {
      throw new SpecError('SPEC_INVALID', `cjkRun.window 必须是 >=2 的整数，收到 ${rules.cjkRun.window}`, {
        hint: '修正 spec/layout/cut.json 的 cjkRun.window（Python 现状为 2）',
      })
    }
    if (!Number.isInteger(rules.cjkRun.step) || rules.cjkRun.step < 1) {
      throw new SpecError('SPEC_INVALID', `cjkRun.step 必须是 >=1 的整数，收到 ${rules.cjkRun.step}`, {
        hint: '修正 spec/layout/cut.json 的 cjkRun.step（Python 现状为 1）',
      })
    }
  }
  for (const [key, pattern] of [['latin', rules.latin], ['cjk', rules.cjk]] as const) {
    try {
      // 只验证可编译：字符类本身由 spec 提供。
      new RegExp(pattern, 'g')
    } catch (error) {
      throw new SpecError('SPEC_INVALID', `切词规则 ${key} 不是合法正则：${pattern}`, {
        hint: '修正 spec/layout/cut.json 的对应字段',
        cause: error,
      })
    }
  }
}

/** 编译切词规则。 */
export function compileCut(rules: CutRules = DEFAULT_CUT_RULES, match: CutMatchRules = DEFAULT_MATCH_RULES): CompiledCut {
  validateRules(rules)
  const latinRe = new RegExp(rules.latin, 'g')
  const cjkRe = new RegExp(rules.cjk, 'g')
  const { window, step, mode, singleCharRun } = rules.cjkRun

  const cutCjkRun = (run: string): string[] => {
    if (run.length === 1) return singleCharRun === 'as-is' ? [run] : []
    if (mode === 'whole-run') return [run]
    const out: string[] = []
    for (let i = 0; i + window <= run.length; i += step) out.push(run.slice(i, i + window))
    return out
  }

  const cut = (text: string): string[] => {
    const source = typeof text === 'string' ? text : ''
    const latinTokens: { at: number; token: string }[] = []
    for (const hit of source.matchAll(latinRe)) {
      const raw = hit[0]
      latinTokens.push({ at: hit.index ?? 0, token: rules.lowercase ? raw.toLowerCase() : raw })
    }
    const cjkTokens: { at: number; token: string }[] = []
    for (const hit of source.matchAll(cjkRe)) {
      const start = hit.index ?? 0
      for (const token of cutCjkRun(hit[0])) cjkTokens.push({ at: start, token })
    }
    if (rules.tokenOrder === 'latin-then-cjk') {
      return [...latinTokens.map((t) => t.token), ...cjkTokens.map((t) => t.token)]
    }
    // positional：按出现位置交错（同一位置拉丁优先——Python 不存在这种形态，仅对照用）。
    return [...latinTokens, ...cjkTokens]
      .map((entry, order) => ({ ...entry, order }))
      .sort((a, b) => (a.at === b.at ? a.order - b.order : a.at - b.at))
      .map((entry) => entry.token)
  }

  const matchExpression = (query: string): string | null => {
    const tokens = cut(query).filter((token) => token !== '')
    if (tokens.length === 0) return match.emptyResult
    return tokens
      .map((token) => (match.quoteEveryToken ? `"${token.replace(/"/g, '""')}"` : token))
      .join(match.joiner)
  }

  return { rules, cut, matchExpression }
}

/** 便捷入口：用默认规则切一次（内部仍走 `compileCut`，保证只有一条实现）。 */
export function cutTokens(text: string, rules: CutRules = DEFAULT_CUT_RULES): string[] {
  return compileCut(rules).cut(text)
}

/** 便捷入口：文本 → 入 FTS 的预处理串（空格连接）。 */
export function cutText(text: string, rules: CutRules = DEFAULT_CUT_RULES): string {
  return cutTokens(text, rules).join(' ')
}

/** 便捷入口：查询串 → FTS5 MATCH 表达式；无 token 时 `null`（与 Python 的 `None` 对齐）。 */
export function ftsMatchExpression(query: string, rules: CutRules = DEFAULT_CUT_RULES, match: CutMatchRules = DEFAULT_MATCH_RULES): string | null {
  return compileCut(rules, match).matchExpression(query)
}

/** 一条向量对拍失败的原因。 */
export interface CutVectorFailure {
  readonly id: string
  readonly input: string
  readonly expected: readonly string[]
  readonly actual: readonly string[]
  readonly firstDiffAt: number
}

/**
 * 用 spec 内的向量校验切词实现。
 * @returns 失败清单（空数组 = 全部通过）
 */
export function verifyCutVectors(spec: CutSpec, rules: CutRules = spec.rules): CutVectorFailure[] {
  const compiled = compileCut(rules, spec.match)
  const failures: CutVectorFailure[] = []
  for (const vector of spec.vectors) {
    const actual = compiled.cut(vector.input)
    const length = Math.min(actual.length, vector.tokens.length)
    let firstDiffAt = length
    for (let i = 0; i < length; i += 1) {
      if (actual[i] !== vector.tokens[i]) {
        firstDiffAt = i
        break
      }
    }
    if (actual.length !== vector.tokens.length || firstDiffAt !== length) {
      failures.push({
        id: vector.id,
        input: vector.input,
        expected: vector.tokens,
        actual,
        firstDiffAt,
      })
    }
  }
  return failures
}
