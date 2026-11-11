/**
 * dsh-fish：fish（大肥鱼生命系统）在 DSH 上的官方原生实现。
 *
 * **影子期（当前）只做三件事**，全都与"能不能安全接管"有关：
 *   1. 契约门禁：spec 版本 / schema 指纹 / 宿主版本三处必须同时对上，否则抛错拒绝启用；
 *   2. 影子初始化：以 `mode=ro` 打开同一个 fish 库，禁止任何写路径；
 *   3. 人类命令：注册 `/fish shadow ...` 最小只读子集（宿主不允许插件给 `dsh` 加子命令，
 *      斜杠命令是唯一等价面）。
 *
 * **影子期明确不做**（与 `~/.dsh/hooks.json` 的 4 条 fish hook、`mcp__fish__*` 并存的安全前提）：
 *   · 不注册任何模型可见工具（同层同名注册会直接抛错）；
 *   · 不向模型上下文注入任何内容（否则双份注入且无法归因）；
 *   · 不写 fish 库（双写者会互相覆盖，而覆盖的表现是"数据莫名少了"）。
 *
 * @module dsh-fish
 */

import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'

import { runStartupGate, SUPPORTED_CONTRACT_VERSION, type StartupGateResult } from './core/contract.ts'
import { FishError } from './core/errors.ts'
import { detectHostVersion } from './core/host-version.ts'
import { createLogger, type LogLevel } from './core/logger.ts'
import { resolvePaths, type FishPaths } from './core/paths.ts'
import { loadSpec, type SpecBundle } from './ports/spec.ts'
import { registerFishCommand, type CommandDeps } from './surfaces/commands.ts'

/** 插件名（与 package.json 一致；宿主冲突报错会带它）。 */
export const name = 'dsh-fish'

/**
 * 影子期的模型面登记数——**恒为 0，且是判据**。
 * 为什么把它写进代码：影子期的纪律如果只写在文档里，下一个人加工具时不会看到；
 * 写成可断言的常量之后，`scripts/check-spec.mjs` 能直接把它钉住。
 */
export const MODEL_SURFACE = { toolRegistrations: 0, promptInjections: 0 } as const

/** 配置形状（`apply` 收到的已由 schemastery 补过默认值）。 */
export interface FishConfig {
  /** 影子期只允许 `shadow`；`active`（写路径）尚未实现，遇到即拒绝。 */
  readonly mode: string
  /** 契约层目录；空串 = 用包内 `spec/`。 */
  readonly specDir: string
  /** fish 家目录；空串 = 用包根兄弟目录 `p0/`。 */
  readonly fishHome: string
  /** 主库路径；空串 = `<fishHome>/data/index.sqlite`。 */
  readonly dbPath: string
  /** 注入包预算（影子期只用于对拍，不真注入）。 */
  readonly budgetTokens: number
  /** 探测不到宿主版本时是否拒绝启动（默认 `false`）。 */
  readonly strictHostVersion: boolean
  /** 是否注册 `/fish` 命令（默认 `true`）。 */
  readonly commandEnabled: boolean
  /** 日志级别。 */
  readonly logLevel: string
}

/** cordis 配置 schema：每个键都给默认值（缺省即影子期安全值）。 */
export const Config = z.object({
  mode: z.string().default('shadow'),
  specDir: z.string().default(''),
  fishHome: z.string().default(''),
  dbPath: z.string().default(''),
  budgetTokens: z.number().default(1600),
  strictHostVersion: z.boolean().default(false),
  commandEnabled: z.boolean().default(true),
  logLevel: z.string().default('info'),
})

const LOG_LEVELS: readonly LogLevel[] = ['debug', 'info', 'warn', 'error']

