# dsh-fish

> ## ⚠️ 2026-11-09 方向变更 + 减重状态（**先读这段，下面正文有部分已过时**）
>
> 用户拍板：「后续不准备在 DSH 进行全量实现了，DSH 侧的插件作为**只读和本体能力的补充**就行」+「DSH 更新频率高、破坏性大，插件侧要轻」。
>
> **减重已完成两批**（commit `82558b7` / `674db78`）：
> - 批次 1：删 `test/conformance/`（117 用例 + 8.2 MB 快照）、`.tmp/`、`scripts/probe/`、`NOTES-T0*.md` → **16.2 MB → 3.2 MB**
> - 批次 2：删 9 个领域模块（`inject` / `board` / `push` / `profile` / `activity` / `naming` / `surface` / `links` / `freshness`，共 4,238 行），新增极简 `domain/presence.ts` → **`lib/index.js` 458 KB → 328 KB**
>
> **当前能力面**（真跑过的）：
> - `/fish shadow presence`（在场 + 最近动态，面板数据源）、`recall`、`list`、`whoami`、`health`
> - `inject` 已降为 `python-only`（只给指引，不执行）——用终端 `fish inject`
> - 面板（`conversation.view` 槽位的 canvas）仍在，产物 `lib/client.js` 9.33 KB
>
> **判据现状**：`typecheck 0` / `build 0` / `unit 54/54` / `check-spec 30/33`（3 条红是"probe 脚本已删"，不是本批引入）。
>
> **下面正文的 §2/§3/§4 描述的是减重前的状态**（T01-T06 批次记录），保留作历史；**新增能力以本段为准**。完整裁决见 [`docs/减重清单-20261109.md`](docs/减重清单-20261109.md)。

fish（大肥鱼生命系统）在 **DeepSeek Harness** 上的官方原生实现（TS / cordis 插件）。

> **当前处于「影子期」**：`mcp__fish__*`（MCP server）与 `~/.dsh/hooks.json` 里的 4 条 fish hook
> **仍在运行**。因此本插件默认 `disabled: true`，且**只做只读对拍**，不接管任何模型可见能力。

---

## 1. 影子期三条硬边界（写进代码、可被脚本断言）

| 边界 | 为什么 | 代码事实 |
|---|---|---|
| **不注册任何模型可见工具** | 同层同名工具注册会直接抛错（`packages/core/tools/src/index.ts:747`），与 `mcp__fish__*` 并存的唯一安全方式是不注册 | `MODEL_SURFACE.toolRegistrations === 0`，`scripts/check-spec.mjs` 断言 |
| **不向模型上下文注入任何内容** | 现有 hook 仍在注入，双份注入会污染上下文且无法归因 | 全仓无 `ctx.systemPrompt` / `agent/pre-step` 调用；`MODEL_SURFACE.promptInjections === 0` |
| **不写 fish 库** | 未退役期双写者会互相覆盖，而覆盖的表现是"数据莫名少了" | 只用 `openReadOnly()`（`mode=ro` + `PRAGMA query_only=1`）；`assertWriteRejected()` 每次都真试一次写来证明拒绝生效 |

## 2. 已交付

**T01（基础设施 + 契约层）**

- **脚手架**：`package.json`（peer 只有 `@deepseek-ai/cordis`，`dsh-*` 全进 devDeps 锁 `0.2.0-rc.2`）、`tsconfig.json`、`tsdown.config.ts`（node ESM 单产物，无 client 半边）、`cordis.patch.yml`（`disabled: true`）
- **契约层 `spec/`**：`schema.sql`、`schema.fingerprint.json`、`events.json`、`tools.json`、`injection.json`、`layout/cut.json`、`contract.json`（含 spec 自摘要）
- **契约门禁**：`runStartupGate()`（版本 + 指纹 + 宿主版本 + spec 摘要），不匹配即抛错（fail closed）
- **`/fish` 命令最小只读子集**：`/fish shadow status | contract | spec | probe`
- **双跑器**：`test/conformance/{run.mjs,run.py,diff.mjs,normalize.mjs}` + fixtures + `allowlist.json`
- **判据脚本**：`scripts/gen-spec.mjs`、`scripts/check-spec.mjs`、`scripts/check-contract-startup.mjs`、`scripts/probe/*`

**T02（四条只读能力 + 逐字节对拍）**

