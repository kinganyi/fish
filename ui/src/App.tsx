import { useCallback, useEffect, useState } from 'react'
import { apiBaseInfo } from './api/client'
import { codes } from './codes'
import { DEFAULT_PAGE, type PageId } from './pages'
import { readPage, readRoute, writePage, writeRoute } from './router'
import { useWorkbench } from './store/workbench'
import { Workbench } from './views/workbench/Workbench'

/**
 * 2 秒轮询（流）/ 10 秒（左栏）/ 60 秒（判断面）。
 *
 * 切到后台标签页时**降到 30 秒**（97 §D3）：不是完全停掉——回来时不该看到半小时前的读数。
 * 轮询只喂"新条目"与聚合，不改选中、不滚顶（96 §六规则 5）。
 */
function usePolling(callback: () => void, periodMs: number) {
  useEffect(() => {
    let lastHiddenFire = 0
    const id = window.setInterval(() => {
      if (document.hidden) {
        const now = Date.now()
        if (now - lastHiddenFire < 30_000) return
        lastHiddenFire = now
      }
      callback()
    }, periodMs)
    return () => window.clearInterval(id)
  }, [callback, periodMs])
}

function Polling() {
  const pollNew = useWorkbench((state) => state.pollNew)
  const refreshFacets = useWorkbench((state) => state.refreshFacets)
  const refreshStats = useWorkbench((state) => state.refreshStats)
  usePolling(() => void pollNew(), 2_000)
  usePolling(() => void refreshFacets(), 10_000)
  usePolling(() => void refreshStats(), 60_000)
  return null
}

/** 键盘：`/` 聚焦切片器 · `↑↓/j k` 行走 · `Enter` 选中 · `Esc` 一步返回 · `1/2` 页签 · `g/G` 栈顶栈底。 */
function useKeyboard() {
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      if (
        target &&
        (target.tagName === 'INPUT' ||
          target.tagName === 'TEXTAREA' ||
          target.tagName === 'SELECT' ||
          target.isContentEditable)
      ) {
        return
      }
      const state = useWorkbench.getState()
      const items = state.items
      switch (event.key) {
        case '/': {
          event.preventDefault()
          document.querySelector<HTMLElement>('[data-rail]')?.focus()
          break
        }
        case 'j':
        case 'ArrowDown': {
          event.preventDefault()
          state.moveFocus(1)
          break
        }
        case 'k':
        case 'ArrowUp': {
          event.preventDefault()
          state.moveFocus(-1)
          break
        }
        case 'Enter': {
          const current = items[state.focusIndex]
          if (current) {
            event.preventDefault()
            void state.select(current.key)
          }
          break
        }
        case 'Escape': {
          if (state.selectedKey || state.tab === 'ledger') {
            event.preventDefault()
            void state.select(null)
            state.setTab('need')
          }
          break
        }
        case '1': {
          state.setTab('need')
          break
        }
        case '2': {
          state.setTab('ledger')
          break
        }
        case 'g': {
          state.setFocus(0)
          break
        }
        case 'G': {
          state.setFocus(Math.max(0, items.length - 1))
          break
        }
        default:
          break
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])
}

/** 深链：挂载读一次 → 之后每次状态变化回写 hash（replaceState，不进历史栈）。 */
function useDeepLink() {
  useEffect(() => {
    const route = readRoute()
    useWorkbench.setState({ scope: route.scope, win: route.win, tab: route.tab })
    void useWorkbench
      .getState()
      .init()
      .then(() => {
        if (route.sel) void useWorkbench.getState().select(route.sel)
      })
  }, [])

  useEffect(() => {
    return useWorkbench.subscribe((state) => {
      writeRoute({
        scope: state.scope,
        win: state.win,
        sel: state.selectedKey,
        tab: state.tab,
      })
    })
  }, [])

  useEffect(() => {
    const onHash = () => {
      const boards = useWorkbench.getState().facets?.boards ?? []
      const route = readRoute(boards)
      useWorkbench.setState({
        scope: route.scope,
        win: route.win,
        tab: route.tab,
        selectedKey: null,
      })
      void useWorkbench.getState().reloadStream()
      if (route.sel) void useWorkbench.getState().select(route.sel)
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
}

/**
 * 读口未起 = **整屏一条说明**：不显示任何数字、不显示骨架数字（判据 A-2）。
 *
 * 启动命令按默认端口写（不带 `--dist`，也不带端口数字），因此页面里唯一的数字是路径
 * `p0` 里的 `0`。判据的机检口径是「命令里的数字不算读数」（QA B3 已确认这一条成立），
 * 所以这里不写"页面内数字数 = 0"那种会被 `p0` 打破的话。
 *
 * 基址来源是 `same-origin` 且页面不是读口自己发的时候（静态托管却没带 `?api=`），
 * 多给一句**不含数字**的提示——否则用户只看到"读口没起来"，不知道是自己漏了参数。
 */
function Unreachable({ onRetry }: { onRetry: () => void }) {
  const sameOriginOnly = apiBaseInfo().source === 'same-origin'
  return (
    <div className="flex h-full items-center justify-center p-8">
      <div className="max-w-xl">
        <h1 className="text-section font-head text-n7">读口没有起来</h1>
        <p className="mt-2 text-body leading-6 text-n6">{codes.unreachable}</p>
        {sameOriginOnly ? (
          <p className="mt-2 text-meta leading-5 text-n6">
            如果前端由独立静态服务托管（不是读口自己发的），地址栏要带 <code>?api=</code>{' '}
            指向读口基址；见 <code>ui/README.md</code> 的「两种拓扑」。
          </p>
        ) : null}
        <button
          type="button"
          onClick={onRetry}
          className="mt-4 rounded border border-n4 bg-n0 px-3 py-1.5 text-sub text-n7 hover:bg-n2 active:bg-n3"
        >
          {codes.retry}
        </button>
      </div>
    </div>
  )
}

export default function App() {
  const boot = useWorkbench((state) => state.boot)
  const retry = useWorkbench((state) => state.retry)
  // 顶级页（现场 / 设置）：深链 `?page=settings`，刷新后仍停在这一屏（用户验收口径）。
  const [page, setPage] = useState<PageId>(() => readPage())
  const pickPage = useCallback((id: PageId) => {
    setPage(id)
    writePage(id)
  }, [])
  useDeepLink()
  useKeyboard()

  useEffect(() => {
    const onHash = () => setPage(readPage())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  if (boot === 'unreachable') {
    return <Unreachable onRetry={() => void retry()} />
  }
  if (boot === 'loading') {
    return (
      <div className="flex h-full items-center justify-center p-8 text-sub text-n6" aria-busy="true">
        {codes.loading}
      </div>
    )
  }
  return (
    <>
      <Polling />
      <Workbench page={page ?? DEFAULT_PAGE} onPick={pickPage} />
    </>
  )
}
