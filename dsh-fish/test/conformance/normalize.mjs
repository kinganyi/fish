/**
 * 规范化（双跑器的**唯一**实现）。
 *
 * 为什么集中在 diff 一侧而不是两侧各写一份：两侧各写一份规范化的必然结果是"差异为零"
 * 可能只是被两套规则各自抹平（假绿）。这里只有一份规则、一份实现，并且 `diff.mjs`
 * 自带 `--self-test`（人为扰动一侧 → 必须报红），防止比较器本身退化成空转。
 *
 * 规则都是**纯文本替换**，且每个规则都会回报"实际命中几次"——命中 0 次也是一条读数
 * （说明这条规则在这批数据里没起作用，别把它当成"我已经处理过了"）。
 *
 * @module test/conformance/normalize
 */

/** 规则名 → 实现。新增规则必须同时更新 README 与 fixtures 的规则清单。 */
const RULES = {
  /** ISO8601 时间戳 → `<TS>`（注入包 as-of、凭证签发时间、看板 last_seen）。 */
  'strip-iso-timestamp': {
    pattern: /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?/g,
    replace: '<TS>',
  },
  /** 26 位 ULID（fish 的 citizen_id / 事件 id）→ `<ULID>`。 */
  'strip-ulid': {
    pattern: /\b[0-9A-HJKMNP-TV-Z]{26}\b/g,
    replace: '<ULID>',
  },
  /** 64 位 sha256（token_hash / 附件摘要）→ `<SHA256>`。 */
  'strip-sha256': {
    pattern: /\b[0-9a-f]{64}\b/g,
    replace: '<SHA256>',
  },
  /** 绝对路径（Windows 盘符 或 POSIX 绝对路径）→ `<PATH>`。 */
  'mask-abs-path': {
    pattern: /(?:[A-Za-z]:\\[^\s"'`<>|]+|\/(?:Users|home|tmp|var)\/[^\s"'`<>|]+)/g,
    replace: '<PATH>',
  },
  /** 自增行号（`#12` / `seq=12`）→ `<SEQ>`：规范化必须去自增，否则永远不等。 */
  'mask-auto-increment': {
    pattern: /\b(?:seq|id)=(\d+)/g,
    replace: 'seq=<SEQ>',
  },
}

/** 行级规则：整行丢弃（正则按行首匹配）。 */
const LINE_RULES = {
  /** 包尾 as-of 行（含"数据截至"时刻）→ 丢弃（时刻已在 strip-iso-timestamp 里变 <TS>，语义上仍应去掉整行）。 */
  'drop-as-of-line': /^as-of /,
  /** 空行 → 丢弃（两端排版差异不该算内容差异）。 */
  'drop-blank-line': /^\s*$/,
}

/** 所有可用规则名（供 fixtures 自校验）。 */
export const ALL_RULES = [...Object.keys(RULES), ...Object.keys(LINE_RULES)]

/** 规范化一份产物，返回规范化结果与每条规则的命中次数。 */
export function normalizeOutput(payload, ruleNames = []) {
  const unknown = ruleNames.filter((name) => !ALL_RULES.includes(name))
  if (unknown.length > 0) {
    throw new Error(`未知规范化规则：${unknown.join(', ')}（可用：${ALL_RULES.join(', ')}）`)
  }
  const hits = Object.fromEntries(ruleNames.map((name) => [name, 0]))
  const lineRuleNames = ruleNames.filter((name) => name in LINE_RULES)

  const applyString = (text) => {
    let out = text
    for (const name of ruleNames) {
      const rule = RULES[name]
      if (rule === undefined) continue
      out = out.replace(rule.pattern, (...args) => {
        hits[name] += 1
        return typeof rule.replace === 'string' ? rule.replace : rule.replace(...args)
      })
    }
    if (lineRuleNames.length > 0) {
      const kept = []
      for (const line of out.split('\n')) {
        const dropped = lineRuleNames.some((name) => {
          const pattern = LINE_RULES[name]
          if (pattern.test(line)) {
            hits[name] += 1
            return true
          }
          return false
        })
        if (!dropped) kept.push(line)
      }
      out = kept.join('\n')
    }
    return out
  }

  const walk = (value) => {
    if (typeof value === 'string') return applyString(value)
    if (Array.isArray(value)) return value.map(walk)
    if (value !== null && typeof value === 'object') {
      const out = {}
      for (const key of Object.keys(value).sort()) out[key] = walk(value[key])
      return out
    }
    return value
  }

  return { value: walk(payload), hits, rules: [...ruleNames] }
}

/**
 * 深比较两份已规范化的产物。
 * @returns `{ equal: boolean, diffs: {path: string, expected: unknown, actual: unknown}[] }`
 */
export function deepDiff(expected, actual, options = {}) {
  const maxDiffs = options.maxDiffs ?? 20
  const diffs = []
  const walk = (left, right, path) => {
    if (diffs.length >= maxDiffs) return
    if (typeof left === 'string' && typeof right === 'string') {
      if (left !== right) diffs.push({ path, expected: left, actual: right })
      return
    }
    if (Array.isArray(left) && Array.isArray(right)) {
      if (left.length !== right.length) {
        diffs.push({ path: `${path}.length`, expected: left.length, actual: right.length })
      }
      const length = Math.min(left.length, right.length)
      for (let i = 0; i < length && diffs.length < maxDiffs; i += 1) walk(left[i], right[i], `${path}[${i}]`)
      return
    }
    if (left !== null && right !== null && typeof left === 'object' && typeof right === 'object') {
      const keys = new Set([...Object.keys(left), ...Object.keys(right)])
      for (const key of [...keys].sort()) {
        if (diffs.length >= maxDiffs) return
        const hasLeft = Object.prototype.hasOwnProperty.call(left, key)
        const hasRight = Object.prototype.hasOwnProperty.call(right, key)
        if (!hasLeft || !hasRight) {
          diffs.push({ path: `${path}.${key}`, expected: hasLeft ? left[key] : '<missing>', actual: hasRight ? right[key] : '<missing>' })
          continue
        }
        walk(left[key], right[key], `${path}.${key}`)
      }
      return
    }
    if (left !== right) diffs.push({ path, expected: left, actual: right })
  }
  walk(expected, actual, '$')
  return { equal: diffs.length === 0, diffs }
}
