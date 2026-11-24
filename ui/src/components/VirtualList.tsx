import { useVirtualizer } from '@tanstack/react-virtual'
import { useEffect, useRef, type ReactNode } from 'react'

/**
 * 唯一的虚拟滚动封装（97 §D2 / §D4）。
 *
 * 为什么必须虚拟化：24h 窗口有 3,404 条行为，全渲染就是 3,404 个 DOM 节点；
 * 判据 A-11 的机检口径是「页面里 `[data-row]` 节点数必须 < 200」——
 * 关掉它节点数会变回三位数以上，判据必须变红（否则说明根本没测到真东西）。
 *
 * 行高动态（折叠展开会变高）用 `measureElement` + `data-index` 让 react-virtual
 * 自己量：单行 56px 是**估算值**，实际高度以测量为准，滚动不跳位。
 */
/**
 * 虚拟滚动的**测试开关**：`?novirtual=1` 时全量渲染（不虚拟化）。
 *
 * 这是判据 A-11 的**变异探针**：验收脚本用同一个页面、同一个 DOM 探针，只换这一个参数，
 * 「DOM 行节点数」必须从绿翻红——证明那条判据测的是**虚拟滚动本身**，而不是
 * 「列表本来就短所以怎么都小于 200」。用户正常路径不会带这个参数（不带等价于关闭）。
 *
 * 为什么放在产品代码里而不是测试里替换组件：A-11 要验的是**真实页面**里的渲染路径，
 * 在测试里另搭一个页面就变成了测测试。这个开关只有一行判断，且默认关闭。
 */
function noVirtualRequested(): boolean {
  if (typeof window === 'undefined') return false
  return new URLSearchParams(window.location.search).get('novirtual') === '1'
}

export function VirtualList({
  count,
  estimateSize,
  renderRow,
  getKey,
  onScroll,
  className,
  focusIndex,
  scrollTopNonce,
}: {
  count: number
  estimateSize: number
  renderRow: (index: number) => ReactNode
  getKey?: (index: number) => string
  onScroll?: (target: HTMLDivElement) => void
  className?: string
  focusIndex?: number
  scrollTopNonce?: number
}) {
  const parentRef = useRef<HTMLDivElement>(null)
  const virtualizer = useVirtualizer({
    count,
    getScrollElement: () => parentRef.current,
    estimateSize: () => estimateSize,
    overscan: 8,
    getItemKey: getKey ? (index) => getKey(index) : undefined,
  })

  // 「回到最新」：只在用户点了那颗按钮时滚顶（非 nonce 变化不滚，绝不自动抢滚动）。
  useEffect(() => {
    if (scrollTopNonce) parentRef.current?.scrollTo({ top: 0, behavior: 'smooth' })
  }, [scrollTopNonce])

  // 键盘焦点跟行走：行在 DOM 里是虚拟的，要先让 react-virtual 把它滚进窗口再聚焦。
  useEffect(() => {
    if (focusIndex === undefined || focusIndex < 0 || focusIndex >= count) return
    virtualizer.scrollToIndex(focusIndex, { align: 'auto' })
    const node = parentRef.current?.querySelector<HTMLElement>(
      `[data-index="${focusIndex}"] [data-key]`,
    )
    node?.focus({ preventScroll: true })
  }, [focusIndex, count, virtualizer])

  // `data-count` = 列表**持有**多少条（不是 DOM 里渲染了多少条）。A-11 拿它判
  // 「DOM 行数 < 持有条数」——少了这个数，列表只载 40 条时「< 200」恒成立，判据就没有牙。
  const containerProps = {
    ref: parentRef,
    onScroll: (event: React.UIEvent<HTMLDivElement>) => onScroll?.(event.currentTarget),
    className: `overflow-y-auto overflow-x-hidden ${className || ''}`,
    role: 'list',
    'aria-label': '行为流水',
    'data-count': count,
  } as const

  if (noVirtualRequested()) {
    return (
      <div {...containerProps} data-virtualized="off">
        {Array.from({ length: count }, (_, index) => (
          <div key={getKey ? getKey(index) : index} data-index={index} data-row="">
            {renderRow(index)}
          </div>
        ))}
      </div>
    )
  }

  return (
    <div {...containerProps} data-virtualized="on">
      <div style={{ height: virtualizer.getTotalSize(), position: 'relative', width: '100%' }}>
        {virtualizer.getVirtualItems().map((virtualRow) => (
          <div
            key={virtualRow.key}
            data-index={virtualRow.index}
            data-row=""
            ref={virtualizer.measureElement}
            style={{
              position: 'absolute',
              top: 0,
              left: 0,
              width: '100%',
              transform: `translateY(${virtualRow.start}px)`,
            }}
          >
            {renderRow(virtualRow.index)}
          </div>
        ))}
      </div>
    </div>
  )
}
