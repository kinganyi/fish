/**
 * CLI 等价层：**`fish.py` 的每一条叶子命令都要有一个明确归属**。
 *
 * 宿主不允许插件给 `dsh` 二进制加子命令，所以"命令等价"只能落在斜杠命令面上：
 * `/fish <一级> [二级...] [--k=v...]`。一级 35 个做命令名，二级起进 `rawInput`
 * 原样透传（`ctx.commands` 只给 `rawInput`，没有参数 schema）。
 *
 * ## 为什么这张表是**人工裁决 + 机器校验**
 *
 * 命令树本身（谁是谁的子命令、几个叶子）由 `scripts/probe/dump-cli.py` 从 argparse
 * 对象树里**机器枚举**出来——那是事实，不许手写。但"这条命令在影子期敢不敢执行"
 * 是**裁决**，机器给不出答案：它取决于这条命令会不会写库、写文件、要不要独占、
 * 会不会读到别的宿主才有的东西。
 *
 * 所以本模块只维护裁决（`CLI_ANNOTATIONS`），而 `buildCliMap()` 把裁决与枚举**交叉校验**：
 *   · 枚举里有、表里没有 → 抛错（"漏一条"必须响亮，这正是判据 2 的红灯形态）；
 *   · 表里有、枚举里没有 → 抛错（登记表不许声明一条不存在的命令）；
 *   · `proxy-readonly` 必须 `kind==='read'` 且写清只读依据（源码行号）；
 *   · `ts` 必须 `kind==='read'`（影子期 TS 侧没有写权限）；
 *   · 非 `ts` 必须有替代命令，且替代命令**必须能在 `fish.py` 的命令树里解析到**
 *     （或指向 spec 里真实存在的 MCP 工具名）——"未实现"三个字不许是终点。
 *
 * ## 四档归属（与架构方案 §6 对齐）
 *
 * | impl | 含义 | 影子期行为 |
 * |---|---|---|
 * | `ts` | TS 侧已有等价实现（T01–T03 交付的只读能力） | 直接调领域层 |
 * | `proxy-readonly` | Python 侧命令是只读的，转发执行 | 在**只读快照副本**上跑 Python（见 `ports/python.ts`） |
 * | `unsupported-shadow` | 写操作 | **拒绝执行** + 给替代（`fish <cmd>` / MCP 工具名） |
 * | `python-only` | 长事务/独占/读宿主私有环境，只能人工在终端跑 | 只给指引，不执行 |
 *
 * @module dsh-fish/domain/cli
 */

import { FishError } from '../core/errors.ts'

/** 命令的副作用类别（与 `spec/tools.json` 的 `writeClass` 是两个口径，别混）。 */
export const CLI_KINDS = ['read', 'write', 'ops'] as const

/** `read` 只读 / `write` 改库或改文件 / `ops` 运维或进程外（长事务、独占、读宿主任意环境）。 */
export type CliKind = (typeof CLI_KINDS)[number]

/** 实现归属的**取值域**（生成器、判据、命令面共用这一份，绝不各写一份）。 */
export const CLI_IMPLS = ['ts', 'proxy-readonly', 'unsupported-shadow', 'python-only'] as const

/** 一条命令的实现归属。 */
export type CliImpl = (typeof CLI_IMPLS)[number]

/** 未实现时的替代：要么是终端里的 fish CLI 路径，要么是 MCP 工具名（两者都可机器校验）。 */
export interface CliReplacement {
  /** 终端里等效的 `fish <cli>` 路径；必须是命令树里的**叶子**。 */
  readonly cli: string
  /** 等价的 MCP 工具名（可选，必须在 `spec/tools.json` 里）。 */
  readonly mcpTool?: string
  /** 一句话：为什么是这条替代。 */
  readonly note?: string
}

/** 一条命令的裁决（人工维护，机器校验）。 */
export interface CliAnnotation {
  /** 与 `fish.py` 命令树**逐字对齐**的路径（空格分隔，如 `board checkin`）。 */
  readonly path: string
  readonly kind: CliKind
  readonly impl: CliImpl
  /** 为什么是这个档（写清语义；proxy 类必须带源码位置）。 */
  readonly reason: string
  /** 只读依据（源码位置/证据）。`proxy-readonly` 必填。 */
  readonly evidence?: string
  /** 非 `ts` 时必填：终端里该跑什么。 */
  readonly replacement?: CliReplacement
}

/** 一个叶子的**成品**（裁决 + 枚举事实），即 `spec/cli-map.json` 的一条。 */
export interface CliMapEntry {
  readonly path: string
  /** 深度：1 = 一级命令，2/3 = 子命令（`kb vault list` 是 3）。 */
  readonly level: number
  readonly kind: CliKind
  readonly impl: CliImpl
  readonly reason: string
  readonly evidence: string | null
  readonly replacement: CliReplacement | null
  /** 来自 `fish.py` 的 help 文本（事实，不许手写）。 */
  readonly help: string
  /** 该叶子是否有 `--apply` 类开关（写操作的强信号）。 */
  readonly applyFlag: boolean
}

/** 一个容器节点（如 `kb vault`）：只用于帮助文本，不进 `entries`。 */
export interface CliGroupEntry {
  readonly path: string
  readonly level: number
  readonly children: readonly string[]
  readonly help: string
}

/** `spec/cli-map.json` 的形状。 */
export interface CliMapFile {
  readonly specVersion: string
  readonly source: Readonly<Record<string, unknown>>
  readonly counts: Readonly<Record<string, number>>
  readonly implCounts: Readonly<Record<CliImpl, number>>
  readonly kindCounts: Readonly<Record<CliKind, number>>
  readonly entries: readonly CliMapEntry[]
  readonly groups: readonly CliGroupEntry[]
}