/** 把外部配置收敛成强类型配置（**非法值必须响亮失败**，不静默改默认）。 */
export function normalizeConfig(raw: Partial<FishConfig> = {}): FishConfig {
  const logLevel = typeof raw.logLevel === 'string' && raw.logLevel !== '' ? raw.logLevel : 'info'
  if (!LOG_LEVELS.includes(logLevel as LogLevel)) {
    throw new FishError('CONTRACT_INVALID', `logLevel 非法：${logLevel}（可选 ${LOG_LEVELS.join('/')}）`)
  }
  const budgetTokens = typeof raw.budgetTokens === 'number' && Number.isFinite(raw.budgetTokens) ? Math.trunc(raw.budgetTokens) : 1600
  if (budgetTokens <= 0) {
    throw new FishError('CONTRACT_INVALID', `budgetTokens 必须为正整数，收到 ${budgetTokens}`)
  }
  return {
    mode: typeof raw.mode === 'string' && raw.mode !== '' ? raw.mode : 'shadow',
    specDir: typeof raw.specDir === 'string' ? raw.specDir : '',
    fishHome: typeof raw.fishHome === 'string' ? raw.fishHome : '',
    dbPath: typeof raw.dbPath === 'string' ? raw.dbPath : '',
    budgetTokens,
    strictHostVersion: raw.strictHostVersion === true,
    commandEnabled: raw.commandEnabled !== false,
    logLevel,
  }
}

/** 影子期一次性装配结果（导出便于命令与验收脚本复用同一份事实）。 */
export interface ShadowRuntime {
  readonly config: FishConfig
  readonly paths: FishPaths
  readonly spec: SpecBundle
  readonly gate: StartupGateResult
}

/**
 * 只做装配、不注册任何宿主面：契约门禁 + 影子初始化。
 * @throws ContractMismatchError / SpecError / DatabaseError —— 任一不满足即抛（fail closed）
 */
export function prepareShadow(rawConfig: Partial<FishConfig> = {}, ctx: Context | null = null): ShadowRuntime {
  const config = normalizeConfig(rawConfig)
  if (config.mode !== 'shadow') {
    throw new FishError('SHADOW_READ_ONLY', `影子期只支持 mode=shadow，收到 ${config.mode}`, {
      hint: '换心期（用户决定退役旧实现后）才引入写路径；当前请把 mode 改回 shadow',
    })
  }
  const logger = createLogger(ctx, { minLevel: config.logLevel as LogLevel })
  const paths = resolvePaths({ specDir: config.specDir, fishHome: config.fishHome, dbPath: config.dbPath })
  const spec = loadSpec(paths.specDir)
  const host = detectHostVersion()
  const gate = runStartupGate({
    specDir: paths.specDir,
    dbPath: paths.dbPath,
    hostVersion: host.version,
    strictHostVersion: config.strictHostVersion,
  })
  logger.info('契约门禁通过', {
    contractVersion: gate.contractVersion,
    hostVersion: gate.hostVersion,
    hostVersionSource: host.source,
    fingerprint: gate.fingerprint.slice(0, 16),
    businessTables: gate.businessTableCount,
    tools: spec.tools.tools.length,
    events: spec.events.types.length,
    injectionSections: spec.injection.sections.length,
  })
  return { config, paths, spec, gate }
}

/**
 * cordis 插件入口。
 * @param ctx 宿主上下文
 * @param rawConfig 已由 schemastery 归一（这里再校验一次非法值）
 */
export function apply(ctx: Context, rawConfig: Partial<FishConfig> = {}): void {
  const runtime = prepareShadow(rawConfig, ctx)
  const logger = createLogger(ctx, { minLevel: runtime.config.logLevel as LogLevel }).child('commands')
  const deps: CommandDeps = {
    paths: runtime.paths,
    spec: runtime.spec,
    gate: runtime.gate,
    logger,
    config: {
      mode: runtime.config.mode,
      budgetTokens: runtime.config.budgetTokens,
      commandEnabled: runtime.config.commandEnabled,
    },
  }
  // 两段式装配：命令面按服务可用性就地装配，缺 `commands` 时插件其余部分照常存活
  // （行级 inject 会卡住整个插件，这是社区实测过的坑）。
  // 关掉命令面时**连 inject 都不发**：不用的服务不去要，是"最小权限"的同一条纪律。
  if (!runtime.config.commandEnabled) {
    logger.info('命令面已按配置关闭（commandEnabled=false）：本插件在模型面与人面都不留痕')
    return
  }
  ctx.inject(['commands'], (scoped) => {
    const dispose = registerFishCommand(scoped, deps)
    scoped.effect(() => dispose, 'dsh-fish: /fish command')
    logger.info('已注册 /fish 命令（影子期只读子集）')
  })
}

