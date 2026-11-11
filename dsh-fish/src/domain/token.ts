/**
 * token 计数与按字符截断（注入包 / 看板 / 候选三段共用的两个原语）。
 *
 * 为什么单独成模块：`countTokens` 是**契约**的一部分（`spec/injection.json` 的
 * `budgetRule`），而它被四个段与包尾预留同时使用。放在 `inject.ts` 里会让
 * `board.ts` / `push.ts` 反向依赖注入包（循环依赖），而循环依赖在 ESM 下的表现是
 * "某个常量在模块求值期还是 undefined"——最难查的那种。
 *
 * ## ⚠ 计的是**码点**，不是 UTF-16 码元（2026-10-27 · T03 实测抓到的真 bug）
 *
 * Python 的 `len(str)` 数的是**码点**；JS 的 `str.length` 数的是 UTF-16 码元。
 * 一个 astral 字符（如看板里的 `🔴` U+1F534）在 Python 里是 1，在 JS 里是 2。
 * 快照对拍实测：同一条看板文本 Python `<!-- 930/1600 -->`、TS `<!-- 931/1600 -->`——
 * 差的那 1 token 就是"一个 emoji 被数成两个"。这不只是计数偏差：预算裁剪的触发点
 * 由它决定，差 1 就可能"该裁的段没裁"。
 *
 * 所以本模块按码点计数（`Array.from` / 手写遍历），截断也按码点切（否则会在代理对
 * 中间切断，产出一个孤立代理项——两侧字节必然不等，且渲染出来是乱码）。
 *
 * @module dsh-fish/domain/token
 */

/** 字符串的**码点**长度（与 Python `len()` 同口径）。 */
export function codePointLength(text: string): number {
  let count = 0
  // for…of 按码点迭代（代理对会被当成一个字符）。
  for (const _ of text) {
    void _
    count += 1
  }
  return count
}

/** 按**码点**切片（与 Python 的 `text[start:end]` 同口径）。 */
export function sliceCodePoints(text: string, start: number, end?: number): string {
  const from = Math.max(0, Math.trunc(start))
  const to = end === undefined ? undefined : Math.max(from, Math.trunc(end))
  let index = 0
  let out = ''
  for (const ch of text) {
    if (index >= (to ?? Number.POSITIVE_INFINITY)) break
    if (index >= from) out += ch
    index += 1
  }
  return out
}

/**
 * token 计数（影子期口径 = Python `count_tokens` 的 **fallback 分支**）：
 * `int(len(text) * 1.2)`，`len` 是**码点**数。
 *
 * 为什么不是 tiktoken 精确值：TS 侧没有 cl100k_base。两条路的取舍写在
 * `NOTES-T02-delivery.md`：**对拍时两侧统一走这一档**（Python 侧把 `_ENC` 钉成 False），
 * 于是"预算裁剪在哪儿触发"也成了可对拍的事实。
 *
 * D3 裁决（2026-10-27）：这条口径写进了 `spec/injection.json` 的 `budgetRule`，
 * 并由 `scripts/check-spec.mjs` 逐位比对实现——换心期若引入真实 tokenizer，
 * **必须同时改 spec 与两侧实现**，否则视为契约破坏（判据会红）。
 */
export function countTokens(text: string): number {
  return Math.trunc(codePointLength(text) * 1.2)
}

/** `budgetRule.rule` 的合法取值（与 spec 里的字符串一一对应）。 */
export const BUDGET_RULE_FALLBACK = 'fallback'

/** 按**字符（码点）**截断（不是 token），并把换行压成空格（复刻 `_clip_chars`）。 */
export function clipChars(text: string | null | undefined, limit: number): string {
  const s = (text ?? '').trim().replace(/\r/g, ' ').replace(/\n/g, ' ')
  const length = codePointLength(s)
  if (limit <= 0 || length <= limit) return s
  return `${sliceCodePoints(s, 0, Math.max(0, limit - 1))}…`
}
