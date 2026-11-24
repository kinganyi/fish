import { useCallback, useEffect, useState } from 'react'
import { AgentAccess } from './AgentAccess'
import { Push } from './Push'
import {
  SETTINGS_PAGES,
  readSettingsPage,
  settingsPageOf,
  writeSettingsPage,
  type SettingsPageId,
} from './pages'

/**
 * 设置页 —— **它自己排二级导航**（`200px 导航 + 内容`）。
 *
 * 为什么要有二级导航（用户 2026-11-24 拍板）：设置里现在有两件互不相干的事——
 * 「宿主接没接入」（时间尺度：天到月，动作是重探/装配置）与「推送有没有用」（尺度：
 * 分钟到周，动作是调口味）。它们挤在一屏里，用户永远在找「刚才那个开关去哪了」。
 *
 * 结构与纪律：
 *  1. **外层两列不变**（`52px` 窄条 + 内容，见 `Workbench.tsx`）：顶级页签是产品一级导航，
 *     二级导航只在设置内部生效，两级不能混成一个。
 *  2. 二级导航项是**数据**（`SETTINGS_PAGES`），加一页 = 加一条；选中态三件叠
 *     （左边 2px 竖线 + 底色 + 字色/字重）——只靠底色在 200px 里不够显眼。
 *  3. **深链** `?page=settings&sub=push`：刷新后仍停在推送页；回写只动 `sub` 这一个键
 *     （`?api=` 里的冒号必须原样，见 `pages.ts` 的第三条纪律）。
 *  4. 子页各自负责自己的数据读取与空态：宿主页的「探测于」在宿主页里，推送页的
 *     「数据截至」在推送页里——**两个时刻不能混成一个横幅**（它们本来就是两次请求）。
 */
export function Settings() {
  const [sub, setSub] = useState<SettingsPageId>(() => readSettingsPage())

  const pick = useCallback((id: SettingsPageId) => {
    setSub(id)
    writeSettingsPage(id)
  }, [])

  // 地址栏被外部改了（后退/前进、手改 URL）时跟着变；本页自己回写不会触发这个事件。
  useEffect(() => {
    const sync = () => setSub(readSettingsPage())
    window.addEventListener('popstate', sync)
    window.addEventListener('hashchange', sync)
    return () => {
      window.removeEventListener('popstate', sync)
      window.removeEventListener('hashchange', sync)
    }
  }, [])

  const current = settingsPageOf(sub)

  return (
    <main className="flex min-h-0 flex-col overflow-hidden bg-n1" aria-label="设置">
      <header className="flex flex-wrap items-baseline justify-between gap-4 border-b border-n3 bg-n0 px-4 py-3">
        <div className="min-w-0">
          <h1 className="text-section font-head text-n7">设置</h1>
          <p className="mt-0.5 truncate text-meta text-n6">{current.hint}</p>
        </div>
      </header>

      <div
        className="grid min-h-0 flex-1"
        style={{ gridTemplateColumns: '200px minmax(0, 1fr)' }}
      >
        <nav
          aria-label="设置二级导航"
          className="min-h-0 overflow-y-auto border-r border-n3 bg-n1 px-1 py-2"
        >
          <ul className="space-y-0.5">
            {SETTINGS_PAGES.map((page) => {
              const selected = page.id === sub
              return (
                <li key={page.id}>
                  <button
                    type="button"
                    aria-current={selected ? 'page' : undefined}
                    data-settings-page={page.id}
                    title={page.hint}
                    onClick={() => pick(page.id)}
                    className={`flex w-full min-w-0 flex-col items-start gap-0.5 border-l-2 px-3 py-2 text-left transition-colors active:bg-n3 ${
                      selected
                        ? 'border-l-accent bg-accent-soft'
                        : 'border-l-transparent hover:bg-n2'
                    }`}
                  >
                    <span
                      className={`block w-full truncate text-sub ${
                        selected ? 'font-name text-accent' : 'text-n7'
                      }`}
                    >
                      {page.label}
                    </span>
                    <span className="block w-full truncate text-meta text-n6">{page.hint}</span>
                  </button>
                </li>
              )
            })}
          </ul>
        </nav>

        {/*
          第二个 grid 单元是**子页的容器**，它必须是 `flex flex-col`：子页根用 `flex-1`/`h-full`
          要有个能「撑到 100% 高」的父级。原来是块级 + `overflow-hidden`，于是子页高度退化成
          内容高度、被这里裁掉——1280×720 下 Agent 接入的刷新按钮直接落在视口外（评审 🟠-1）。
          裁切只留一层（本容器），子页内部各自滚自己的区域。
        */}
        <div className="flex min-h-0 min-w-0 flex-col overflow-hidden">
          {sub === 'push' ? <Push /> : <AgentAccess />}
        </div>
      </div>
    </main>
  )
}
