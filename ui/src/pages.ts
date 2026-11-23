/**
 * 顶级页签清单 —— **产品一级导航的唯一数据源**。
 *
 * 窄条（52px）里放的是「页」，不是「功能入口」：加一页**只往这个数组加一条**，
 * `NavRail` 按数组渲染，不写死任何单个按钮。
 *
 * 三条纪律：
 *  1. **只放已建好的页**：没做完的页签不要提前挂进来——画一个点不动的入口，等于给用户
 *     一个"够不着"的承诺（用户明确反对）。
 *  2. 页签名与数据契约名分开：页签叫「现场」，读口 schema 仍叫 `fish-workbench/1`
 *     （契约锚点，改名要连带改测试与外部脚本）。
 *  3. `group: 'bottom'` 的页贴窄条**底部**（用户 2026-11-23 的裁决：「设置」是第二个面，
 *     不属于现场那套四栏，位置在最下面，不跟「现场」抢顶部）。
 */
export type PageId = 'scene' | 'settings'

/** `top` = 顶部主流程；`bottom` = 贴底的工具面（设置）。 */
export type PageGroup = 'top' | 'bottom'

export interface PageDef {
  /** 深链与 `data-page` 用的稳定 id（不进中文，免得 %-编码）。 */
  id: PageId
  /** 窄条里显示的名字（2 字以内才放得下 52px 的宽度）。 */
  label: string
  /** 窄条上方的小标记（无图标库，用几何字符；纯装饰，`aria-hidden`）。 */
  glyph: string
  /** 一句话说明，挂 `title`——窄条放不下正文，但鼠标停一下要知道这是哪一页。 */
  hint: string
  /** 窄条里的分组：`top` 从上往下排，`bottom` 贴底（见 NavRail）。 */
  group: PageGroup
}

export const PAGES: PageDef[] = [
  { id: 'scene', label: '现场', glyph: '◉', hint: '此刻正在发生的事', group: 'top' },
  { id: 'settings', label: '设置', glyph: '⚙', hint: '宿主接入：装没装、接没接、接的哪条通道', group: 'bottom' },
]

export const DEFAULT_PAGE: PageId = 'scene'

/** 分组后的页清单（`top` 在前、`bottom` 贴底），保持数组内顺序。 */
export function pagesOf(group: PageGroup): PageDef[] {
  return PAGES.filter((page) => page.group === group)
}
