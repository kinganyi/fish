import type { Tab } from '../store/scope'

/**
 * 右栏两个页签：页签一「要你做的 / 整体」，页签二「这一条」。
 *
 * 两条规矩（96 §八）：
 *  · 点页签二而无选中 → 页签二写"还没选中任何一条行为"，不是空面板；
 *  · 切页签**不改选中**（选中是"当前这条"，页签是"看它的哪个面"）。
 */
export function Tabs({ tab, onChange }: { tab: Tab; onChange: (tab: Tab) => void }) {
  return (
    <div role="tablist" aria-label="右栏页签" className="flex border-b border-n3">
      <button
        type="button"
        role="tab"
        aria-selected={tab === 'need'}
        onClick={() => onChange('need')}
        className={`flex-1 border-b-2 px-3 py-3 text-sub active:bg-n3 ${
          tab === 'need'
            ? 'border-accent font-name text-n7'
            : 'border-transparent text-n6 hover:bg-n2 hover:text-n7'
        }`}
      >
        要你做的 / 整体
      </button>
      <button
        type="button"
        role="tab"
        aria-selected={tab === 'ledger'}
        onClick={() => onChange('ledger')}
        className={`flex-1 border-b-2 px-3 py-3 text-sub active:bg-n3 ${
          tab === 'ledger'
            ? 'border-accent font-name text-n7'
            : 'border-transparent text-n6 hover:bg-n2 hover:text-n7'
        }`}
      >
        这一条
      </button>
    </div>
  )
}
