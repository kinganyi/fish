import { useMemo, useState } from 'react'
import type { Win } from '../../api/types'
import { codes, fill } from '../../codes'
import { num } from '../../format'
import { isFiltered, scopeLabel, SCOPE_ALL } from '../../store/scope'
import { isEmptyDb, useWorkbench } from '../../store/workbench'
import { WIN_LABEL } from '../../windows'
import { Missing } from '../../components/AsyncBoundary'
import { NewItemsPill } from '../../components/NewItemsPill'
import { RowGroup } from '../../components/RowGroup'
import { StreamRow } from '../../components/StreamRow'
import { VirtualList } from '../../components/VirtualList'

const WIN_TABS: Win[] = ['24h', '7d', 'all']

/**
 * 中栏：五类行为混排的一条只读时间线（全屏唯一正文区）。
 *
 * 排序/游标/折叠都在服务端算好（`sk` 单键 + `fold` 字段）——前端只渲染，
 * 这里再算一次就是第二份领域实现。
 */
export function Stream() {
  const items = useWorkbench((state) => state.items)
  const buffer = useWorkbench((state) => state.buffer)
  const streamState = useWorkbench((state) => state.streamState)
  const streamAvailability = useWorkbench((state) => state.streamAvailability)
  const streamError = useWorkbench((state) => state.lastError)
  const totalKnown = useWorkbench((state) => state.totalKnown)
  const foldStat = useWorkbench((state) => state.foldStat)
  const scope = useWorkbench((state) => state.scope)
  const win = useWorkbench((state) => state.win)
  const selectedKey = useWorkbench((state) => state.selectedKey)
  const focusIndex = useWorkbench((state) => state.focusIndex)
  const facets = useWorkbench((state) => state.facets)
  const stats = useWorkbench((state) => state.stats)
  const setWin = useWorkbench((state) => state.setWin)
  const setScope = useWorkbench((state) => state.setScope)
  const select = useWorkbench((state) => state.select)
  const setFocus = useWorkbench((state) => state.setFocus)
  const setAtTop = useWorkbench((state) => state.setAtTop)
  const loadMore = useWorkbench((state) => state.loadMore)
  const flushBuffer = useWorkbench((state) => state.flushBuffer)
  const reloadStream = useWorkbench((state) => state.reloadStream)

  const [scrollTopNonce, setScrollTopNonce] = useState(0)

  // 「空库」与「这一刀没数据」是两件事：前者是状态，后者可能是筛选或时间窗造成的。
  // 判据与左栏**同一份**（`isEmptyDb`），否则同一屏会说出两句互相打脸的话（QA K1）。
  const dbEmpty = useMemo(() => isEmptyDb(facets, stats), [facets, stats])

  const emptyVariant: 'db' | 'filtered' | 'window' = dbEmpty
    ? 'db'
    : isFiltered(scope)
      ? 'filtered'
      : 'window'

  const handleScroll = (target: HTMLDivElement) => {
    setAtTop(target.scrollTop < 8)
    if (target.scrollTop + target.clientHeight > target.scrollHeight - 360) {
      void loadMore()
    }
  }

  const keyOf = (index: number) => items[index]?.key || `row-${index}`

  return (
    <section className="relative flex min-w-0 flex-col bg-n0" aria-label="行为流水">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-n3 px-4 py-2">
        <h2 className="text-section font-head text-n7">流水</h2>
        <div className="flex items-center gap-1" role="group" aria-label="时间范围">
          {WIN_TABS.map((tab) => (
            <button
              key={tab}
              type="button"
              aria-pressed={win === tab}
              onClick={() => setWin(tab)}
              className={`rounded border px-2 py-1 text-meta active:bg-n3 ${
                win === tab
                  ? 'border-accent bg-accent-soft text-accent'
                  : 'border-n3 bg-n0 text-n6 hover:bg-n2 hover:text-n7'
              }`}
            >
              {WIN_LABEL[tab]}
            </button>
          ))}
        </div>
      </header>

      <div className="flex items-center gap-2 border-b border-n3/60 px-4 py-1 text-meta text-n6">
        <span>
          本刀 <span className="font-mono text-n7">{num(totalKnown)}</span> 条
        </span>
        {foldStat ? (
          <span title="折叠读数只统计当前返回的这一页，不是整个窗口">
            · 本页折叠 {foldStat.groups} 组，覆盖 {foldStat.covered} 条
          </span>
        ) : null}
        {buffer.length ? <span className="text-accent">· 有 {buffer.length} 条新的</span> : null}
      </div>

      <div className="relative min-h-0 flex-1">
        <NewItemsPill
          count={buffer.length}
          onClick={() => {
            flushBuffer()
            setScrollTopNonce((value) => value + 1)
          }}
        />

        {streamAvailability === 'missing' ? (
          // 「缺」不显示重试：重试不会好（R-08）。
          <Missing cap="/api/stream" detail={streamError} />
        ) : streamState === 'loading' || streamState === 'idle' ? (
          <div className="space-y-2 p-4" aria-busy="true" aria-label={codes.loading}>
            <div className="h-3 w-1/3 animate-pulse rounded bg-n2" />
            <div className="h-3 w-2/3 animate-pulse rounded bg-n2" />
            <div className="h-3 w-1/2 animate-pulse rounded bg-n2" />
          </div>
        ) : streamState === 'error' ? (
          <div className="m-4 space-y-2 rounded border border-n3 bg-n1 p-4 text-sub text-n6">
            <p className="text-n7">流水没读出来。</p>
            <p className="font-mono text-meta">{streamError}</p>
            <p>能做的：确认读口在跑，然后重试。</p>
            <button
              type="button"
              onClick={() => void reloadStream()}
              className="rounded border border-n4 bg-n0 px-2 py-1 text-meta text-n7 hover:bg-n2 active:bg-n3"
            >
              {codes.retry}
            </button>
          </div>
        ) : items.length === 0 ? (
          <div className="m-4 space-y-3 rounded border border-n3 bg-n1 p-4 text-sub leading-6 text-n6">
            {emptyVariant === 'db' ? <p>{codes.emptyDb}</p> : null}
            {emptyVariant === 'filtered' ? (
              <>
                <p>{fill(codes.filteredEmpty, { scope: scopeLabel(scope) })}</p>
                <button
                  type="button"
                  onClick={() => setScope(SCOPE_ALL)}
                  className="rounded border border-n4 bg-n0 px-2 py-1 text-meta text-n7 hover:bg-n2 active:bg-n3"
                >
                  {codes.clearFilter}
                </button>
              </>
            ) : null}
            {emptyVariant === 'window' ? (
              <>
                <p>{codes.windowEmpty}</p>
                <button
                  type="button"
                  onClick={() => setWin('7d')}
                  className="rounded border border-n4 bg-n0 px-2 py-1 text-meta text-n7 hover:bg-n2 active:bg-n3"
                >
                  {codes.windowWider}
                </button>
              </>
            ) : null}
          </div>
        ) : (
          <VirtualList
            className="h-full"
            count={items.length}
            estimateSize={56}
            getKey={keyOf}
            focusIndex={focusIndex}
            scrollTopNonce={scrollTopNonce}
            onScroll={handleScroll}
            renderRow={(index) => {
              const item = items[index]
              if (item.fold) {
                return (
                  <RowGroup
                    item={item}
                    index={index}
                    selectedKey={selectedKey}
                    focused={focusIndex === index}
                    onSelect={(key) => void select(key)}
                  />
                )
              }
              return (
                <StreamRow
                  item={item}
                  index={index}
                  selected={selectedKey === item.key}
                  focused={focusIndex === index}
                  onSelect={(key) => void select(key)}
                  onFocus={() => setFocus(index)}
                />
              )
            }}
          />
        )}
      </div>
    </section>
  )
}