- **领域层 `src/domain/`**：`config`（配置键表）、`names`（别名/档期/workspace）、`identity`（凭证/证据分级/whoami）、`memory`（检索/回退/列出/渲染）、`inject`（L0+L1+L2+③④⑤+裁剪链+包尾）、`health`（七项读数）——**只读 SQL，无写路径**
- **命令面**：`/fish shadow inject | recall <q> | whoami | health`（超 60 行截断并写 `.tmp/`，回显路径）
- **对拍**：T02 交付时为 **51 个用例**（cut 1 + inject 11 + recall 18 + whoami 11 + health 10），`MATCH=51 / DIFF=0 / pending=0`；T03 扩到 **78**（见下）。规范化规则自带变异自证（21/21）
- **判据脚本**：`scripts/check-shadow-readonly.mjs`（生产库零写入的两段证明）、`test/mutation/run-t02-mutations.mjs`（3 条对拍变异）
- **单元测试**：`test/unit/domain.test.mjs`（16 条：证据只降不升 / 未复刻段拒绝 / 裁剪次序 / 检索过滤 / 自检渲染）

**T04（命令等价 76 叶子 → `/fish` + 跨宿主只读管理面）**

- **命令树枚举（机器事实）**：`scripts/probe/dump-cli.py` 反射 `fish.py` 的 argparse 对象树
  （拦截 `parse_args`，**不执行任何子命令**）→ 实测 **一级 35 / 叶子 76 / 容器 12 / 最深 3 级**。
  > 与 `03-调研` §2 的"34 / 68 / 79"不一致：79 是 `add_parser(` 字面数，**68 是 `set_defaults(func=` 的条数**
  > （多个叶子共用一个 func，例如 6 条 `board *` 都指向 `cmd_board`），命令树实测叶子是 80（2026-11-09 实测；旧文写 76 是过时读数）。
  > 以实测为准，差集写进 `NOTES-T04-delivery.md` §2。
- **裁决表 `src/domain/cli.ts`**：76 条逐条登记 `kind`（read/write/ops）+ `impl`
  （`ts` 4 / `proxy-readonly` 9 / `unsupported-shadow` 38 / `python-only` 25）+ `reason` + 替代命令；
  与枚举**双向交叉校验**（漏一条 / 多一条 / proxy 标写命令 / 写命令标 ts / 替代解析不到 → 全部抛错）
- **产物 `spec/cli-map.json`**：由 `gen-spec.mjs` 生成（不手写），进 `contract.json` 自摘要；
  `check-spec` 三条新判据：覆盖 100%（对着生产树）/ 命令面注册一致 / 取值域与替代可解析
- **四档执行**：`ts` 直调领域层；`proxy-readonly` **在只读快照副本上跑 Python**（见下）；
  `unsupported-shadow` 明确拒绝 + 给替代（**不许静默成功**）；`python-only` 只给指引
- **跨宿主管理面 `src/hosts/`**：`probe`（配置/条目/matcher 方言/脚本存在性，只读）、
  `health`（每宿主 × 4 事件心跳 + 新鲜度 + "配置有但从未触发"告警）、
  `dry-run`（**只输出 diff**，合并与序列化由 `p0/install_hooks.py` 自己算，本仓不复刻）
- **`/fish hosts probe | health | dry-run`**：只读三件，**不实现 apply**（换心期才做，且需用户逐次确认）
- **判据脚本**：`scripts/check-cli-surface.mjs`（写命令全拒 + 副本零写入 + 只读档真能跑）、
  `scripts/check-hosts-dryrun.mjs`（幂等 + 零写入 + 夹具证明 diff 非空转 + 与安装器读数对拍）、
  `test/mutation/run-t04-mutations.mjs`（3 条变异）

**T03（注入包八段全实现 + 生产快照对拍 + 预算口径契约化）**

- **新增领域层（⚠️ 2026-11-09 已删，见顶部减重段）**：`board`（看板段与其取数链：presence / board_posts / tool_activity / 重叠与残留行 /
  可读名）、`push`（五类候选 + 打分 + 静默降权 + 24h 去重 + 看板变更 + 动作重叠）、`profile`
  （会话画像与 ④C 四档 `gapPolicy`）、`activity`（动作聚合）、`surface`（路径/占用面原语）、
  `naming`（可读名与会话消歧）、`links`（关联边读取）、`freshness`（内容新鲜度）、
  `token`（码点计数与截断）、`openloop`（未闭合与错题本取数）