/** 枚举器（`dump-cli.py`）产出的一个节点。 */
export interface CliEnumerationNode {
  readonly path: string
  readonly depth: number
  readonly kind: 'leaf' | 'group'
  readonly help: string
  readonly children: readonly string[]
  readonly hasApplyFlag: boolean
  readonly func: string | null
}

/** 枚举器的完整产出。 */
export interface CliEnumeration {
  readonly python: string
  readonly fishPy: string
  readonly fishPySha256_12: string
  readonly rootCommands: readonly string[]
  readonly commands: readonly CliEnumerationNode[]
}

/** `buildCliMap` 的额外上下文：用于校验 `mcpTool` 指向的是真实工具。 */
export interface CliMapBuildContext {
  readonly knownTools: readonly string[]
}

/**
 * **裁决表**：76 条叶子逐条登记（`fish.py` 当前实测：35 一级 / 76 叶子 / 88 节点 / 最深 3 级）。
 *
 * 三条写表纪律（每一条都对着一次真实踩坑）：
 *   1. `kind` 按**生产语义**判，不按"看起来像读"——`board show` 看着是读，实际会
 *      `board_ensure` 建行（`p0/fish.py:11492` "取或建一块板"），所以它是 `write`；
 *   2. `proxy-readonly` 只给**逐行核过没有写语句**的命令，`evidence` 写源码位置。
 *      没核过的一律 `python-only`（宁可给人一条指引，不替人赌一次写）；
 *   3. `reason` 说"为什么"，`replacement` 说"那我去哪做"——两样缺一条，这条标注就等于没说。
 */
