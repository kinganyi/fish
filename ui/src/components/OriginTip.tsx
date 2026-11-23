import type { SourceStat } from '../api/types'

/**
 * 每个数字旁边的「来源 + 口径」入口（判据 A-12 的落点）。
 *
 * 用原生 `<details>`：无 JS 状态、键盘可达、点开才展开。删掉它这屏就变成
 * 一堆没出处的数字——用户没法验证任何读数。
 */
export function OriginTip({
  sources,
  cap,
  detail,
}: {
  sources: SourceStat[]
  cap: string
  detail: string
}) {
  return (
    <details className="relative inline-block">
      <summary
        className="cursor-pointer list-none rounded px-1 text-meta text-n6 hover:bg-n2 hover:text-n7 focus-visible:outline-2 active:bg-n3"
        title="来源与口径"
        aria-label={`${cap} 的来源与口径`}
      >
        ⓘ
      </summary>
      <div className="absolute right-0 z-10 mt-1 w-64 rounded border border-n3 bg-n0 p-2 text-meta leading-5 text-n6 shadow-sm">
        <p className="mb-1 font-name text-n7">{cap}</p>
        <p className="mb-1">{detail}</p>
        <ul className="space-y-0.5 font-mono">
          {sources.length === 0 ? <li>（后端未返回来源明细）</li> : null}
          {sources.map((source) => (
            <li key={source.source}>
              {source.ok ? 'ok' : '缺'} · {source.source}
              {source.rows !== null ? ` · 读 ${source.rows} 行` : ''}
              {source.error ? ` · ${source.error}` : ''}
            </li>
          ))}
        </ul>
      </div>
    </details>
  )
}
