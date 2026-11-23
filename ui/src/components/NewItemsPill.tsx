/**
 * 顶部「N 条新行为（回到最新）」。
 *
 * 铁律（96 §六规则 5）：2 秒轮询到新条目时**绝不自动滚顶**——
 * 用户正在读中间某条，页面自己跳回顶部等于把内容抽走。
 * 只有点了这颗按钮才插入并滚顶。
 */
export function NewItemsPill({ count, onClick }: { count: number; onClick: () => void }) {
  if (count <= 0) return null
  return (
    <div className="pointer-events-none absolute inset-x-0 top-2 z-10 flex justify-center">
      <button
        type="button"
        onClick={onClick}
        className="pointer-events-auto rounded-full border border-accent/40 bg-accent-soft px-3 py-1 text-sub font-name text-accent shadow-sm hover:bg-accent-soft active:bg-n3"
      >
        {count} 条新行为（回到最新）
      </button>
    </div>
  )
}