export const CLI_ANNOTATIONS: readonly CliAnnotation[] = [
  // ---------------------------------------------------------------- 记忆与身份
  {
    path: 'remember',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '写 memory（append 事件 + 投影 + FTS + links），影子期 TS 侧没有任何写路径',
    replacement: { cli: 'remember', mcpTool: 'remember', note: '终端里跑，或走仍在册的 MCP 工具' },
  },
  {
    path: 'recall',
    kind: 'read',
    impl: 'ts',
    reason: 'T02 已复刻 `searchMemories` / `renderRecall`，只读（不写 memory_access_log）',
  },
  {
    path: 'list',
    kind: 'read',
    impl: 'proxy-readonly',
    reason: '只列 code_index（不含检索、不写审计），Python 侧是纯 SELECT；TS 未做同形渲染',
    evidence: 'p0/fish.py:6919-6928（cmd_list：一条 SELECT code_index + 打印）',
    replacement: { cli: 'list', note: 'Python 侧纯读，可在快照副本上转发' },
  },
  {
    path: 'neighbors',
    kind: 'read',
    impl: 'proxy-readonly',
    reason: '邻域五策略仍在 Python（TS 侧没有关联边读取原语：`links.ts` 已按第二批减重删除）',
    evidence: 'p0/fish.py:6885-6916（cmd_neighbors）+ 6350-6407（neighbors()：无 INSERT/UPDATE/DELETE）',
    replacement: { cli: 'neighbors', note: 'Python 侧纯读，可在快照副本上转发' },
  },
  {
    path: 'forgetting',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '`--apply` 会把 dormant 落库并写事件（默认只打印计划）',
    replacement: { cli: 'forgetting', note: '终端里先看计划，确认后再 --apply' },
  },
  {
    path: 'distill',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '`--write` 会落文件（同 tag 的 episode 聚成建议），影子期不产出文件',
    replacement: { cli: 'distill', note: '终端里跑演练形态（不加 --write 就不写文件）' },
  },
  {
    path: 'withdraw',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '撤回一条记忆（改 status + 追加 events），是写操作',
    replacement: { cli: 'withdraw' },
  },
  {
    path: 'supersede',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '把旧编码标记为被替代（事务写 + events），影子期只在 Python 侧做',
    replacement: { cli: 'supersede', mcpTool: 'supersede' },
  },
  {
    path: 'owner',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '`owner set` 会写 config/citizens（`show` 只读，但同一路径两种形态）',
    replacement: { cli: 'owner', note: '终端里跑 `fish owner show` 只读；set 形态是写' },
  },
  {
    path: 'owner-fix',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '修正记忆归属（追加 events + 更新投影），MCP 面刻意不提供这条权限',
    replacement: { cli: 'owner-fix' },
  },
  {
    path: 'whoami',
    kind: 'read',
    impl: 'ts',
    reason: 'T02 已复刻 `resolveIdentity` / `renderWhoami`（证据级别只降不升）',
  },
  {
    path: 'events',
    kind: 'read',
    impl: 'proxy-readonly',
    reason: '只读 events 表最近 N 行；TS 未做这段渲染（事件只增不改，转发不改变语义）',
    evidence: 'p0/fish.py:11418-11425（cmd_events：一条 SELECT … ORDER BY seq DESC LIMIT ?）',
    replacement: { cli: 'events', note: 'Python 侧纯读，可在快照副本上转发' },
  },
  {
    path: 'init',
    kind: 'ops',
    impl: 'python-only',
    reason: '首次建库/建 home（SCHEMA + 目录），进程外一次性动作；插件内跑它等于"插件替你建库"',
    replacement: { cli: 'init', note: '终端里跑（影子期插件不建库）' },
  },
  {
    path: 'healthcheck',
    kind: 'read',
    impl: 'ts',
    reason: 'T02 已复刻 `healthcheckReadings` / `renderHealthcheck`（七项读数）',
  },
  {
    path: 'inject',
    kind: 'read',
    impl: 'python-only',
    // 2026-11-09 减重第二批：`domain/inject.ts`（连带 board/push/profile/activity 约 224 KB）
    // 已按裁决整块删除——注入包口径是 fish 侧漂移最频繁的一块，插件只保留「只读 + 本体能力补充」。
    // 降成 python-only 而不是留一个永远报错的 ts 分支：裁决表说什么就该真是什么。
    reason: 'TS 侧注入包复刻已按 2026-11-09 减重裁决删除（跟随 fish 漂移的主要耦合面）',
    replacement: { cli: 'inject', note: '终端里跑 `fish inject`：Python 侧仍是注入包的权威口径' },
  },
  {
    path: 'report',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '渲染日报到 fish-home（写文件 + 可能有隐藏写路径），影子期不改 fish-home',
    replacement: { cli: 'report' },
  },

  // ---------------------------------------------------------------- 项目 / 别名 / 工作区
  {
    path: 'project-add',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '登记一个新前缀（code_registry + events）',
    replacement: { cli: 'project-add', mcpTool: 'add_project' },
  },
  {
    path: 'project retire',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '退役前缀 = 停止分配新编码（`--apply` 才落库，默认只打印计划）',
    replacement: { cli: 'project retire' },
  },
  {
    path: 'project restore',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '把 retired 改回 active（`--apply` 才落库）',
    replacement: { cli: 'project restore' },
  },
  {
    path: 'project close',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '项目闭合（改状态 + events；`--apply` 才落库）',
    replacement: { cli: 'project close', mcpTool: 'close_project' },
  },
  {
    path: 'project reopen',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '把 closed 改回 active（`--apply` 才落库）',
    replacement: { cli: 'project reopen', mcpTool: 'reopen_project' },
  },
  {
    path: 'project status',
    kind: 'read',
    impl: 'ts',
    reason:
      'T05 已复刻 `project_status_rows`（六列 + 每列来源 + 降级清单）。' +
      '⚠ 生产**默认形态**会把渲染结果写进 `<home>/10-项目库/项目状态表.md`，`--json` 形态才纯读；' +
      '影子期只实现纯读的那一半：同一份表格照常渲染，但**不落盘**，回执末尾明写"没有写文件"',
    evidence: 'p0/fish.py:14517（project_status_rows 的 docstring 明写"只读：不改任何表、不写盘"）；写盘在 render_project_status:14645',
  },
  {
    path: 'alias link',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '建立名字别名（改检索语义；MCP 面刻意不给这条权限）',
    replacement: { cli: 'alias link' },
  },
  {
    path: 'alias unlink',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '撤销别名（幂等写）',
    replacement: { cli: 'alias unlink' },
  },
  {
    path: 'alias list',
    kind: 'read',
    impl: 'ts',
    reason:
      'T05 已复刻 `cmd_alias` 的 list 分支：原始别名行 + 按 canonical 折叠的分组（单向展开，最多 8 跳）',
    evidence: 'p0/fish.py:6722-6738（cmd_alias 的 else 分支：SELECT + resolve_name）',
  },
  {
    path: 'workspace set',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: 'workspace_map 是项目归属的唯一权威来源，set 是显式数据变更（写 events）',
    replacement: { cli: 'workspace set' },
  },
  {
    path: 'workspace remove',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '删除映射（写 events 留痕）',
    replacement: { cli: 'workspace remove' },
  },
  {
    path: 'workspace list',
    kind: 'read',
    impl: 'ts',
    reason: 'T05 已复刻 `_workspace_cmd_list`（`list_workspace_mappings` + 一行一条渲染）',
    evidence: 'p0/fish.py:10036-10037 → 10065-10075（_workspace_cmd_list：list_workspace_mappings + 打印）',
  },
  {
    path: 'workspace seed',
    kind: 'read',
    impl: 'proxy-readonly',
    reason: '打印建议**绝不自动写入**（源码 docstring 明写）',
    evidence: 'p0/fish.py:10025-10028（"list / seed 只读：seed 打印建议但绝不自动写入"）',
    replacement: { cli: 'workspace seed', note: 'Python 侧纯读，可在快照副本上转发' },
  },

  // ---------------------------------------------------------------- 看板
  {
    path: 'board show',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '读板前会 `board_ensure`（取或**建**一块板，会 INSERT boards 行），所以不是纯读',
    replacement: { cli: 'board show', mcpTool: 'board', note: '要只读看板内容请用 `/fish shadow presence`（只剩在场与动态两条 SELECT）' },
  },
  {
    path: 'board checkin',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '签到（upsert 自己的 presence 行 + events），影子期不写库',
    replacement: { cli: 'board checkin', mcpTool: 'board' },
  },
  {
    path: 'board post',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '往看板写一条动态（board_posts + events）',
    replacement: { cli: 'board post', mcpTool: 'board' },
  },
  {
    path: 'board render',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '把看板渲染成 fish-home 里的 Markdown（写文件 + 可能归一 boards.path）',
    replacement: { cli: 'board render' },
  },
  {
    path: 'board close',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '关闭一块板（改 boards.status + events）',
    replacement: { cli: 'board close' },
  },
  {
    path: 'board sweep',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '清理过期看板（DELETE + events）',
    replacement: { cli: 'board sweep' },
  },

  // ---------------------------------------------------------------- 布局 / 知识库
  {
    path: 'layout render',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '渲染门面文件树（写 fish-home/00-首页、10-项目库…），影子期不改 fish-home',
    replacement: { cli: 'layout render', note: '终端里必须带 `--all`（本批只支持全量渲染）' },
  },
  {
    path: 'layout verify',
    kind: 'read',
    impl: 'python-only',
    reason: '体检逻辑在 `verify_layout()` 辅助函数里（`p0/fish.py` 只做分发），未逐行核过写路径，不赌',
    replacement: { cli: 'layout verify', note: '终端里跑，读数与文件树一致' },
  },
  {
    path: 'kb tree',
    kind: 'read',
    impl: 'ts',
    reason:
      'T05 已复刻 `kb_tree`（统计层 + 显示层分开、双预算截断、深度口径）。' +
      '影子期**不写 kb_access 审计**（生产每次读都追一条），所以"未读/读次数"里不会出现 DSH 的读取',
    evidence: 'p0/fish_kb.py:1541-1665（kb_tree：只统计不读正文；`_kb_log_quiet(action="tree")` 是唯一写点，影子期不写）',
  },
  {
    path: 'kb open',
    kind: 'read',
    impl: 'ts',
    reason:
      'T05 已复刻 `kb_open`（8 类路径拒绝 + 敏感名单二次授权 + 编码/媒体双判 + 截断声明）。' +
      '影子期不写 kb_access',
    evidence: 'p0/fish_kb.py:1731-1834（kb_open；正文读取一律 read_bytes，审计写点不实现）',
  },
  {
    path: 'kb search',
    kind: 'read',
    impl: 'ts',
    reason:
      'T05 已复刻 `kb_search`（按需扫描、不建索引、敏感文件不进片段、片段 240 字、排序 -occurrences/-mtime/rel）',
    evidence: 'p0/fish_kb.py:1966-2108（kb_search：只扫 .md，query 只以 hash 进审计；影子期不写审计）',
  },
  {
    path: 'kb stats',
    kind: 'read',
    impl: 'ts',
    reason:
      'T05 已复刻 `kb_stats`（规模/未读/最近变更/每库读写计数）；镜像/离线两档来源如实标注',
    evidence: 'p0/fish_kb.py:2673-2729（kb_stats：_kb_scan_counts + 四条 kb_access 聚合，纯 SELECT）',
  },
  {
    path: 'kb toc',
    kind: 'read',
    impl: 'proxy-readonly',
    reason:
      '目录快照（分类概览 + 前 3 标题 + 分页，2026-10-28 新增；纯读、不写 kb_access）' +
      '——TS 侧尚未复刻，影子期在快照副本上只读转发',
    evidence: 'p0/fish.py:13765（add_parser "toc"）+ 13411-13412（act=="toc" → _kb_cmd_toc）',
    replacement: { cli: 'kb toc', note: 'Python 侧纯读，可在快照副本上转发' },
  },
  {
    path: 'kb doctor',
    kind: 'ops',
    impl: 'python-only',
    reason: '知识库体检（可能修索引/登记），运维动作只能在终端人工跑',
    replacement: { cli: 'kb doctor' },
  },
  {
    path: 'kb write',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '写知识库 Markdown（追加/更新 + kb_access），影子期不写用户知识库',
    replacement: { cli: 'kb write', mcpTool: 'kb_write' },
  },
  {
    path: 'kb rollback',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '按备份回滚一次写入（改文件 + kb_access）',
    replacement: { cli: 'kb rollback', mcpTool: 'kb_write' },
  },
  {
    path: 'kb vault list',
    kind: 'read',
    impl: 'ts',
    reason:
      'T05 已复刻 `_kb_cmd_vault` 的 list 分支（`kb_vault_list`：默认库在前，其次按名字；一行 `_kb_vault_line`）',
    evidence: 'p0/fish_kb.py:567-571 + 2984-2993（kb_vault_list 纯 SELECT + list 渲染）',
  },
  {
    path: 'kb vault add',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '登记一个 vault（写 kb_vault + events）',
    replacement: { cli: 'kb vault add' },
  },
  {
    path: 'kb vault remove',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '注销 vault（改 kb_vault 状态）',
    replacement: { cli: 'kb vault remove' },
  },
  {
    path: 'kb vault set-default',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '改默认 vault（写 config/kb_vault）',
    replacement: { cli: 'kb vault set-default' },
  },
  {
    path: 'kb vault set-mode',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '改 vault 读写模式（ro/rw）——这直接决定别人能不能写你的知识库',
    replacement: { cli: 'kb vault set-mode' },
  },
  {
    path: 'kb vault scan',
    kind: 'ops',
    impl: 'python-only',
    reason: '扫描 vault 目录（遍历文件系统 + 可能写登记），运维动作只在终端跑',
    replacement: { cli: 'kb vault scan' },
  },
  {
    path: 'kb backup prune',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '按保留策略删知识库备份（删除不可逆）',
    replacement: { cli: 'kb backup prune' },
  },

  // ---------------------------------------------------------------- 语料层
  {
    path: 'corpus ingest',
    kind: 'ops',
    impl: 'python-only',
    reason: '入库是秒级以上长任务（独占 + 游标 + 脱敏），进程内跑会卡住宿主事件循环',
    replacement: { cli: 'corpus ingest', note: '终端里跑（T03.5 已定档 audit/幂等，但执行面仍在 Python）' },
  },
  {
    path: 'corpus stats',
    kind: 'read',
    impl: 'ts',
    reason:
      'T05 已复刻 `fish_corpus.stats`（段/会话/by_host/by_project/degraded/orphan/台账四档/滞后/体积）。' +
      '读的是独立库 `corpus.sqlite`（只读打开），不是主库快照',
    evidence: 'p0/fish_corpus.py:575-703（stats：库不存在返回 db_missing 不建库；readonly=True 打开）',
  },
  {
    path: 'corpus search',
    kind: 'read',
    impl: 'ts',
    reason:
      'T05 已复刻 `fish_corpus.search`（MATCH + 过滤下推、0.6/0.2/0.2 排序、出口二次脱敏）。' +
      '影子期不写 `corpus_access_log`（生产每次检索都追一条）',
    evidence: 'p0/fish_corpus.py:892-967（search：只读连接；`_log_access` 是唯一写点，影子期不写）',
  },
  {
    path: 'corpus read',
    kind: 'read',
    impl: 'ts',
    reason:
      'T05 已复刻 `fish_corpus.read`（门牌三档 + 段级脱敏 + 源文件缺失声明）。' +
      '影子期不写访问审计、**也不回填** `push_log.reaction`（那是主库写）',
    evidence: 'p0/fish_corpus.py:990-1043（read：投影正文 + source 状态；MCP 的采纳回填在 mcp_server.py:2056-2062）',
  },
  {
    path: 'corpus retag',
    kind: 'write',
    impl: 'python-only',
    reason: '改语料标签（写 corpus.sqlite），长事务只能在终端跑',
    replacement: { cli: 'corpus retag' },
  },
  {
    path: 'corpus forget',
    kind: 'write',
    impl: 'python-only',
    reason: '按来源遗忘语料（DELETE，不可逆），必须在终端人工确认后跑',
    replacement: { cli: 'corpus forget' },
  },
  {
    path: 'corpus rebuild',
    kind: 'ops',
    impl: 'python-only',
    reason: '重建语料库（`--apply` 才落库，秒级长任务 + 独占）',
    replacement: { cli: 'corpus rebuild' },
  },
  {
    path: 'corpus doctor',
    kind: 'ops',
    impl: 'python-only',
    reason: '语料库体检（可能修索引），运维动作只在终端跑',
    replacement: { cli: 'corpus doctor' },
  },

  // ---------------------------------------------------------------- 推送 / 动作流 / 会话
  {
    path: 'push stats',
    kind: 'read',
    impl: 'proxy-readonly',
    reason: '只读推送效果度量（push_stats 无写语句）',
    evidence: 'p0/fish.py:10753-10754 + 10612-10715（push_stats：全 SELECT/聚合）',
    replacement: { cli: 'push stats', note: 'Python 侧纯读，可在快照副本上转发' },
  },
  {
    path: 'push status',
    kind: 'read',
    impl: 'proxy-readonly',
    reason: '只读推送配置（get_config 七次）',
    evidence: 'p0/fish.py:10759-10764（cmd_push 的 else 分支：get_config 打印）',
    replacement: { cli: 'push status', note: 'Python 侧纯读，可在快照副本上转发' },
  },
  {
    path: 'push sweep',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '把超窗推送收口成 ignored（写 push_log.reaction）',
    replacement: { cli: 'push sweep' },
  },
  {
    path: 'activity stats',
    kind: 'read',
    impl: 'python-only',
    reason: '读数依赖 `data/_audit/hooks/pretool-asks.jsonl`（快照副本里没有）→ 转发会漏报撞车询问条数',
    replacement: { cli: 'activity stats', note: '终端里跑（读的是本机审计文件）' },
  },
  {
    path: 'activity prune',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '删除超过保留期的 tool_activity 行（DELETE，不可逆）',
    replacement: { cli: 'activity prune' },
  },
  {
    path: 'session start',
    kind: 'ops',
    impl: 'python-only',
    reason: 'session 是 CLI 专属命令族（MCP 面不签发凭证），要读宿主 env/payload 才能算身份',
    replacement: { cli: 'session start' },
  },
  {
    path: 'session status',
    kind: 'read',
    impl: 'python-only',
    reason: '同上：会话状态要读宿主环境（DSH_SESSION_*），插件进程里读到的是**它自己的**进程环境',
    replacement: { cli: 'session status' },
  },
  {
    path: 'session end',
    kind: 'write',
    impl: 'python-only',
    reason: '关闭会话（回收 token + 写 events），只能在会话真身所在进程外做',
    replacement: { cli: 'session end' },
  },
  {
    path: 'session agent',
    kind: 'ops',
    impl: 'python-only',
    reason: '给会话绑 agent 标识（读宿主载荷 + 写 session_tokens）',
    replacement: { cli: 'session agent' },
  },
  {
    path: 'prompt-push',
    kind: 'ops',
    impl: 'python-only',
    reason: 'UserPromptSubmit hook 的子进程入口（读 stdin 的 hook payload），不是人手动跑的命令',
    replacement: { cli: 'prompt-push' },
  },

  // ---------------------------------------------------------------- 公民 / 归档 / 运维
  {
    path: 'citizen-add',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '登记一个公民（citizens + events）',
    replacement: { cli: 'citizen-add' },
  },
  {
    path: 'citizen-list',
    kind: 'read',
    impl: 'proxy-readonly',
    reason: '只列公民（无写语句），TS 未做这段渲染',
    evidence: 'p0/fish.py:9992-10016（cmd_citizen_list：SELECT + 打印）',
    replacement: { cli: 'citizen-list', note: 'Python 侧纯读，可在快照副本上转发' },
  },
  {
    path: 'citizen-retire',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '退役公民（要求显式 --migrate-to/--force，避免静默丢归属）',
    replacement: { cli: 'citizen-retire' },
  },
  {
    path: 'citizen-merge',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason:
      '合并两个公民（默认 dry-run 打印计划；`--apply` 会把 owner/session 归属迁到 dst 并写 events）' +
      '——影子期不写，也不替用户决定"哪两个身份其实是同一个"',
    evidence: 'p0/fish.py:8501-8540（citizen_merge：apply=False 只统计）+ 8543-8550（cmd_citizen_merge）',
    replacement: { cli: 'citizen-merge', note: '终端里先跑 dry-run 看计划，确认后再 --apply' },
  },
  {
    path: 'archive',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '归档 = 搬行（INSERT 冷表 + DELETE 热表 + 写文件），默认 dry-run、`--apply` 才落库',
    replacement: { cli: 'archive', note: '终端里先看计划，确认后再 --apply' },
  },
  {
    path: 'rebuild',
    kind: 'ops',
    impl: 'python-only',
    reason: '重建 FTS/投影（长事务 + 独占），进程内跑会阻塞宿主事件循环',
    replacement: { cli: 'rebuild' },
  },
  {
    path: 'reindex',
    kind: 'ops',
    impl: 'python-only',
    reason: '重建索引（长事务 + 独占）',
    replacement: { cli: 'reindex' },
  },
  {
    path: 'config',
    kind: 'write',
    impl: 'unsupported-shadow',
    reason: '同一路径两种形态：无参列出（读）/ 带参 `set_config` 写库（p0/fish.py:10589）',
    replacement: { cli: 'config', note: '终端里跑 `fish config`（只读列表）或 `fish config <key> <value>`（写）' },
  },
  // ── ui：只读读口（2026-10-28 新增）─────────────────────────────────
  // 这两条是 honest 的 python-only：读口是 Python 侧的 `fish_ui.py`，
  // TS 半边完全没有对应实现（也不该有——影子期不碰 HTTP 面）。
  // 加命令必须同时裁决，否则 `buildCliMap` 会拒绝生成 spec：
  // 这是"契约是唯一真源"在起作用，不是麻烦。
  {
    path: 'ui serve',
    kind: 'ops',
    impl: 'python-only',
    reason: '起一个只读 HTTP 读口（绑回环、mode=ro + query_only）。属运维动作，影子期 TS 侧不提供',
    replacement: { cli: 'ui serve', note: '终端里跑 `python p0/fish.py ui serve --port 8787`' },
  },
  {
    path: 'ui dump',
    kind: 'read',
    impl: 'python-only',
    reason: '打印一份只读快照 JSON（调试/对拍用）。虽然只读，但要读整个主库并做撞车聚合，'
      + 'TS 侧没有同形实现，故不标 proxy-readonly',
    replacement: { cli: 'ui dump', note: '终端里跑 `python p0/fish.py ui dump`' },
  },
] as const

