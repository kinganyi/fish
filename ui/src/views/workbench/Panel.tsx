import { Tabs } from '../../components/Tabs'
import { useWorkbench } from '../../store/workbench'
import { TabLedger } from './TabLedger'
import { TabNeedYou } from './TabNeedYou'

/**
 * 右栏 = 判断面（97 §A4/§A5）：页签一答"现在需要我做什么"，页签二答"这一条背后是谁"。
 *
 * 宽度由 `Workbench` 的栅格给到 320（设计允许 320–400 的**下限**）——加窄条后把省下的
 * 40px 让给中栏，正文区才是这屏的主角（96 §5.3：看整体比看单条高频）。
 */
export function Panel() {
  const tab = useWorkbench((state) => state.tab)
  const setTab = useWorkbench((state) => state.setTab)
  return (
    <aside className="flex min-w-0 flex-col border-l border-n3 bg-n0" aria-label="判断面">
      <Tabs tab={tab} onChange={setTab} />
      <div className="min-h-0 flex-1 overflow-y-auto">
        {tab === 'need' ? <TabNeedYou /> : <TabLedger />}
      </div>
    </aside>
  )
}
