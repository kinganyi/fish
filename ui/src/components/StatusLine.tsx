import type { Win } from '../api/types'
import { codes } from '../codes'
import { num } from '../format'
import { isFiltered, scopeLabel, SCOPE_ALL, type Scope } from '../store/scope'
import { useWorkbench } from '../store/workbench'
import { WIN_LABEL } from '../windows'

/**
 * 常驻「当前切片」状态行（97 §A2 改法三）。
 *
 * 96 §11-7 删掉了"全部/全部项目"入口，代价是用户筛完不知道在哪退出。
 * 这一行就是那个 0 记忆负担的出口：显示当前刀 + 窗口 + 条数，点一下即取消。
 *
 * 窗口名来自 `windows.ts` 的**唯一一张表**：这里多写一份 `'近 24 小时'`，
 * 页签一的 ⓘ 就会在改文案时与状态行漂开（R-09 的来源之一）。
 */
export function StatusLine({
  scope,
  win,
  total,
}: {
  scope: Scope
  win: Win
  total: number | null
}) {
  const setScope = useWorkbench((state) => state.setScope)
  const filtered = isFiltered(scope)
  return (
    <div className="border-b border-n3 px-4 py-2 text-meta text-n6">
      <button
        type="button"
        onClick={() => setScope(SCOPE_ALL)}
        disabled={!filtered}
        className={`w-full rounded px-1 py-0.5 text-left ${
          filtered ? 'hover:bg-n2 hover:text-n7 active:bg-n3' : 'cursor-default'
        }`}
        title={filtered ? '点一下取消筛选' : undefined}
        data-rail=""
      >
        <span className="text-n6">当前切片：</span>
        <span className="font-name text-n7">{scopeLabel(scope)}</span>
        <span className="text-n6"> · {WIN_LABEL[win]}</span>
      </button>
      <div className="mt-0.5 flex items-center justify-between gap-1 px-1">
        <span>
          本刀 <span className="font-mono text-n7">{num(total)}</span> 条
        </span>
        {filtered ? <span className="text-accent">{codes.clearFilter}</span> : null}
      </div>
    </div>
  )
}