/**
 * dsh-fish **自己的**命令名（不来自 `fish.py`，因此不进 `cli-map.json`）。
 *
 * 为什么单独列：判据③要比"命令面注册项 vs cli-map"，把自有命令混进 cli-map 会让
 * 覆盖率判据失去意义（它比的应该是"Python 命令树 == 映射表"）。所以自有命令显式登记在这里，
 * 判据侧用 `NATIVE_COMMANDS ∪ cli-map 一级` 做全集比对。
 */
export const NATIVE_COMMANDS = ['shadow', 'hosts'] as const

/** `fish.py` 里一级命令名（35 个，来自枚举；本函数只做去重与排序）。 */
export function cliRootPaths(): readonly string[] {
  const roots = new Set<string>()
  for (const annotation of CLI_ANNOTATIONS) {
    const [root] = annotation.path.split(' ')
    if (root !== undefined && root !== '') roots.add(root)
  }
  return [...roots].sort()
}

/** 全部叶子路径（76 条）。 */
export function cliLeafPaths(): readonly string[] {
  return CLI_ANNOTATIONS.map((annotation) => annotation.path).slice().sort()
}

/** 全部容器路径（`kb vault`、`kb backup` 这类只有子命令、没有自己动作的节点）。 */
export function cliGroupPaths(): readonly string[] {
  const groups = new Set<string>()
  for (const annotation of CLI_ANNOTATIONS) {
    const parts = annotation.path.split(' ')
    for (let i = 1; i < parts.length; i += 1) groups.add(parts.slice(0, i).join(' '))
  }
  return [...groups].sort()
}

