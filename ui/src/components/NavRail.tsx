import { pagesOf, type PageId } from '../pages'

/**
 * 顶级页签窄条（52px）——**产品的第 4 列，不是装饰**。
 *
 * 四件事定死：
 *  1. 它渲染的是 `PAGES` 这份**页面清单**；加一页 = 加一条数据，不写死单个按钮。
 *  2. 选中态**不是纯色块**：左侧 2px 竖线 + 底色 + 文字色 + 字重，四者同时变——
 *     只靠底色在 52px 的窄条上几乎看不出来（用户 2026-11-23 拍的验收口径）。
 *  3. 未做的页**不画**：`PAGES` 里没有就不渲染，不做"灰掉的占位入口"。
 *  4. **分组**：`top` 组从上往下；`bottom` 组用 `mt-auto` 贴到窄条底部
 *     （「设置」是工具面，用户要求它在最下面，不跟「现场」抢顶部）。
 *
 * 52px 是 48–56 的中间值：2 字中文（12px/字）加左右留白放得下，又不至于把中栏挤到 55% 以下。
 */
export function NavRail({
  current,
  onPick,
}: {
  current: PageId
  onPick: (id: PageId) => void
}) {
  const groups = [
    { key: 'top', pages: pagesOf('top'), className: '' },
    { key: 'bottom', pages: pagesOf('bottom'), className: 'mt-auto' },
  ] as const

  return (
    <nav aria-label="顶级页签" className="flex h-full min-w-0 flex-col border-r border-n3 bg-n1">
      {groups.map((group) => (
        <ul
          key={group.key}
          role="tablist"
          aria-orientation="vertical"
          aria-label={group.key === 'top' ? '产品一级导航' : '产品一级导航（贴底）'}
          className={`flex flex-col ${group.className}`}
        >
          {group.pages.map((page) => {
            const selected = page.id === current
            return (
              <li key={page.id}>
                <button
                  type="button"
                  role="tab"
                  aria-selected={selected}
                  data-page={page.id}
                  title={page.hint}
                  onClick={() => onPick(page.id)}
                  className={`flex h-12 w-full flex-col items-center justify-center gap-0.5 border-l-2 text-meta transition-colors active:bg-n3 ${
                    selected
                      ? 'border-l-accent bg-accent-soft font-name text-accent'
                      : 'border-l-transparent text-n6 hover:bg-n2 hover:text-n7'
                  }`}
                >
                  <span aria-hidden="true" className="font-mono text-sub leading-none">
                    {page.glyph}
                  </span>
                  <span className="leading-none">{page.label}</span>
                </button>
              </li>
            )
          })}
        </ul>
      ))}
    </nav>
  )
}
