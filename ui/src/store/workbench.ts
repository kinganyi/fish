import { create } from 'zustand'
import { ApiError, NetworkError } from '../api/client'
import { fetchEntry, fetchFacets, fetchStats, fetchStream } from '../api/endpoints'
import type { AvailState, EntryData, Envelope, FacetsData, SourceStat, StatsData, StreamData, StreamItem, Win } from '../api/types'
import { SCOPE_ALL, scopeToParam, type Scope, type Tab } from './scope'
import { STATS_DAYS } from '../windows'

export type LoadState = 'idle' | 'loading' | 'ok' | 'error'
/**
 * 一次刷新的结果。`missing` 与 `error` **必须分开**（评审 R-08）：
 * `missing` 是"这块能力没接（缺表/缺库）",重试不会好；`error` 是"这次读失败了",重试可能好。
 * `network` 是"读口根本没起来"——只有它参与整屏空态的判定。
 */
type FetchOutcome = 'ok' | 'missing' | 'error' | 'network'

const PAGE = 40
const BUFFER_MAX = 200

/** 网络失败与业务失败必须分开：前者整屏「读口未起」，后者按三段文案（97 §D5）。 */
function isNetwork(error: unknown): boolean {
  return error instanceof NetworkError
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.code}：${error.hint}`
  if (error instanceof NetworkError) return `连不上读口（${error.message}）`
  return (error as Error)?.message || '未知错误'
}

/** `availability.state="missing"` = 「缺」（缺表/缺库）——由后端给原因，前端不推断。 */
function missingDetail(envelope: Envelope<unknown>): string | null {
  if (envelope.availability?.state === 'missing') {
    return envelope.availability.detail || '数据源缺失（后端未给原因）'
  }
  return null
}

/**
 * 信封 → 区块的 availability（F-7：不再把 `missing` 折成 `error`）。
 *
 * 两者是不同的东西：`error` 是"这次读失败了，重试可能好"；`missing` 是"这块能力没接
 * （缺表/缺库），重试也不会好"。折成一个，中栏/右栏就永远只会说"重试"——那正是
 * 评审 R-08 说的「左栏对了、中栏右栏把 missing 说成 error」。
 * 所以四条链（facets / stream / stats / entry）**各自**落一份 availability。
 */
function availabilityOf(envelope: Envelope<unknown>): AvailState {
  return envelope.availability?.state ?? 'ok'
}

/**
 * 「库是空的」的**唯一判据**（QA K1）。
 *
 * `facets.window.total === 0` 只说明**这个窗口**里没行；要断言"库空"还得看累计
 * （`stats.tools.cumulative === 0`）。原来左栏只看前半句、中栏看两半句，
 * 于是停了一整天再打开时：左栏说"这个库还是空的"，中栏说"这个时间窗里没有行为"。
 * 两处都只调这个函数，不允许各自推导——判据见 `p0/tools/verify_v4.py`。
 */
export function isEmptyDb(
  facets: FacetsData | null,
  stats: StatsData | null,
): boolean {
  if (!facets || !stats) return false
  return facets.window.total === 0 && stats.tools.cumulative === 0
}

interface WorkbenchState {
  scope: Scope
  win: Win
  tab: Tab
  selectedKey: string | null
  focusIndex: number

  facets: FacetsData | null
  facetsState: LoadState
  /** `/api/facets` 的 `availability.state`：`missing` 要单独保留，不能折成 `error`（F-7）。 */
  facetsAvailability: AvailState
  /** 当前枚举值由构造点决定：ok/empty/missing/error/unreachable 全都有落点（详见 AsyncBoundary 注释）。 */
  streamState: LoadState
  streamAvailability: AvailState
  stats: StatsData | null
  statsState: LoadState
  statsAvailability: AvailState
  /** `/api/stats` 的 `sources[]`——页签一每块的「来源 + 口径」从这里来（A-12）。 */
  statsSources: SourceStat[]
  entry: EntryData | null
  entryState: LoadState
  entryAvailability: AvailState
  entryError: string | null

  items: StreamItem[]
  topCursor: string
  cursor: string
  /** 本页第一响应里的 `window.since`：翻页时原样回传（R-01：窗口不随翻页滑动）。 */
  winSince: string
  totalKnown: number | null
  foldStat: StreamData['fold'] | null
  asOf: string | null

  buffer: StreamItem[]
  atTop: boolean
  boot: 'loading' | 'ok' | 'unreachable'
  lastError: string | null

  init: () => Promise<void>
  retry: () => Promise<void>
  setScope: (scope: Scope) => void
  setWin: (win: Win) => void
  setTab: (tab: Tab) => void
  select: (key: string | null) => Promise<void>
  refreshFacets: () => Promise<FetchOutcome>
  refreshStats: () => Promise<FetchOutcome>
  reloadStream: () => Promise<FetchOutcome>
  pollNew: () => Promise<void>
  loadMore: () => Promise<void>
  flushBuffer: () => void
  setAtTop: (atTop: boolean) => void
  setFocus: (index: number) => void
  moveFocus: (delta: number) => void
}

// 并发护栏：切切片/窗口时旧请求可能晚到，用自增令牌丢弃（不清游标、不串数据）。
let streamToken = 0
let entryToken = 0
let pollBusy = false
let moreBusy = false

function mergeUnique(fresh: StreamItem[], existing: StreamItem[]): StreamItem[] {
  const seen = new Set(existing.map((item) => item.key))
  return [...fresh.filter((item) => !seen.has(item.key)), ...existing]
}

export const useWorkbench = create<WorkbenchState>((set, get) => ({
  scope: SCOPE_ALL,
  win: '24h',
  tab: 'need',
  selectedKey: null,
  focusIndex: 0,

  facets: null,
  facetsState: 'idle',
  facetsAvailability: 'ok',
  streamState: 'idle',
  streamAvailability: 'ok',
  stats: null,
  statsState: 'idle',
  statsAvailability: 'ok',
  statsSources: [],
  entry: null,
  entryState: 'idle',
  entryAvailability: 'ok',
  entryError: null,

  items: [],
  topCursor: '',
  cursor: '',
  winSince: '',
  totalKnown: null,
  foldStat: null,
  asOf: null,

  buffer: [],
  atTop: true,
  boot: 'loading',
  lastError: null,

  init: async () => {
    set({ boot: 'loading', lastError: null })
    const outcomes = await Promise.all([
      get().refreshFacets(),
      get().refreshStats(),
      get().reloadStream(),
    ])
    // 三路都连不上 = 读口没起来（整屏文案，不显示任何数字）；否则只是某块不可用。
    const allNetwork = outcomes.every((outcome) => outcome === 'network')
    set({ boot: allNetwork ? 'unreachable' : 'ok' })
  },

  retry: async () => {
    await get().init()
  },

  setScope: (scope) => {
    set({ scope })
    void get().reloadStream()
  },

  setWin: (win) => {
    // 三件事一起走：流水换窗、左栏计数换窗、右栏页签一的时间窗跟着换
    // （R-09：ⓘ 里的「近 N」必须与用户此刻选的窗口同源，否则说明文字在骗人）。
    set({ win })
    void get().reloadStream()
    void get().refreshFacets()
    void get().refreshStats()
  },

  setTab: (tab) => {
    // 切页签**不改选中**（选中是"当前这条"，页签是"看它的哪个面"，96 §八）。
    set({ tab })
  },

  select: async (key) => {
    set({ selectedKey: key, tab: key ? 'ledger' : 'need' })
    if (!key) {
      set({ entry: null, entryState: 'idle', entryAvailability: 'ok', entryError: null })
      return
    }
    const token = ++entryToken
    set({ entryState: 'loading', entryAvailability: 'ok', entryError: null })
    try {
      const envelope = await fetchEntry(key)
      if (token !== entryToken) return
      const missing = missingDetail(envelope)
      if (missing) {
        // 「缺」不是「错」：页签二要说"缺 /api/entry"，不是说"重试"（R-08）。
        set({ entry: null, entryState: 'ok', entryAvailability: 'missing', entryError: missing })
        return
      }
      set({
        entry: envelope.data,
        entryState: 'ok',
        entryAvailability: availabilityOf(envelope),
        entryError: null,
      })
    } catch (error) {
      if (token !== entryToken) return
      set({ entry: null, entryState: 'error', entryAvailability: 'error', entryError: describe(error) })
    }
  },

  refreshFacets: async () => {
    try {
      const envelope = await fetchFacets(get().win)
      const availability = availabilityOf(envelope)
      const missing = missingDetail(envelope)
      if (missing) {
        // 「缺」不是「错」：state 保持 ok、availability 记 missing，Block 才会说"缺 facets"。
        set({ facetsState: 'ok', facetsAvailability: 'missing', lastError: missing })
        return 'missing'
      }
      set({ facets: envelope.data, facetsState: 'ok', facetsAvailability: availability })
      return 'ok'
    } catch (error) {
      set({ facetsState: 'error', facetsAvailability: 'error', lastError: describe(error) })
      return isNetwork(error) ? 'network' : 'error'
    }
  },

  refreshStats: async () => {
    try {
      // `days` 由当前窗口推出（`windows.ts` 的唯一一张表）：推送/热推/热搜的时间窗
      // 与流水窗口同步，页签一 ⓘ 里的「近 N」才与用户看到的一致（R-09）。
      const envelope = await fetchStats(STATS_DAYS[get().win], 5)
      const missing = missingDetail(envelope)
      if (missing) {
        set({ stats: null, statsState: 'ok', statsAvailability: 'missing', lastError: missing })
        return 'missing'
      }
      set({
        stats: envelope.data,
        statsState: 'ok',
        statsAvailability: availabilityOf(envelope),
        statsSources: envelope.sources,
      })
      return 'ok'
    } catch (error) {
      set({ statsState: 'error', statsAvailability: 'error', lastError: describe(error) })
      return isNetwork(error) ? 'network' : 'error'
    }
  },

  reloadStream: async () => {
    const token = ++streamToken
    const { scope, win } = get()
    // 切片/窗口变了就**立刻清空**：留着上一刀的行会让"这刀下面有什么"读错。
    set({
      streamState: 'loading',
      streamAvailability: 'ok',
      items: [],
      topCursor: '',
      cursor: '',
      winSince: '',
      buffer: [],
      focusIndex: 0,
    })
    try {
      const envelope = await fetchStream({ win, scope: scopeToParam(scope), limit: PAGE })
      if (token !== streamToken) return 'ok'
      const missing = missingDetail(envelope)
      if (missing) {
        set({ items: [], streamState: 'ok', streamAvailability: 'missing', lastError: missing })
        return 'missing'
      }
      set({
        streamState: 'ok',
        streamAvailability: availabilityOf(envelope),
        items: envelope.data.items,
        topCursor: envelope.data.topCursor,
        cursor: envelope.data.cursor,
        // 记下这一刀的窗口起点：后面每一页都带同一个 since（R-01）。
        winSince: envelope.data.window?.since || '',
        totalKnown: envelope.data.counts.total,
        foldStat: envelope.data.fold,
        asOf: envelope.asOf,
      })
      return 'ok'
    } catch (error) {
      if (token !== streamToken) return 'error'
      set({ streamState: 'error', streamAvailability: 'error', lastError: describe(error) })
      return isNetwork(error) ? 'network' : 'error'
    }
  },

  pollNew: async () => {
    if (pollBusy) return
    const { topCursor, scope, win, streamState, winSince } = get()
    if (!topCursor || streamState !== 'ok') return
    pollBusy = true
    try {
      const envelope = await fetchStream({
        win,
        scope: scopeToParam(scope),
        since: topCursor,
        limit: PAGE,
        // 轮询也钉在同一刀上：窗口滑走时新条目会与上面的行错开一个 since。
        wsince: winSince || undefined,
      })
      const fresh = envelope.data.items
      if (!fresh.length) return
      const topCursorNext = envelope.data.topCursor || fresh[0].sk
      if (get().atTop) {
        set((state) => ({
          items: mergeUnique(fresh, state.items),
          topCursor: topCursorNext,
        }))
      } else {
        // 不在顶部：只缓冲 + 计数（上限 200），**绝不自动滚顶**（会在读正文时把内容抽走）。
        set((state) => ({
          buffer: [...fresh, ...state.buffer].slice(0, BUFFER_MAX),
          topCursor: topCursorNext,
        }))
      }
    } catch {
      // 轮询失败不弹错：下一拍会再试；真正的读口故障由首屏三请求判定。
    } finally {
      pollBusy = false
    }
  },

  loadMore: async () => {
    if (moreBusy) return
    const { cursor, scope, win, streamState, winSince } = get()
    if (!cursor || streamState !== 'ok') return
    moreBusy = true
    try {
      const envelope = await fetchStream({
        win,
        scope: scopeToParam(scope),
        before: cursor,
        limit: PAGE,
        wsince: winSince || undefined,
      })
      if (!envelope.data.items.length) return
      set((state) => ({
        items: mergeUnique(state.items, envelope.data.items),
        cursor: envelope.data.cursor,
      }))
    } catch {
      // 同上：翻页失败不制造假空态。
    } finally {
      moreBusy = false
    }
  },

  flushBuffer: () => {
    const { buffer } = get()
    if (!buffer.length) {
      set({ atTop: true })
      return
    }
    set((state) => ({
      items: mergeUnique(buffer, state.items),
      buffer: [],
      atTop: true,
    }))
  },

  setAtTop: (atTop) => set({ atTop }),
  setFocus: (index) => set({ focusIndex: index }),
  moveFocus: (delta) => {
    const { focusIndex } = get()
    const count = get().items.length
    if (!count) return
    const next = Math.max(0, Math.min(count - 1, focusIndex + delta))
    set({ focusIndex: next })
  },
}))