/** 按路径取裁决（未登记返回 `null`）。 */
export function findCliAnnotation(path: string): CliAnnotation | null {
  return CLI_ANNOTATIONS.find((annotation) => annotation.path === path) ?? null
}

/** 一次命令调用的解析结果。 */
export interface CliRoute {
  /** 命中的路径（叶子或容器）。 */
  readonly path: string
  /** 叶子给裁决；容器给 `null`（容器只渲染帮助）。 */
  readonly annotation: CliAnnotation | null
  /** 路径之后剩下的 token（原样透传）。 */
  readonly rest: readonly string[]
}

/**
 * 解析一条 CLI 调用（最长前缀匹配）。
 *
 * 为什么用"最长前缀"而不是"取前两个词"：命令树最深 3 级（`kb vault list`），
 * 取固定长度会把 `kb vault` 当成叶子、把 `list` 当成位置参数——那条命令会**静默做错事**。
 */
export function resolveCliRoute(tokens: readonly string[]): CliRoute | null {
  const leaves = new Set(cliLeafPaths())
  const groups = new Set(cliGroupPaths())
  for (let take = Math.min(tokens.length, 3); take >= 1; take -= 1) {
    const path = tokens.slice(0, take).join(' ')
    if (leaves.has(path)) {
      return { path, annotation: findCliAnnotation(path), rest: tokens.slice(take) }
    }
    if (groups.has(path)) {
      return { path, annotation: null, rest: tokens.slice(take) }
    }
  }
  return null
}

