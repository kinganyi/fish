/**
 * Python 文本口径的两个小原语（`str()` / `repr()`）——**逐字节对拍**要的就是这个。
 *
 * 为什么需要它们：Python 的 f-string 把 `None` 印成字面 `None`、`True`/`False` 首字母大写；
 * 而 `%r`（`repr`）把字符串印成**单引号**形态。JS 的 `String()` / `JSON.stringify()` 三处
 * 全不一样。对拍时这些差异会以"看起来像实现不同"的红灯出现，追下去却是格式化口径——
 * 所以口径本身收在这里，一处改、三族共用。
 *
 * @module dsh-fish/core/pytext
 */

/** Python `str(value)` 的渲染口径（`None` → `None`，布尔 → `True`/`False`）。 */
export function pyText(value: unknown): string {
  if (value === null || value === undefined) return 'None'
  if (typeof value === 'boolean') return value ? 'True' : 'False'
  return String(value)
}

/**
 * Python `repr(str)` 的渲染口径（单引号、转义 `\\` / `'` / 换行 / 制表）。
 *
 * 与 CPython 一致的两条小规则：字符串里有 `'` 且没有 `"` 时改用双引号；其余情况用单引号。
 * 不可打印字符只处理最常见的三个（`\n` `\r` `\t`）——回执里出现的也只会是它们。
 */
export function pyRepr(value: string): string {
  const text = String(value)
  const quote = text.includes("'") && !text.includes('"') ? '"' : "'"
  let out = ''
  for (const ch of text) {
    if (ch === '\\') out += '\\\\'
    else if (ch === quote) out += `\\${quote}`
    else if (ch === '\n') out += '\\n'
    else if (ch === '\r') out += '\\r'
    else if (ch === '\t') out += '\\t'
    else out += ch
  }
  return `${quote}${out}${quote}`
}
