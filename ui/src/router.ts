import type { BoardFacet, Win } from './api/types'
import { scopeFromParam, scopeToParam, type Scope, type Tab } from './store/scope'

/**
 * hash 深链（97 §B6）：`#/w?scope=project:anyi-studio&win=24h&sel=tool:40424&tab=ledger`。
 *
 * 为什么用 hash 而不是 path：生产由读口 `--dist` 同源发静态，hash 不触发服务端请求，
 * **静态服务不需要任何 rewrite 规则**。判据：刷新后「切片 / 窗口 / 选中 / 页签」四件事同时复现。
 */
export interface Route {
  scope: Scope
  win: Win
  sel: string | null
  tab: Tab
}

const WINDOWS: Win[] = ['24h', '7d', 'all']

export function readRoute(boards: BoardFacet[] = []): Route {
  const hash = window.location.hash.replace(/^#/, '')
  const queryIndex = hash.indexOf('?')
  const params = new URLSearchParams(queryIndex >= 0 ? hash.slice(queryIndex + 1) : '')
  const win = params.get('win') as Win | null
  const tab = params.get('tab')
  const sel = params.get('sel')
  return {
    scope: scopeFromParam(params.get('scope') || 'all', boards),
    win: win && WINDOWS.includes(win) ? win : '24h',
    sel: sel || null,
    tab: tab === 'ledger' ? 'ledger' : 'need',
  }
}

export function writeRoute(route: Route): void {
  const params = new URLSearchParams()
  params.set('scope', scopeToParam(route.scope))
  params.set('win', route.win)
  if (route.sel) {
    params.set('sel', route.sel)
    params.set('tab', route.tab)
  }
  const next = `#/w?${params.toString()}`
  if (next !== window.location.hash) {
    window.history.replaceState(null, '', next)
  }
}