- **注入包**：`UNSUPPORTED_SECTIONS` 清空 → **八段全实现**；看板段自带预算上限（`inject.board_max_ratio`）
  并逐行截断、候选段按"候选 → L2 → 注意事项 → 错题本 → 规矩 → 看板 → L1 条目 → L0 铁律"裁剪
- **契约**：`spec/injection.json` 新增 `budgetRule`（**fallback `int(len×1.2)`，码点计数**）与
  `pushClasses`（六类候选的实现归属，`kb: python` 是唯一边界）；`budgetRule` 由 `gen-spec.mjs` 产出
- **生产快照**：`scripts/gen-snapshot.mjs` → `test/conformance/snapshot/`（`index.sqlite` + `manifest.json`
  + `README.md`，sha256 可核）；`database.mode="snapshot"` 让两侧跑**同一份生产字节**
- **对拍**：**78 个用例**（T02 的 51 + board 4 + push 4 + attention-gap 2 + **snapshot 16**），
  `MATCH=78 / DIFF=0 / pending=0`
- **判据脚本**：`check-spec` 新增 3 条（预算口径 ×2 + 候选类归属），共 **25/25**（T03.5 后为 **27/27**，见下）；
  `test/mutation/run-t03-mutations.mjs`（3 条变异：看板缺省语义 / 24h 去重 / budgetRule）

**T03.5（契约漂移修复：`corpus` 入 spec + 删写死工具数）**

- `corpus` 定档 **`audit/yes`**：MCP 面只有 `search/read/stats`（写动作在 CLI）、`stats` 库不存在时不建库、`search` 只追加审计
- `check-spec` **删掉写死的 `EXPECTED.tools`**，新增 `spec.tools_match_production`（与 `p0` 现场逐名比对）与 `spec.tools_write_class_domain` → 判据 **27/27**
- 漂移复核：`tools.json` 仅新增 `corpus`；`events.json` 仅行号平移（过滤行号后 0 行变更）；schema/指纹/injection/cut **字节未变**

T03.5 起（沿用）与 **T05 新增**的两段只读能力：

**T05（补齐只读能力：知识库 / 项目与别名 / 语料层）**

- **知识库 `src/domain/kb.ts`**：`kb tree | open | search | stats | vault list` 全部复刻——
  强制忽略规则（`.obsidian/` `.trash/` `90-模板/`，判定层每次合并、删不掉）、逐段敏感名单（`密码学` 不算密码）、
  八类路径拒绝（`..` / 绝对 / 盘符 / UNC / ADS / 设备名 / NUL / 链接 / 越界）、tree 的"统计层 + 显示层"两分、
  双预算截断（截断了就把"怎么深入"钉在末行）、open 的三句固定声明、search 的"未建索引 + 跳过计数"。
  **影子期不写 `kb_access`**（生产每次读都追一条审计）——领域层干脆没有这个函数。
- **项目坐标 `src/domain/project.ts`**：`project status`（六列 + **每格来源** + 按列降级 + 7 条脚注 +
  retired 折叠 + `--json` 与生产同形）、`alias list`（原始行 + canonical 折叠分组）、
  `workspace list`。别名展开**复用** `names.ts`、未了事项谓词**复用** `openloop.ts`（同一件事不留第二份 SQL）。
  **不落盘**：生产默认会写 `10-项目库/项目状态表.md`，影子期只渲染不写（回执明写）。
- **语料层 `src/domain/corpus.ts`**：独立库 `corpus.sqlite` 的 `search | read | stats`——
  门牌三档（`bad_pointer` / `not_found` / `ambiguous_pointer`，多命中回候选不猜）、
  过滤下推 + `max(limit×8, 64)` 候选池 + `0.6/0.2/0.2` 排序、**出口二次脱敏**、源文件缺失声明。
  T04 说"corpus 只能 python-only"的理由是**转发路径**（快照副本里没有这个库）；只读直读不受这个限制。
- **命令面**：11 条从 `python-only` / `unsupported-shadow` / `proxy-readonly` 升为 **`ts`**
  （`kb tree|open|search|stats|vault list`、`corpus search|read|stats`、`project status`、`alias list`、`workspace list`）；
  `implCounts` 由生成器产出：`ts 15 / proxy 7 / unsupported-shadow 37 / python-only 17`。
