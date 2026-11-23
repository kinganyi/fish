import { getJson, requestJson } from './client'
import type { EntryData, FacetsData, MetaData, StatsData, StreamData, Win } from './types'

/**
 * 四个端点的薄封装（参数名与读口契约一致，`97 §C`）。
 * 这里**不做任何加工/补默认值**——空态与「判不到」由视图按 `availability` 决定。
 */

export function fetchFacets(win: Win, signal?: AbortSignal) {
  return getJson<FacetsData>('/api/facets', { win }, signal)
}

export interface StreamQuery {
  win: Win
  scope: string
  limit?: number
  /** 游标：比它**新**（轮询方向）。 */
  since?: string
  /** 游标：比它**旧**（翻页方向）。 */
  before?: string
  /**
   * 页面固定窗口起点（R-01）：把第一页响应里的 `window.since` 原样带回。
   * 不传就按 `win` 现算——滑窗会让翻到末端时少 1–2 行（窗口把老行滑出去了）。
   */
  wsince?: string
  signal?: AbortSignal
}

export function fetchStream({ win, scope, limit = 40, since, before, wsince, signal }: StreamQuery) {
  return getJson<StreamData>(
    '/api/stream',
    { win, scope, limit, since, before, wsince },
    signal,
  )
}

export function fetchStats(days = 1, top = 5, signal?: AbortSignal) {
  return getJson<StatsData>('/api/stats', { days, top }, signal)
}

export function fetchEntry(id: string, signal?: AbortSignal) {
  return getJson<EntryData>('/api/entry', { id }, signal)
}

/** `/api/meta`：读口自述（无 `data` 信封，字段直接是顶层）。 */
export function fetchMeta(signal?: AbortSignal) {
  return requestJson<MetaData>('/api/meta', undefined, signal)
}