/** 帮助文本（顶层：按一级命令分组列出；指定容器：列出子命令）。 */
export function renderCliHelpText(path: string | null = null): string {
  const header = 'fish 命令等价面（`/fish <一级> [二级...] [--k=v...]`；二级起原样透传）'
  if (path !== null) {
    const children = CLI_ANNOTATIONS.filter((annotation) => annotation.path.startsWith(`${path} `))
    if (children.length === 0) {
      const annotation = findCliAnnotation(path)
      if (annotation === null) return `未知命令：${path}`
      return [
        `fish ${annotation.path}`,
        `  类别：${annotation.kind}｜实现：${annotation.impl}`,
        `  理由：${annotation.reason}`,
        ...(annotation.evidence === undefined ? [] : [`  只读依据：${annotation.evidence}`]),
        `  替代：${formatReplacement(annotation.replacement)}`,
      ].join('\n')
    }
    return [
      `fish ${path} <子命令>`,
      ...children.map((annotation) => `  ${annotation.path}  [${annotation.impl}]  ${annotation.reason}`),
    ].join('\n')
  }
  const roots = cliRootPaths()
  const lines = [header, `一级命令 ${roots.length} 个｜叶子 ${CLI_ANNOTATIONS.length} 条｜自有命令 ${NATIVE_COMMANDS.join(' / ')}`]
  for (const root of roots) {
    const own = findCliAnnotation(root)
    if (own !== null) {
      lines.push(`  ${root}  [${own.impl}/${own.kind}]`)
      continue
    }
    const children = CLI_ANNOTATIONS.filter((annotation) => annotation.path.startsWith(`${root} `))
    lines.push(`  ${root}  <${children.length} 个子命令>`)
  }
  lines.push('  输入 `/fish <命令> --help` 看单条；输入 `/fish shadow status` 看影子期状态')
  return lines.join('\n')
}