export {
  SUPPORTED_CONTRACT_VERSION,
  runStartupGate,
  compareVersions,
  type StartupGateResult,
} from './core/contract.ts'
export { computeSchemaFingerprint, diffFingerprints, type SchemaFingerprint } from './core/fingerprint.ts'
export { openReadOnly, objectCounts, businessTableNames, assertWriteRejected } from './core/db.ts'
export { FishError, ContractMismatchError, SpecError, DatabaseError, toFishError } from './core/errors.ts'
export { detectHostVersion, type HostVersionInfo } from './core/host-version.ts'
export { createLogger, SILENT_LOGGER, type FishLogger, type LogLevel } from './core/logger.ts'
export { resolvePaths, findPackageRoot, type FishPaths } from './core/paths.ts'
export { cutTokens, cutText, ftsMatchExpression, compileCut, verifyCutVectors, DEFAULT_CUT_RULES, type CutSpec } from './layout/cut.ts'
export {
  loadSpec,
  SPEC_FILES,
  WRITE_CLASSES,
  IDEMPOTENT_VALUES,
  CLI_KIND_VALUES,
  CLI_IMPL_VALUES,
  type Idempotent,
  type SpecBundle,
  type WriteClass,
} from './ports/spec.ts'
export { handleFishCommand, COMMAND_NAME, parseCommandInput, finalizeOutput, OUTPUT_LINE_LIMIT } from './surfaces/commands.ts'
export { NATIVE_COMMANDS } from './domain/cli.ts'
export { HOSTS_SUBCOMMANDS, handleHostsCommand, renderHostsHealth, renderHostsProbe, HOSTS_USAGE } from './surfaces/hosts.ts'
export { flagsToArgv, handleCliInput, runFishCli } from './surfaces/cli.ts'
export { outputLogPath, withHeader } from './surfaces/output.ts'

// ---- 领域层（T02 只读子集）：双跑器与单元测试复用**生产实现**，不另写一份 ----
export {
  CONFIG_DEFAULTS,
  configKeyDef,
  getConfig,
  getConfigBool,
  getConfigFloat,
  getConfigInt,
  getConfigString,
  loadDecayConfig,
  type ConfigKeyDef,
  type ConfigType,
} from './domain/config.ts'
export {
  ALIAS_KINDS,
  ALIAS_MAX_HOPS,
  aliasMap,
  currentOpenStint,
  currentOpenStints,
  findCitizenByName,
  normalizeWorkspacePath,
  resolveName,
  resolveProjectByCwd,
  resolveProjectNames,
  walkAlias,
  type AliasKind,
  type NameResolution,
  type ProjectStint,
} from './domain/names.ts'
export {
  clampEvidence,
  cliEntryHint,
  credentialStateLine,
  EVIDENCE_LEVELS,
  findSessionCitizen,
  hashSessionToken,
  IDENTITY_ANONYMOUS,
  IDENTITY_HINTS,
  IDENTITY_SOURCE_REGISTER,
  IDENTITY_SOURCE_TOKEN,
  IDENTITY_SOURCE_UNATTRIBUTED,
  IDENTITY_STATUS,
  identitySourceLine,
  isExpired,
  loadCitizen,
  lookupSessionToken,
  nowUtc,
  parseUtc,
  relatedCitizens,
  relatedCitizensLines,
  remainingTokenHours,
  renderWhoami,
  resolveIdentity,
  resolveOwner,
  sameNameWarnings,
  validateSessionToken,
  whoami,
  type CitizenRow,
  type EvidenceLevel,
  type Identity,
  type IdentityStatus,
  type SessionTokenInfo,
  type WhoamiInput,
  type WhoamiReport,
} from './domain/identity.ts'
export {
  countMemories,
  ensureFtsReadable,
  formatListDeclaration,
  formatStatusTag,
  LIKE_ESCAPE_SQL,
  likeLiteral,
  listMemories,
  LIST_DEFAULT_LIMIT,
  LIST_SOURCE,
  RECALL_DIMENSION_NOTE,
  RECALL_KEYWORD_NOTE,
  recentFallback,
  renderRecall,
  searchMemories,
  TAGS_ONLY_HINT,
  tagLikePattern,
  type MemoryRow,
  type RecallScope,
  type SearchMemoriesOptions,
} from './domain/memory.ts'
export {
  healthcheckReadings,
  renderHealthcheck,
  type HealthReadings,
} from './domain/health.ts'