- **对拍**：**117 个用例**（78 + 新增 **39**：kb 20 / 项目别名 8 / 语料 11），`MATCH=117 / DIFF=0 / pending=0`；
  新增三条规范化规则（`mask-elapsed-ms` / `mask-lag-seconds` / `mask-db-bytes`，都是**测量读数**，语义读数不比少）。
- **契约**：`spec/corpus.schema.sql`（语料库 DDL，由 `dump-corpus-ddl.py` 从生产 `DDL` 导出）进 `SPEC_FILES`；
  `check-spec` 新增 `spec.corpus_schema_matches_production` → **34/34**。
- **判据**：`check-cli-surface` 新增 `cli.ts_commands_zero_write`（15 条 ts 命令跑完，夹具库 + 语料库逐字节不变）；
  `check-shadow-readonly` 新增 `A.zero_write_on_corpus`（153 MB 语料库副本 sha/mtime 前后对比）；
  `test/unit/t05.test.mjs`（17 条）、`test/mutation/run-t05-mutations.mjs`（3 条变异）。

## 3. 目录

```
dsh-fish/
├── package.json  tsconfig.json  tsdown.config.ts  pnpm-workspace.yaml
├── cordis.patch.yml            # 默认 disabled: true
├── lib/index.js                # 构建产物（GitHub 安装不跑 build，故入库）
├── spec/                       # 契约层（唯一真源）
│   ├── contract.json           # contractVersion / minHostVersion / implMatrix / specFiles 摘要
│   ├── schema.sql              # 主库 DDL 快照（排 FTS 影子表，保留 memory_fts）
│   ├── schema.fingerprint.json # 逐对象指纹（可复算）
│   ├── events.json             # 事件类型 + payload 键 + 来源行号
│   ├── tools.json              # 工具清单（name/desc/schema/写入类/幂等性/impl=python；条数以生成时实测为准）
│   ├── injection.json          # L0 + 8 段：顺序 / 来源 / 预算键 / 裁剪序
│   ├── cli-map.json            # 76 条 CLI 叶子 → /fish 的映射（kind/impl/reason/替代；生成器产出）
│   └── layout/cut.json         # 中文 bigram 规则 + 24 组 Python 冻结向量
├── src/
│   ├── index.ts                # apply()：门禁 → 影子初始化 → 装配 /fish
│   ├── core/{contract,db,fingerprint,errors,host-version,logger,paths}.ts
│   ├── domain/                 # 只读能力（config/names/identity/memory/inject/health +
│   │                           #   T03：board/push/profile/activity/surface/naming/links/freshness/token/openloop
│   │                           #   T04：cli（命令裁决表 + cli-map 构建 + 帮助/拒绝文案））
│   ├── hosts/                  # T04：registry / probe / health / dryrun（只读管理面，无 apply）
│   ├── layout/cut.ts           # 切词（spec 为真源）
│   ├── ports/{spec,python}.ts  # spec 加载与校验 / CLI 只读转发（快照隔离 + 超时 + 截断）
│   └── surfaces/{commands,cli,hosts,output}.ts   # /fish 总入口 / CLI 命令面 / hosts 管理面 / 回显收尾
├── scripts/
│   ├── gen-spec.mjs            # 从 p0 事实机械生成 spec/（幂等、无时间戳）
│   ├── gen-snapshot.mjs        # 生产库 → 冻结快照（sha256 + 复制时刻，不静默覆盖）
│   ├── check-spec.mjs          # 判据②（含指纹负向用例 + 预算口径一致性 + cli-map 三条）
│   ├── check-contract-startup.mjs  # 判据③（模拟启动，走 prepareShadow）
│   ├── check-shadow-readonly.mjs   # 判据⑤（副本严格对照 + 生产现场观察）
│   ├── check-cli-surface.mjs       # 判据④（写命令全拒 + 副本零写入 + 只读档真能跑）
│   ├── check-hosts-dryrun.mjs      # 判据⑤（hooks dry-run 幂等 + 零写入 + 与安装器对拍）
│   └── probe/{probe-sqlite,probe-cut,probe-concurrent}.mjs + 抽取脚本（含 dump-cli.py / dump-hooks-plan.py）
├── test/
│   ├── unit/{core.test.mjs,apply.test.mjs,domain.test.mjs,cli.test.mjs}
│   ├── conformance/{run.mjs,run.py,diff.mjs,normalize.mjs,fixtures/,snapshot/,allowlist.json}
│   └── mutation/{run-mutations.mjs,run-t02-mutations.mjs,run-t03-mutations.mjs,run-t035-mutations.mjs,run-t04-mutations.mjs}
└── NOTES-T01-probe.md  NOTES-T01/T02/T03/T03.5/T04-delivery.md
```