/** 替代命令的人读渲染（没有替代时给一句明确的"没有替代"）。 */
export function formatReplacement(replacement: CliReplacement | null | undefined): string {
  if (replacement === null || replacement === undefined) return '（无：影子期不执行，也没有等价入口）'
  const parts = [`fish ${replacement.cli}`]
  if (replacement.mcpTool !== undefined) parts.push(`MCP 工具 \`mcp__fish__${replacement.mcpTool}\``)
  const note = replacement.note === undefined ? '' : `（${replacement.note}）`
  return `${parts.join(' 或 ')}${note}`
}

/**
 * 影子期拒绝执行的文案（`unsupported-shadow`）。
 *
 * 为什么必须**明确说"没有写入"**：拒绝得含糊，读者会怀疑"到底写没写一半"；
 * 而这条命令的整个意义就是"让它写不进去"。
 */
export function renderCliRefusal(annotation: CliAnnotation, reason: string): string {
  return [
    `影子期不执行写操作：fish ${annotation.path}`,
    `  原因：${reason}`,
    `  替代：${formatReplacement(annotation.replacement)}`,
    '  本次调用**没有**打开写连接、**没有**启动 Python、**没有**产生任何写入。',
    '  （影子期纪律：不注册模型工具 / 不注入上下文 / 不写 fish 库 / 不改宿主配置）',
  ].join('\n')
}

/** 只给指引（`python-only`）的文案。 */
export function renderCliGuidance(annotation: CliAnnotation, reason: string): string {
  return [
    `影子期只给指引，不执行：fish ${annotation.path}`,
    `  原因：${reason}`,
    `  替代：${formatReplacement(annotation.replacement)}`,
    '  本次调用**没有**启动 Python、**没有**产生任何写入。',
  ].join('\n')
}

/**
 * 校验一条裁决的内部自洽（与命令树无关的那部分）。
 *
 * 为什么单独导出：`buildCliMap` 用的是**固定的**裁决表，等于"错误分支只有改源码才能触发"；
 * 导出之后单元测试能拿合成裁决逐条喂进去（写命令标 ts / proxy 标写命令 / 缺替代 /
 * 替代指向不存在的工具），这些分支才真的被覆盖过。
 * @throws FishError `SPEC_INVALID`
 */
export function validateCliAnnotation(annotation: CliAnnotation, knownTools: readonly string[]): void {
  const where = `cli-map 标注 ${annotation.path}`
  if (!CLI_KINDS.includes(annotation.kind)) {
    throw new FishError('SPEC_INVALID', `${where}：kind=${String(annotation.kind)} 不在取值域 ${CLI_KINDS.join('/')}`)
  }
  if (!CLI_IMPLS.includes(annotation.impl)) {
    throw new FishError('SPEC_INVALID', `${where}：impl=${String(annotation.impl)} 不在取值域 ${CLI_IMPLS.join('/')}`)
  }
  if (annotation.reason.trim().length < 6) {
    throw new FishError('SPEC_INVALID', `${where}：reason 太短（"未实现"三个字不是理由）`)
  }
  if (annotation.impl === 'ts' && annotation.kind !== 'read') {
    // 影子期 TS 侧没有写路径——把写命令标成 ts 等于把"写权限"偷偷授予实现层。
    throw new FishError('SPEC_INVALID', `${where}：kind=${annotation.kind} 的命令不许标成 ts（影子期 TS 只读）`)
  }
  if (annotation.impl === 'proxy-readonly') {
    if (annotation.kind !== 'read') {
      throw new FishError('SPEC_INVALID', `${where}：proxy-readonly 只允许 kind=read，收到 ${annotation.kind}`)
    }
    if ((annotation.evidence ?? '').trim().length < 6) {
      throw new FishError('SPEC_INVALID', `${where}：proxy-readonly 必须写清只读依据（源码位置）`)
    }
  }
  if (annotation.impl !== 'ts') {
    if (annotation.replacement === undefined) {
      throw new FishError('SPEC_INVALID', `${where}：impl=${annotation.impl} 必须给替代命令（未实现项不许是终点）`)
    }
    const mcpTool = annotation.replacement.mcpTool
    if (mcpTool !== undefined && !knownTools.includes(mcpTool)) {
      throw new FishError(
        'SPEC_INVALID',
        `${where}：替代里写的 MCP 工具 ${mcpTool} 不在生产工具表里（登记了一条不存在的替代）`,
      )
    }
  } else if (annotation.replacement !== undefined) {
    throw new FishError('SPEC_INVALID', `${where}：ts 实现不该有替代命令（它自己就是实现）`)
  }
}