// ---- 领域层（T03 留下来的原语）：token 计数与截断（码点口径）----
export { BUDGET_RULE_FALLBACK, clipChars, codePointLength, countTokens, sliceCodePoints } from './domain/token.ts'

// ---- 面板数据源（2026-11-09 减重第二批：`board.ts` 砍掉后剩下的两条只读 SELECT）----
export {
  BOARD_POSTS_LIMIT_DEFAULT,
  presenceRows,
  PRESENCE_LIMIT_DEFAULT,
  readPresencePanel,
  recentBoardPosts,
  renderPresencePanel,
  type BoardPostRow,
  type PresenceOptions,
  type PresencePanel,
  type PresenceRow,
} from './domain/presence.ts'

export {
  countLessonRows,
  countOpenLoopRows,
  lessonRows,
  openLoopRows,
  recentLessonRows,
} from './domain/openloop.ts'

// ---- T04：命令等价（76 叶子 → /fish）+ 跨宿主只读管理面 ----
export {
  buildCliMap,
  cliGroupPaths,
  cliLeafPaths,
  cliRootPaths,
  CLI_ANNOTATIONS,
  CLI_IMPLS,
  CLI_KINDS,
  findCliAnnotation,
  formatReplacement,
  renderCliGuidance,
  renderCliHelpText,
  renderCliRefusal,
  resolveCliRoute,
  validateCliAnnotation,
  type CliAnnotation,
  type CliEnumeration,
  type CliEnumerationNode,
  type CliGroupEntry,
  type CliImpl,
  type CliKind,
  type CliMapEntry,
  type CliMapFile,
  type CliReplacement,
  type CliRoute,
} from './domain/cli.ts'
export {
  copySnapshot,
  DEFAULT_PY_TIMEOUT_MS,
  resolveFishPy,
  resolvePythonBin,
  runFishCliOnSnapshot,
  type FishCliRunOptions,
  type FishCliRunResult,
  type SnapshotInfo,
} from './ports/python.ts'
export {
  COLLECTOR_SCRIPTS,
  FISH_HOOK_SCRIPTS,
  REQUIRED_EVENTS,
  findHostSpec,
  resolveHostSpecs,
  type HostEvent,
  type HostSpec,
} from './hosts/registry.ts'
export {
  matcherCoversWriteTools,
  matcherDialect,
  probeAllHosts,
  probeHost,
  renderHostProbe,
  scriptPathOf,
  type FishHookEntry,
  type HostProbeResult,
  type MatcherVerdict,
} from './hosts/probe.ts'
export {
  checkHostHealth,
  formatAge,
  HEARTBEAT_RELDIR,
  HEARTBEAT_STALE_MS,
  heartbeatDir,
  heartbeatFileName,
  listHeartbeatDir,
  readHeartbeat,
  renderHostHealth,
  type HeartbeatReading,
  type HostHealthResult,
} from './hosts/health.ts'
export {
  DIFF_LINE_LIMIT,
  HOOKS_PLAN_TIMEOUT_MS,
  renderHostsDryRun,
  runHooksDryRun,
  unifiedDiff,
  type HostDryRunResult,
  type HostsDryRunResult,
  type HostPlanPayload,
  type HooksPlanPayload,
} from './hosts/dryrun.ts'

