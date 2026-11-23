/**
 * 读口契约的类型（与 `p0/fish_workbench_api.py` 一一对应，手写、无 codegen）。
 *
 * 纪律：所有「可能没有」的读数都是 `null`，**不是 0**——
 * 0 是读数，「空」是状态，「缺」是能力，「判不到」是证据不足，四者文案必须不同（96 §七）。
 *
 * 关于 `partial`：**本期不支持**（QA F-8）。读口侧原来有一个 `Sources.state()`
 * 会返回 `"partial"`，但它没有任何可构造的输入（全文件无人调用，任何一个源失败都走
 * `missing` 信封），UI 也没有"部分可用该显示什么"的口径。口径没定就不发明状态，
 * 所以后端方法已删、这里也不再声明这个状态。详见 `ui/README.md` §六。
 */

export type AvailState =
  | 'ok'
  | 'empty'
  | 'missing'
  | 'error'
  | 'unreachable'

export interface Availability {
  state: AvailState
  detail: string | null
}

export interface SourceStat {
  source: string
  ok: boolean
  rows: number | null
  error: string | null
  ms: number | null
}

export interface Envelope<T> {
  schema: string
  asOf: string
  availability: Availability
  data: T
  sources: SourceStat[]
  /** 错误信封（`ok:false`）时才有的三段 */
  ok?: boolean
  code?: string
  hint?: string
  action?: string
}

export type ActorKind = 'citizen' | 'session' | 'system' | 'none'

export interface Actor {
  name: string
  labelKind: ActorKind
  citizenId: string | null
  host: string
  sessionTag: string
}

export interface ToolInfo {
  name: string
  action: string
  ok: boolean | null
}

export interface PathInfo {
  dir: string
  abs: string
  raw: string
  label: string
}

export type Src = 'tool' | 'post' | 'push' | 'event'
export type Cls = 'tool' | 'fold' | 'post' | 'push' | 'memory' | 'system'

export interface StreamItem {
  key: string
  src: Src
  cls: Cls
  rk: number
  ts: string
  sk: string
  actor: Actor
  project: string | null
  text: string
  tool: ToolInfo | null
  path: PathInfo | null
  meta: Record<string, unknown>
  fold?: { count: number; items: StreamItem[] }
  related?: Record<string, unknown>
}

export interface Counts {
  total: number | null
  totalKnown: boolean
  returned: number
  rawFetched: number
}

export interface FoldStat {
  groups: number
  covered: number
  thresholdSec: number
  minItems: number
  displayed: number
  displayedRaw: number
}

export interface StreamScope {
  kind: 'all' | 'project' | 'project-none' | 'board'
  project: string | null
  board: number | null
  label: string
  win: Win
  since: string
}

export interface StreamData {
  items: StreamItem[]
  cursor: string
  topCursor: string
  counts: Counts
  fold: FoldStat
  scope: StreamScope
  window: { win: Win; since: string }
}

export type Win = '24h' | '7d' | 'all'

/**
 * `/api/meta` 的载荷（R-09）：读口自述，**不是**信封。
 *
 * 为什么要它：看这屏之前得能确认"我在看哪个读口、它实现了哪些端点、读的是哪个库"。
 * `capabilities` 是**已实现**的端点清单（不是计划），所以"非空"本身就是判据。
 */
export interface MetaData {
  schema: string
  schemas: string[]
  capabilities: string[]
  db: { index: string; corpus: string | null }
  readOnly: boolean
  serverPid: number
  asOf: string
}

export interface ProjectFacet {
  name: string
  label: string
  count: number
  lastTouchedAt: string | null
  isUnattributed: boolean
  bySource: Record<string, number>
}

export interface BoardFacet {
  id: number
  label: string
  scopeKey: string | null
  scope: string
  periodKey: string
  memberCount: number
  runningCount: number
  postCount: number
  lastPostAt: string | null
  scopeKind: string
}

export interface FacetsData {
  projects: ProjectFacet[]
  unattributed: ProjectFacet | null
  boards: BoardFacet[]
  boardsOtherOpen: number
  window: { win: Win; since: string; periodKey: string; total: number }
}

export interface Motion {
  inMotion: number
  seen24h: number
  lastActionAt: string | null
  windowMinutes: number
  quantifier: string
  inMotionSince: string
}

export interface StintProject {
  name: string
  count: number
  oldestDays: number | null
}

export interface IdleItem {
  label: string
  sessionTag: string
  doing: string
  autoSource: string
  lastSeenAt: string
  bucket: 'auto' | 'declared' | 'unclear'
}

export interface PushTop {
  code: string
  pushed: number
  accepted: number
  ignored: number
  pending: number
  rate: number | null
  lastPushedAt: string | null
}

export interface PushStats {
  total: number | null
  accepted: number | null
  ignored: number | null
  pending: number | null
  decided: number | null
  acceptRate: number | null
  channels: { channel: string; count: number }[]
  top: PushTop[]
  rateDenominator: string
}

export interface ToolCount {
  name: string
  count: number
}

export interface HotItem {
  code: string
  title: string | null
  count: number
  lastAccessedAt: string | null
}

export interface StatsData {
  window: { days: number; since: string }
  motion: Motion
  openStints: { total: number; projects: StintProject[]; oldestDays: number | null }
  idleDeclared: { auto: number; declared: number; unclear: number; total: number; items: IdleItem[] }
  push: PushStats
  tools: {
    in24h: number
    in24hByName: ToolCount[]
    cumulative: number
    cumulativeByName: ToolCount[]
  }
  citizens: {
    total: number
    active24h: number
    byKind: { kind: string; count: number }[]
    byHost: { host: string | null; count: number }[]
  }
  hotAccess: { window: HotItem[]; allTime: HotItem[] }
}