## 4. 验收命令与当前读数

```powershell
pnpm install            # 首次；需要 pnpm 11（本机 11.7.0）
pnpm run typecheck      # 0 错
pnpm run build          # lib/index.js
FISH_HOME=… node scripts/gen-snapshot.mjs   # 首次/换基准：冻结生产快照（已存在则拒绝覆盖）
pnpm run check:spec     # 34/34 PASS（含指纹负向用例 + 预算口径一致性 + 工具面与 p0 逐名比对 + cli-map 三条 + 语料库 DDL 与生产一致）
pnpm run check:contract # 启动门禁 PASS
pnpm run check:readonly # 生产库 + 语料库零写入：副本严格对照 PASS（A.zero_write_on_copy / A.zero_write_on_corpus）+ 生产现场三件套前后一致
pnpm run check:cli      # 7/7 PASS（54 条写/运维命令全部拒绝 + 副本零写入 + 15 条 ts 真能跑 + ts 跑完夹具库零写入）
pnpm run check:hosts    # 11/11 PASS（dry-run 幂等 + 零写入 + 夹具证明 diff 非空转 + 与安装器对拍）
pnpm run probe          # 三个先验假设：6/6、diff=0(50/50)、4/4
pnpm run conformance    # 117 个用例：MATCH=117 / DIFF=0 / pending=0（含 16 条生产快照用例 + T05 的 39 条）
pnpm run conformance:self-test   # 比较器 + 规范化规则变异自证 21/21
pnpm run conformance:mutation    # T02 的 3 条对拍变异：改坏必红、还原必绿
pnpm test               # 77/77（core 9 + apply 4 + domain 32 + cli 15 + t05 17；需要 FISH_HOME）
node test/mutation/run-mutations.mjs  # T01 的 5 条（假绿即失败）
node test/mutation/run-t03-mutations.mjs  # T03 的 3 条（看板缺省语义 / 24h 去重 / budgetRule）
pnpm run conformance:mutation:t04         # T04 的 3 条（cli-map 漏一条 / 写标 ts / dry-run 写盘）
pnpm run conformance:mutation:t05         # T05 的 3 条（kb 忽略规则 / 别名少一跳 / 语料正文丢一截）
```

判据脚本对**生产库只读**：`p0/data/index.sqlite` 只以 `mode=ro` 打开；需要写（如 `recall` 的 access_log）
的一律先 `backup` 到临时副本，跑完即删。`pnpm test` 与 `check:spec` 需要 `FISH_HOME` 指向含
`data/index.sqlite` 的 fish 家目录（与 T01 同一前提）。**T05 起建议始终设 `FISH_HOME`**：
对拍的 golden 侧已改为"`FISH_HOME` 优先"，否则它会去读包根的兄弟目录（导出树，滞后于生产）。

## 5. 启用前置条件（**换心期**才做，影子期不要做）

0. **hooks 的安装/回滚仍归 Python**：本插件只做 `probe` / `health` / `dry-run` 三件只读，
   **没有 apply**（安装仍用 `python p0/install_hooks.py --apply`，回滚用 `--restore latest`，
   两条都要求"写完重启宿主"）。换心期若要接管安装，必须先补"先备份 sha256 + 只替换 fish 标记段 +
   连跑两次字节不变 + 失败回滚"四件事，并按 dry-run 的 diff 做逐宿主人工确认。
1. 从 profile 摘除 `mcp-fish`（否则模型会看到两套同名工具语义；同层注册还会直接抛错）；
2. 清空 `~/.dsh/hooks.json` 的 4 条 fish hook（否则注入两份）；
3. 确认 `spec/contract.json.contractVersion` 与插件常量 `SUPPORTED_CONTRACT_VERSION` 同代、且
   `schema.fingerprint.json` 与现网库一致（`pnpm run check:contract` 会回答）；