// ---------------------------------------------------------------- T05：只读能力三族
//
// 时间原语（三族共用）：库内 UTC 与输出层本地时区是两个口径，各写一份必然漂开。
export { datePartsInOffset, isoLocal, isoWithOffset, offsetMinutesOf, parseStamp, utcStamp } from './core/time.ts'
// Python 文本口径（`str()` / `repr()`）：回执逐字节对拍要的就是这两个。
export { pyRepr, pyText } from './core/pytext.ts'
export {
  effectiveGlobs,
  fnmatchCase,
  judgeComponent,
  KB_DEFAULT_MAX_OPEN_BYTES,
  KB_MANDATORY_DENY,
  KB_MAX_ITEMS_PER_DIR,
  KB_MAX_LIMIT,
  KB_MEDIA_EXTS,
  KB_SEARCH_MAX_FILE_BYTES,
  KB_SENSITIVE_PATTERNS,
  KB_TREE_MAX_DEPTH,
  KbError,
  kbDetectEol,
  kbDetectText,
  kbFitText,
  kbIgnored,
  kbIsDeviceName,
  kbIsSensitiveName,
  kbOpen,
  kbResolveTarget,
  kbSearch,
  kbStats,
  kbTree,
  kbVaultGet,
  kbVaultList,
  kbWalk,
  parseStrictBool,
  renderKbError,
  renderKbOpen,
  renderKbSearch,
  renderKbStats,
  renderKbTree,
  renderKbVaultList,
  type KbFitResult,
  type KbOpenResult,
  type KbScanInfo,
  type KbSearchHit,
  type KbSearchResult,
  type KbStatsResult,
  type KbTarget,
  type KbTextDetection,
  type KbTreeChild,
  type KbTreeNode,
  type KbTreeResult,
  type KbVaultBrief,
  type KbVaultRow,
  type KbVaultStats,
  type KbWalkEntry,
} from './domain/kb.ts'
export {
  asOfIso,
  defaultHomeRoot,
  LAYOUT_STATUS_TABLE,
  parseAsOf,
  projectStatusPayload,
  projectStatusRows,
  PST_COLUMNS,
  PST_DASH_NO_BOARD,
  PST_DASH_NO_MEMORY,
  PST_DASH_NO_OWNER,
  PST_DASH_NONE,
  PST_DASH_UNAVAILABLE,
  PST_OPEN_WINDOW_DAYS,
  PST_OWNER_FALLBACK_DAYS,
  renderAliasList,
  renderProjectStatus,
  renderProjectStatusTable,
  renderWorkspaceList,
  workspaceMappings,
  type AliasRow,
  type ProjectAsOf,
  type ProjectStatusResult,
  type ProjectStatusRow,
  type WorkspaceMapping,
} from './domain/project.ts'
export {
  ageText,
  buildFilterSql,
  canonicalPointer,
  CORPUS_DB_NAME,
  CORPUS_SOURCE_LINE,
  corpusDbPath,
  corpusRead,
  corpusSearch,
  corpusStats,
  corpusHeader,
  FILTER_COLUMNS,
  matchExpression,
  openCorpusReadOnly,
  parsePointer,
  PointerError,
  pushPointer,
  rankCandidates,
  rankNorm,
  READ_DEFAULT_MAX_CHARS,
  REDACT_PLACEHOLDER,
  REDACT_RULES,
  redact,
  renderCorpusRead,
  renderCorpusSearch,
  renderCorpusStats,
  renderPointerError,
  resolvePointer,
  safeField,
  SEARCH_CANDIDATE_FACTOR,
  SEARCH_CANDIDATE_MIN,
  SEARCH_DEFAULT_LIMIT,
  SEARCH_MAX_LIMIT,
  sid8,
  SNIPPET_CHARS,
  type CorpusCandidate,
  type CorpusHit,
  type CorpusReadResult,
  type CorpusSearchResult,
  type CorpusSourceState,
  type CorpusStatsResult,
  type RankedHit,
} from './domain/corpus.ts'