export interface CitizenProfile {
  displayName: string | null
  kind: string | null
  role: string | null
  host: string | null
  status: string | null
  createdAt: string | null
  lastSeenAt: string | null
}

export interface SessionProfile {
  present: boolean
  running: boolean
  boardId?: number | null
  status?: string | null
  doing?: string
  autoSource?: string | null
  surface?: string
  autoSurface?: string
  surfaceMismatch?: boolean
  lastSeenAt?: string | null
}

export interface EntryActor {
  label: string
  labelKind: ActorKind
  citizenId: string | null
  sessionTag: string
  host: string
  hostBasis: string
  citizen: CitizenProfile | null
  session: SessionProfile
}

export interface LedgerTool {
  key: string
  toolName: string
  action: string
  pathLabel: string
  ok: boolean | null
  ts: string
  project: string | null
  sessionTag: string
}

export interface LedgerEvent {
  key: string
  type: string
  ts: string
  code: string
}

export interface EntryData {
  entry: StreamItem
  actor: EntryActor
  ledger: {
    tools: LedgerTool[]
    events: LedgerEvent[]
    days: { date: string; count: number }[]
    postCount: number
  }
  related: { project: string | null; board: { id: number; label: string } | null; code: string | null }
}

/**
 * 宿主接入层（`/api/hosts`，`p0/fish_hosts.py` 的投影）。
 *
 * 三态在这里**必须分开**（96 §七）：`yes` 已接 / `no` 未接 / `unverified` 判不到。
 * 「有配置文件」与「fish 接上了」是两件事：`configState` 说的是前者，`fishPresence` 说的是后者。
 * `none` 是**没找到**（不是判不到）；读不出内容才是 `unreadable`。
 */
export type HostCapState = 'yes' | 'no' | 'unverified'
export type FishPresence = 'both' | 'hooks' | 'mcp' | 'none'
/**
 * 配置落点的四档：`ready` 可读 / `missing` 不存在 / `unreadable` 读不出 /
 * `path_unresolved` **路径没解析出来**（如家目录含 `<用户名>`、`%VAR%` 没展开）。
 * 最后一档是「判不到」，**不是**「不存在」：此时 `configPath` 为 `null`，
 * `configReason` 会写清哪个占位符没解析。
 */
export type HostConfigState = 'ready' | 'missing' | 'unreadable' | 'path_unresolved'

export interface HostCapability {
  state: HostCapState
  /** 后端给的中文标签（已接 / 未接 / 未验证）——前端不自己造词。 */
  label: string
  evidence: string
}

export interface HostConfigInfo {
  role: 'hooks' | 'mcp'
  label: string
  /** `null` = 路径没解析出来（占位符未展开）——**不是**「文件不存在」。 */
  path: string | null
  format: 'json' | 'toml' | 'yaml'
  locator: string
  exists: boolean
  size: number | null
  mtime: string | null
  parsed: boolean
  parseError: string | null
  parseSoft: boolean
  fishHit: boolean
  detail: string
  scripts: string[]
  missingScripts: string[]
  /** 解析时容忍过的宿主自定义标签（如 `!!js`）：值原样保留、未求值。 */
  toleratedTags: string[]
  /** 路径没解析出来时的原因（此时 `path` 为 `null`）：判不到 ≠ 不存在。 */
  pathUnresolved: string | null
}

export interface HostEvidence {
  id: string
  label: string
  /** `null` = 判不到（证据不足），**不是**失败。 */
  ok: boolean | null
  detail: string
}

export interface HostLastSeenSource {
  source: string
  at: string
  rows: number
}

export interface HostStatus {
  id: string
  display: string
  /** 会话键前缀（机器证据词表）；空串 = 这台宿主不产生 fish 会话键。 */
  protocol: string
  protocolNote: string | null
  aliases: string[]
  pseudo: boolean
  installed: boolean
  home: string | null
  homeExists: boolean
  configPath: string | null
  configExists: boolean
  configFormat: string | null
  configState: HostConfigState
  configReason: string | null
  fishPresence: FishPresence
  hooksSupported: boolean
  events: string[]
  matcherDialect: { kind: string; note: string } | null
  collector: string[]
  capabilities: Record<string, HostCapability>
  configs: HostConfigInfo[]
  evidence: HostEvidence[]
  version: string | null
  /** `version` 的来源类别（`asar` / `json` / `text`；`electron` = 只读到 Electron 版本）。 */
  versionKind: string | null
  versionSource: string | null
  versionNote: string | null
  /** Electron 运行时版本：**不是**应用版本，只作为「判不到应用版本」时的说明。 */
  electronVersion: string | null
  electronSource: string | null
  lastSeenAt: string | null
  lastSeenSources: HostLastSeenSource[]
  /** `null` = 判不到（库里没记录），不是「在跑」。 */
  stalled: boolean | null
  stalledNote: string | null
  allowedActions: string[]
  blockedActions: string[]
  blockedReasons: Record<string, string>
  notes: string[]
}

export interface HostsData {
  items: HostStatus[]
  generatedAt: string
  consistency: { ok: boolean; warnings: string[]; checked: Record<string, unknown> }
}
