import type { BoardFacet, Win } from './api/types'
import { DEFAULT_PAGE, PAGES, type PageId } from './pages'
import { scopeFromParam, scopeToParam, type Scope, type Tab } from './store/scope'

/**
 * 深链（97 §B6）：`#/w?scope=project:anyi-studio&win=24h&sel=tool:40424&tab=ledger`。
 *
 * 为什么用 hash 而不是 path：生产由读口 `--dist` 同源发静态，hash 不触发服务端请求，
 * **静态服务不需要任何 rewrite 规则**。判据：刷新后「切片 / 窗口 / 选中 / 页签」四件事同时复现。
 *
 * **顶级页**（现场 / 设置）另走地址栏查询串 `?page=settings`（用户 2026-11-23 的验收口径）：
 *  · 页是「看哪一屏」，切片/窗口是「这一屏里看哪一段」，两者的深链形状不同，混进一个
 *    query 会让两种语义互相污染（切页时不该顺带改切片）；
 *  · 查询串里的 `?api=<读口基址>` 必须原样保留——那是静态托管时的**唯一**基址来源。
 */
export interface Route {
  scope: Scope
  win: Win
  sel: string | null
  tab: Tab
}

const WINDOWS: Win[] = ['24h', '7d', 'all']

function isPageId(value: string | null): value is PageId {
  return Boolean(value) && PAGES.some((page) => page.id === value)
}

/** 当前顶级页：查询串优先（`?page=settings`），其次 hash（兼容手写链接），否则缺省页。 */
export function readPage(): PageId {
  const fromQuery = new URLSearchParams(window.location.search).get('page')
  if (isPageId(fromQuery)) return fromQuery
  const hash = window.location.hash.replace(/^#/, '')
  const queryIndex = hash.indexOf('?')
  if (queryIndex >= 0) {
    const fromHash = new URLSearchParams(hash.slice(queryIndex + 1)).get('page')
    if (isPageId(fromHash)) return fromHash
  }
  return DEFAULT_PAGE
}

/**
 * 回写顶级页（`replaceState`，不进历史栈）。
 *
 * **只动 `page` 这一个键，且逐字节保留其它参数**：`?api=http://127.0.0.1:8790` 里的冒号
 * 若被 `URLSearchParams` 重新编码成 `%3A`，地址栏就会变一个样（虽然还能读）——那不是
 * 「回写一个键」，那是**改了用户的 URL**。所以这里按 `&` 切原串、只增删 `page=`。
 */
export function writePage(page: PageId): void {
  const raw = window.location.search.replace(/^\?/, '')
  const kept = raw
    .split('&')
    .filter(Boolean)
    .filter((part) => part.split('=')[0] !== 'page')
  if (page !== DEFAULT_PAGE) kept.push(`page=${page}`)
  const search = kept.length > 0 ? `?${kept.join('&')}` : ''
  const next = window.location.pathname + search + window.location.hash
  const current = window.location.pathname + window.location.search + window.location.hash
  if (next !== current) {
    window.history.replaceState(null, '', next)
  }
}

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