/**
 * 把**机器枚举**的命令树与**人工裁决**表交叉校验，产出 `spec/cli-map.json` 的内容。
 *
 * 失败即抛 `SPEC_INVALID`（生成器在第 0 步调用它：**标注不合规就一个文件都不写**）。
 * @param enumeration `scripts/probe/dump-cli.py` 的输出
 * @param context 生产工具名清单（用于校验 `mcpTool` 替代确实存在）
 */
export function buildCliMap(enumeration: CliEnumeration, context: CliMapBuildContext): CliMapFile {
  const leaves = enumeration.commands.filter((node) => node.kind === 'leaf')
  const groups = enumeration.commands.filter((node) => node.kind === 'group')
  const enumeratedPaths = leaves.map((node) => node.path)
  const annotatedPaths = CLI_ANNOTATIONS.map((annotation) => annotation.path)

  const duplicated = annotatedPaths.filter((path, index) => annotatedPaths.indexOf(path) !== index)
  if (duplicated.length > 0) {
    throw new FishError('SPEC_INVALID', `cli-map 标注里有重复路径：${[...new Set(duplicated)].join(', ')}`)
  }
  const missing = enumeratedPaths.filter((path) => !annotatedPaths.includes(path))
  if (missing.length > 0) {
    throw new FishError('SPEC_INVALID', `命令树里有 ${missing.length} 条命令没有裁决：${missing.join(', ')}`, {
      hint: '在 src/domain/cli.ts 的 CLI_ANNOTATIONS 里补一条（未实现项必须显式登记，不许静默省略）',
      details: { missing },
    })
  }
  const stale = annotatedPaths.filter((path) => !enumeratedPaths.includes(path))
  if (stale.length > 0) {
    throw new FishError('SPEC_INVALID', `裁决表里有 ${stale.length} 条命令在命令树里不存在：${stale.join(', ')}`, {
      hint: '生产树删/改了命令名 → 同步删掉这条标注（否则登记表会继续声明一个不存在的契约）',
      details: { stale },
    })
  }

  for (const annotation of CLI_ANNOTATIONS) validateCliAnnotation(annotation, context.knownTools)

  const entries: CliMapEntry[] = leaves
    .map((node) => {
      const annotation = findCliAnnotation(node.path)
      if (annotation === null) {
        // 上面已经查过 missing，这里只是让类型收敛（并防止未来有人绕过检查）。
        throw new FishError('SPEC_INVALID', `命令 ${node.path} 没有裁决（枚举与裁决表不同步）`)
      }
      if (annotation.kind === 'read' && node.hasApplyFlag) {
        throw new FishError(
          'SPEC_INVALID',
          `命令 ${node.path} 带 --apply 开关却被标成 kind=read（--apply 是写操作的强信号）`,
        )
      }
      return {
        path: node.path,
        level: node.depth,
        kind: annotation.kind,
        impl: annotation.impl,
        reason: annotation.reason,
        evidence: annotation.evidence ?? null,
        replacement: annotation.replacement ?? null,
        help: node.help,
        applyFlag: node.hasApplyFlag,
      }
    })
    .sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0))

  const groupEntries: CliGroupEntry[] = groups
    .map((node) => ({ path: node.path, level: node.depth, children: [...node.children].sort(), help: node.help }))
    .sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0))

  const implCounts = Object.fromEntries(CLI_IMPLS.map((impl) => [impl, entries.filter((entry) => entry.impl === impl).length])) as Record<CliImpl, number>
  const kindCounts = Object.fromEntries(CLI_KINDS.map((kind) => [kind, entries.filter((entry) => entry.kind === kind).length])) as Record<CliKind, number>

  return {
    specVersion: 'v0',
    source: {
      files: ['p0/fish.py'],
      method: '反射 argparse 对象树（捕获根 parser 后递归 _SubParsersAction.choices），不解析 --help 文本',
      fishPy: enumeration.fishPy,
      fishPySha256_12: enumeration.fishPySha256_12,
      python: enumeration.python,
      rootCommands: [...enumeration.rootCommands].sort(),
    },
    counts: {
      level1: new Set(entries.map((entry) => entry.path.split(' ')[0])).size,
      leaves: entries.length,
      groups: groupEntries.length,
      maxDepth: entries.reduce((max, entry) => Math.max(max, entry.level), 0),
      level1AsCommands: enumeration.rootCommands.length,
    },
    implCounts,
    kindCounts,
    entries,
    groups: groupEntries,
  }
}
