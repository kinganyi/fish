import { getJson, postJson, postJsonBody, requestJson } from './client'
import type {
  EntryData,
  FacetsData,
  HostStatus,
  HostsData,
  MetaData,
  PushEventsData,
  PushOverviewData,
  PushPreset,
  PushPresetResult,
  PushWriteResult,
  StatsData,
  StreamData,
  Win,
} from './types'

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

/**
 * 设置页的宿主投影（宿主接入层一期：**只读**）。
 *
 * 三个端点都是**现探**（读口不缓存探测结果）：
 *   · 列表只含「探测到安装痕迹」的宿主；
 *   · 详情与刷新返回同一份结构——刷新用 POST，因为它是**允许发生的动作**，
 *     不是「再读一次」。刷新不写盘（判据见 `p0/tools/verify_hosts.py` ⑤）。
 */
export function fetchHosts(signal?: AbortSignal) {
  return getJson<HostsData>('/api/hosts', undefined, signal)
}

export function fetchHost(id: string, signal?: AbortSignal) {
  return getJson<HostStatus>(`/api/hosts/${encodeURIComponent(id)}`, undefined, signal)
}

export function refreshHost(id: string, signal?: AbortSignal) {
  return postJson<HostStatus>(`/api/hosts/${encodeURIComponent(id)}/refresh`, signal)
}

/**
 * 设置页 · 推送（诊断只读 + 两个窄写动作）。
 *
 * 四个端点的分工：
 *   · `overview`：整页数据（`totals` 来自后端 `fish.push_stats`，前端**不重算**）；
 *   · `events`：最近推送（按 id 倒序；`reaction` 不传=不过滤，`pending`=只要待判）；
 *   · `config`：写**一个**白名单键（后端判白名单/类型/范围，审计落 `_audit/`）；
 *   · `preset`：止血 `pause` / 恢复 `resume`。
 *
 * 写失败时后端回 400 `bad_key` / `bad_value`，前端把 `ApiError.hint` 原文贴出来
 * （**不翻译、不吞**：配置改错的后果比一句难看的话严重）。
 */
export function fetchPushOverview(days: number, signal?: AbortSignal) {
  return getJson<PushOverviewData>('/api/push/overview', { days }, signal)
}

export interface PushEventsQuery {
  limit?: number
  /** 空 = 不过滤；`inject` / `prompt` / `tool_after` 收窄到一个通道。 */
  channel?: string
  /**
   * 不传 = 不过滤；`pending` = 只要「待判」（库里 `reaction IS NULL`）；`accepted` / `ignored` 原样。
   *
   * **空串不是契约**（评审 🟠-2）：`buildUrl` 会跳过空串参数，后端 `parse_qs` 默认也丢空值，
   * 于是「空串=待判」这条契约在两端都到不了。改用显式哨兵 `pending`，两边都不用改
   * 「空值=不传」的全局语义。
   */
  reaction?: string
  signal?: AbortSignal
}

export function fetchPushEvents({ limit = 20, channel, reaction, signal }: PushEventsQuery = {}) {
  return getJson<PushEventsData>(
    '/api/push/events',
    { limit, channel, reaction },
    signal,
  )
}

export function postPushConfig(key: string, value: string | number | boolean, signal?: AbortSignal) {
  return postJsonBody<PushWriteResult>('/api/push/config', { key, value }, signal)
}

export function postPushPreset(preset: PushPreset, signal?: AbortSignal) {
  return postJsonBody<PushPresetResult>('/api/push/preset', { preset }, signal)
}
