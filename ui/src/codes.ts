import raw from '../codes.json'

/**
 * 空态/离线文案的唯一真相（`ui/codes.json`）。
 *
 * 为什么要单独一个 json：这四句话是判据 A-10 的对象（四种空态四句不同），
 * 而且是**唯一一处**允许出现"没有数据"措辞的地方——散在组件里就没人能一次改齐。
 * 构建期可 grep，运行期只读。
 */
export const codes = raw

/** 把 `{name}` 占位替换成实参；缺失的占位保留原文，**绝不填默认数字**。 */
export function fill(template: string, vars: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (whole, key: string) =>
    key in vars ? String(vars[key]) : whole,
  )
}
