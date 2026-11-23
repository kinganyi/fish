import { useState } from 'react'
import type { StreamItem } from '../api/types'
import { StreamRow } from './StreamRow'
import { Time } from './Time'

/**
 * 折叠行：服务端算好的 `fold` 字段，前端只渲染（两条规则：同会话连续工具动作 /
 * 同 code 连续推送，见 `p0/fish_workbench_api.py` 的 `fold_items`）。
 *
 * 为什么折叠必须存在：24h 里同一个会话能吞掉一半以上的动作、同一个 code 能被连推几十次，
 * 不折叠就看不见别人在干什么。展开后**原地显示、不跳位**（展开的是同一行的高度，不新开面板）。
 */
export function RowGroup({
  item,
  index,
  selectedKey,
  focused,
  onSelect,
}: {
  item: StreamItem
  index: number
  selectedKey: string | null
  focused: boolean
  onSelect: (key: string) => void
}) {
  const [open, setOpen] = useState(false)
  const count = item.fold?.count ?? 0
  const items = item.fold?.items ?? []
  const truncated = Boolean(item.meta.truncated)
  const isPush = item.meta.kind === 'push'
  const kindWord = isPush ? '连续推送' : '连续动作'

  return (
    <div
      className={`border-b border-n3/60 border-l-2 ${
        open ? 'border-l-n4 bg-n1' : 'border-l-transparent'
      }`}
    >
      <div
        className={`flex min-h-14 items-center gap-3 py-2 pr-3 pl-1 ${
          focused ? 'bg-n2/60' : 'hover:bg-n2/50'
        }`}
      >
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
          className="flex w-full items-center gap-3 rounded text-left active:bg-n3"
          title={open ? '收起这一组' : '展开这一组'}
        >
          <span aria-hidden="true" className="w-5 shrink-0 text-center font-mono text-sub text-n6">
            ⋯
          </span>
          <span className="min-w-0 flex-1">
            <span className="block truncate text-body text-n7">
              {item.text.replace('（展开）', '')}
              <span className="ml-2 rounded bg-n2 px-1 font-mono text-meta text-n7">
                {open ? '收起' : `展开 ${count}`}
              </span>
            </span>
            <span className="mt-0.5 block truncate font-mono text-meta text-n6">
              <Time ts={item.ts} /> · {item.project || '未归属'} · {kindWord}
            </span>
          </span>
        </button>
      </div>
      {open ? (
        <div className="border-l-2 border-n4 bg-n1" data-fold-open="true">
          {items.map((sub, offset) => (
            <StreamRow
              key={sub.key}
              item={sub}
              index={index * 1000 + offset}
              selected={sub.key === selectedKey}
              focused={false}
              nested
              onSelect={onSelect}
              onFocus={() => undefined}
            />
          ))}
          {truncated ? (
            <p className="px-6 py-2 text-meta text-n6">
              这一组只带回了前 {items.length} 条明细（其余在更早的翻页里）。
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
