import type { StreamItem } from '../api/types'
import { Time } from './Time'

const ICON: Record<string, string> = {
  exec: '⌘',
  write: '✒',
  edit: '✎',
  post: '▤',
  push: '✉',
  memory: '◼',
  system: '•',
}

const TYPE_WORD: Record<string, string> = {
  tool: '工具动作',
  post: '看板登记',
  push: '推送',
  memory: '记忆事件',
  system: '系统事件',
  fold: '连续动作',
}

function iconOf(item: StreamItem): string {
  if (item.src === 'tool') {
    if (item.tool?.name === 'edit') return ICON.edit
    if (item.tool?.name === 'write') return ICON.write
    return ICON.exec
  }
  if (item.src === 'post') return ICON.post
  if (item.src === 'push') return ICON.push
  if (item.cls === 'memory') return ICON.memory
  return ICON.system
}

/** 主行三段固定：谁（加粗）/ 做了什么 / 何时（等宽右对齐）。 */
function splitActor(item: StreamItem): { name: string; rest: string } {
  const name = item.actor.name
  if (name && item.text.startsWith(name)) {
    return { name, rest: item.text.slice(name.length) }
  }
  return { name: '', rest: item.text }
}

export function StreamRow({
  item,
  index,
  selected,
  focused,
  nested = false,
  onSelect,
  onFocus,
}: {
  item: StreamItem
  index: number
  selected: boolean
  focused: boolean
  nested?: boolean
  onSelect: (key: string) => void
  onFocus: () => void
}) {
  const { name, rest } = splitActor(item)
  const failed = item.tool?.ok === false
  const typeWord = TYPE_WORD[item.cls] || TYPE_WORD[item.src] || item.src
  return (
    <div
      role="listitem"
      tabIndex={0}
      aria-selected={selected}
      aria-label={`${typeWord}：${item.text}`}
      data-key={item.key}
      data-selected={selected ? 'true' : 'false'}
      onClick={() => onSelect(item.key)}
      onFocus={onFocus}
      onKeyDown={(event) => {
        if (event.key === 'Enter') {
          event.preventDefault()
          onSelect(item.key)
        }
      }}
      className={`flex min-h-14 cursor-pointer items-center gap-3 border-b border-n3/60 py-2 pr-3 active:bg-n3 ${
        nested ? 'pl-6' : 'pl-1'
      } ${
        selected
          ? 'border-l-2 border-l-accent bg-accent-soft/60'
          : focused
            ? 'border-l-2 border-l-n4 bg-n2/60'
            : 'border-l-2 border-l-transparent hover:bg-n2/50'
      }`}
    >
      <span
        aria-hidden="true"
        className="w-5 shrink-0 text-center font-mono text-sub text-n6"
        title={typeWord}
      >
        {iconOf(item)}
      </span>
      <div className="min-w-0 flex-1">
        <p className="truncate text-body text-n7" title={item.text}>
          {name ? <span className="font-name">{name}</span> : null}
          <span>{rest}</span>
          {failed ? (
            <span className="ml-2 rounded border border-err/40 px-1 text-meta text-err">未成功</span>
          ) : null}
        </p>
        <p className="mt-0.5 flex items-center gap-2 truncate font-mono text-meta text-n6">
          <Time ts={item.ts} />
          <span aria-hidden="true">·</span>
          <span className="font-sans">{item.project || '未归属'}</span>
          <span aria-hidden="true">·</span>
          <span className="font-sans">{typeWord}</span>
          {item.actor.labelKind === 'session' ? (
            <span className="rounded bg-n2 px-1 font-sans">会话</span>
          ) : null}
          {item.actor.labelKind === 'none' ? (
            <span className="rounded bg-n2 px-1 font-sans">无公民归属</span>
          ) : null}
        </p>
      </div>
      <span className="sr-only">{index}</span>
    </div>
  )
}