4. 把 `cordis.patch.yml` 的 `disabled: true` 改成 `false`，重启宿主。

## 6. 回滚

```powershell
# 一行回滚：把 patch 里的 disabled 改回 true，重启宿主。
#   · 本插件零写路径 → 回滚不需要还原任何数据；
#   · spec/ 与 lib/ 都是生成物 → 回滚时一并回退 git 版本即可；
#   · 若换心期已注册工具，务必先退役本插件再恢复 mcp-fish（顺序反了会撞同名）。
```

## 7. 已知差异 / 未实现

`test/conformance/allowlist.json` 是唯一登记处：每条都带 `owner` + `expires`，**过期即红**。
T02 起该清单**清零**（inject / recall / whoami / health 四条全部实现），T03 之后**仍然为零**。

仍在影子期**不覆盖**的东西（不是差异，是未实现，逐条见 `NOTES-T03-delivery.md` §5/§6）：

- 候选段的 **`kb`（知识库）类**：TS 不做 vault 检索（数据源是 `kb_vault` + 文件系统遍历 + 编码探测）。
  边界**机器可查**：`spec/injection.json.pushClasses` 里 `kb: python`；`meta.push_kb_vaults` 给出
  "库里有几个 vault"的读数；快照用例选的项目实测 kb 命中为 0，所以判据上不产生差异；
- **`timing='prompt'`** 的候选（提示词时机）：会明确抛错，属提示词推送批次；
- `board` 的 `branch` 作用域、`board_observed_overlaps` / `board_simulate`：已实现但注入包不用
  （Python 的 `board_block` 只用声明重叠与残留行），无对拍覆盖。

T04 起新增的一批**未实现项**（T05 之后：`python-only` 17 条 + `unsupported-shadow` 37 条，逐条理由在
`spec/cli-map.json` 的 `reason` 里，**机器可查**）：

- `proxy-readonly` 7 条（`list` / `events` / `neighbors` / `citizen-list` / `push stats` / `push status` /
  `workspace seed`）——每条的只读依据都写了源码行号；T05 把 `alias list` / `workspace list` 升成了 `ts`；
- 仍标 `python-only` 的读命令：`layout verify` / `kb doctor`、`kb vault scan`（运维）/ `corpus ingest|retag|forget|rebuild|doctor`
  （长事务或写面）/ `activity stats`（读本机审计文件）/ `session status`（读宿主私有环境）；
  T05 之后 `kb tree|open|search|stats|vault list`、`corpus search|read|stats`、`project status` **已是 `ts`**；
- `board show` 看着是读，实际会 `board_ensure` 建行（`p0/fish.py:11492`），所以是 `write` → 拒绝；
- 写命令一律 `unsupported-shadow`：`/fish board checkin` 会明确说"影子期不执行写操作"并给替代
  （`fish board checkin` 或 MCP 工具名），**不许静默成功**。

T05 的**新增未覆盖项**（`NOTES-T05-delivery.md` §8）：`kb *` / `corpus *` 的 `--json` 形态（传了显式报错 + 指引）、
`kb_access` / `corpus_access_log` 审计（影子期刻意不写）、`project status` 的落盘形态。

## 8. 设计偏差与待决（详见 `NOTES-T02-delivery.md` §6、`NOTES-T03-delivery.md` §5、`NOTES-T04-delivery.md` §5）

- **token 计数口径（已契约化）**：TS 没有 cl100k_base，两侧统一走 Python `count_tokens` 的 fallback
  分支（`int(len*1.2)`，`len` 数的是**码点**）。这条口径写进了 `spec/injection.json.budgetRule`，
  并由 `check-spec` 逐位比对——换心期若引入真实 tokenizer，**必须同时改 spec 与两侧实现**。
- **FTS JOIN 形态**：Python 的"真表名 + 别名并存"写法在 Node 24 的 SQLite 上直接报
  `column index out of range`，TS 用等价子查询；判据是**结果集**而非 SQL 文本。
- 注入包口径：调研文档写"8 段"/架构方案写"9 段"，实测是 **L0 + 8 个可选段**，spec 按实测记录。
- 事件口径：源码里 **27 处** `append_event`，生产库实测 **21 种**；spec 收录 27 并标 `observedInDb`。
- 宿主版本探测不到时默认**放行**（`strictHostVersion: true` 可改为拒绝）——这是显式取舍，不是遗漏。

