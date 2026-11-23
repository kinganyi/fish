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

  return (
    <div
      ref={parentRef}
      onScroll={(event) => onScroll?.(event.currentTarget)}
      className={`overflow-y-auto overflow-x-hidden ${className || ''}`}
      role="list"
      aria-label="行为流水"
    >
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
