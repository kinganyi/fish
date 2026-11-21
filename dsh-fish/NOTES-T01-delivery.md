# NOTES · T01 交付记录（基础设施 + 契约骨架 + 双跑器）

> 执行：工程师（Alex）｜时刻：2026-10-27 13:37–14:30（Asia/Shanghai）
> 基线：DSH `0.2.0-rc.2`｜宿主 Node `24.21.0`（验收跑 `v24.19.0`）｜Python `3.11.9`
> 生产库：`大肥鱼生命系统/p0/data/index.sqlite`（**全程只读**）｜未改动 `p0/`、`fish-home/`、`data/`

---

## 1. 步骤 1：脚手架（做了什么、为什么这么做）

| 文件 | 关键决策 |
|---|---|
| `package.json` | `name: dsh-fish`、`type: module`、`dsh.bundle.patch: ./cordis.patch.yml`（**无 `dsh.client`**：影子期不挂 client）；`peerDependencies` **只有** `@deepseek-ai/cordis@^4.0.0`；`@deepseek-ai/dsh` 与 `dsh-commands` 进 devDeps 并锁 `0.2.0-rc.2`（本机版本）；`schemastery@3.18.4` 进 dependencies（社区模板同做法） |
| `tsdown.config.ts` | node ESM 单产物 `lib/index.js`（显式 `entryFileNames`：tsdown 0.22 默认出 `.mjs`，与 `exports` 不一致会变成"装得上但 import 不到"）；`deps.neverBundle` 外置 `@deepseek-ai/*`（打包会得到第二份 cordis） |
| `cordis.patch.yml` | 一行 `insert`，**`disabled: true`**；注释写清启用前置三件事（摘 MCP、清 hook、版本同代） |
| `pnpm-workspace.yaml` | pnpm 11 的 `allowBuilds`（只有 `esbuild: true`，其余 5 个原生依赖显式 `false`）——否则 `tsdown` 找不到 esbuild binary |
| `src/index.ts` | `apply()`：`prepareShadow()`（契约门禁 → 只读打开 → spec 加载）→ `ctx.inject(['commands'])` 注册 `/fish`。**两段式装配**：缺 `commands` 服务时插件其余部分照常存活（行级 `inject` 会卡整插件） |
| `src/surfaces/commands.ts` | 只有 `shadow status / contract / spec / probe` 四个只读子命令；回显里永远带"影子期三条纪律" |

`apply()` **不做**的事（都被断言钉住）：注册工具、注入上下文、写库。

## 2. 步骤 2：契约层 `spec/`（生成而非手写）

`scripts/gen-spec.mjs` 从 p0 事实机械生成，**幂等**（连跑两次 7 个文件 sha256 全同，实测 `IDEMPOTENT=True`）：

| 文件 | 来源与口径 |
|---|---|
| `schema.sql`（420 行） | `sqlite_master.sql` 导出；排 FTS 影子表（`memory_fts_*`）与 `sqlite_sequence`，**保留 `memory_fts` 本体定义**。对象计数 `table=28 view=2 index=29 trigger=2` |
| `schema.fingerprint.json` | 逐对象 hash + 两档总指纹（全量 / 业务）；做法在 `src/core/fingerprint.ts`（**唯一实现**，脚本 import 构建产物，不重写） |
| `events.json` | 静态解析 `append_event(...)` 调用点（含 `_EVENT_BY_PROJECT_STATUS` 展开）+ 库内 `SELECT DISTINCT type` 交叉核对：**声明 27 种 / 库内观测 21 种**；每条带 payload 键、必填键、来源行号、`observedInDb` |
| `tools.json` | **`import mcp_server.TOOLS` 原样导出**（不解析嵌套字面量）：18 个工具，含 `inputSchema`、`writeClass`、`idempotent`、`impl: python`、`pythonSymbol` |
| `injection.json` | `INJECTION_SECTIONS`(8) + L0 = 9 段；装配顺序（L1 → 看板 → 规矩 → 注意 → 错题本 → 候选 → L2）、裁剪序（候选 → L2 → 注意 → 错题本 → 规矩 → 看板 → L1条目 → L0铁律 → L1）、预算键（`inject.budget_tokens=1600`、`inject.board_max_ratio=0.25`、`inject.rules_per_source=3`）、包尾两行规则 |
| `layout/cut.json` | 规则（`latin`/`cjk`/`lowercase`/`tokenOrder=latin-then-cjk`/`cjkRun{overlapping-bigram,2,1}`）+ **24 组向量，期望值由 Python 现场算并冻结** |
| `contract.json` | `contractVersion: v0`、`minHostVersion: 0.2.0-rc.2`、`implMatrix` 18 块全 `python`、`capabilities` 18 项、`concurrency`（WAL / busy 5000 / `BEGIN IMMEDIATE` / 长事务 50ms 上限）、`shadowPolicy`（0 工具 0 注入 ro），以及**其余 6 个 spec 文件的 sha256** |

## 3. 步骤 3：双跑器

- `run.mjs`（TS 侧）：执行器表里**只有 `cut`**（纯函数，调生产 `lib.cutTokens`）；未实现的 op 显式 `pending` + 原因，**不写假实现凑绿**。
- `run.py`（Python 侧）：`backup` 生产库到临时目录 → `mcp_server.FishServer(temp/data)`（**就是模型今天看到的那条路径**）→ `call_tool('inject'|'recall'|'whoami')`；`whoami` 在副本里现签一条 machine token。
- `normalize.mjs`：**唯一**一份规范化实现（5 条字符串规则 + 2 条行规则，逐条回报命中次数）。
- `diff.mjs`：`MATCH` / `DIFF`（首差异路径 + 两侧片段）/ `PENDING`（**必须** allowlist 且未过期），另有 `--self-test`（6/6：相等必 MATCH、改一字必 DIFF、规范化真抹平时间戳、未登记 pending 必红）。
- `allowlist.json`：3 条 `pending-impl`（inject/recall/whoami → owner T02，到期 2026-11-15）。

## 4. 步骤 4：验收读数（全部本机可复现）

| # | 判据 | 命令 | 退出码 | 关键读数 |
|---|---|---|---|---|
| 1 | `pnpm typecheck` 0 错 | `pnpm run typecheck` | **0** | `ts_errors=0`（strict + `noUncheckedIndexedAccess`） |
| 2 | 对现网库只读跑指纹校验通过 | `node scripts/check-spec.mjs` | **0** | **22/22 PASS**；`fingerprint=04cbfc6692d042d0…`、`business=f1ff2c7b3ecd…`、业务表 **21**、spec 摘要 6 个文件 |
| 3 | 改 `contractVersion` 后启动必抛错 | `node scripts/check-contract-startup.mjs` + 变异 | 正常 **0** / 变异后 **1** | 正常运行 `PASS 启动门禁通过`；把 spec 改成 `v1` 后 `CONTRACT_MISMATCH … spec=v1 / 插件支持=v0` |
| 4 | 干净 profile 里 `dsh plugin add` 成功 | `DSH_HOME=<dsh-fish>/.tmp/dsh-home dsh plugin add --profile probe <repo>` | **0** | `+ dsh-fish link:…`、`Done in 1.5s`；profile `package.json` 的 `dsh.profile.bundles = ["@deepseek-ai/dsh-base","dsh-fish"]`（bundle patch 被登记） |
| 5 | 三个先验假设读数 | `node scripts/probe/{probe-sqlite,probe-cut,probe-concurrent}.mjs` | **0 / 0 / 0** | 6/6 pass、`diff=0`(50/50)、4/4 pass（`busyErrors=0`；反向对照 `busy_timeout=0` 时 104 次 BUSY） |
| 附 | 单元测试 | `pnpm test` | **0** | **13/13**（`core.test.mjs` 9 条 + `apply.test.mjs` 4 条；后者用假 ctx 跑真 `apply()`，断言"只装配 `/fish`、零工具、零注入"） |
| 附 | 双跑器 | `node test/conformance/{run.mjs,run.py,diff.mjs}` | **0** | `MATCH=1`（cut-tokens 真 0 diff）、`PENDING=3`（allowlist 未过期）、`DIFF=0` |
| 附 | 比较器自证 | `node test/conformance/diff.mjs --self-test` | **0** | 6/6 |
| 附 | 生成器幂等 | `node scripts/gen-spec.mjs` ×2 | **0** | 7 个产物 sha256 前后全同 |

> 判据 4 的临时 profile 建在 `dsh-fish/.tmp/dsh-home/`（**不碰** `C:\Users\15596\.dsh`）。执行完后已删除，创建/删除都记录在 §7。

## 5. 步骤 5：变异（5/5 符合预期，每次都在 `finally` 还原并重建）

| # | 改坏什么 | 判据 | 变异后读数 | 还原后 |
|---|---|---|---|---|
| M1 | `if (!delta.fingerprintMatched)` → 短路成 `false && …` | 判据② `check-spec.mjs` | **红**：`FAIL gate.fingerprint_negative_test 门禁没有抛错` → 21/22 | 复绿 22/22 |
| M2 | `contractVersion` 校验短路 + spec 改 `v1` | 判据③ `check-contract-startup.mjs` | **红（假绿）**：启动竟然 `PASS`（exit 0），即"改版本也不拦" | 复绿 exit 0 |
| M2-control | 只把 spec 改成 `v1`（源码不动） | 判据③ | **红（正确拦截）**：`CONTRACT_MISMATCH spec=v1 / 支持=v0` exit 1 | — |
| M3 | 切词 bigram 滑窗少一组（`i + window + 1 <= len`） | `probe-cut.mjs` | **红**：`FAIL diff=50/50`，首个差异如 `ts(12)=[…"常用","用会"]` 重复 | 复绿 `diff=0` |
| M3-conformance | 同一条切词变异 | `test/conformance/diff.mjs` | **红**：`DIFF=1 matched=0` | 复绿 `MATCH=1` |

> M3 第一次跑时是**假绿**（probe-cut 仍 PASS）：根因是 `probe-cut.mjs` 里当时**复制了一份** `cutTokens`。
> 已改为 import `lib/` 里的生产实现（并顺带把 `probe-sqlite.mjs` 的 MATCH 表达式也接上 lib），
> 复跑后 M3 变红——这条自我纠正是本轮最有价值的发现之一：**探针自己漂了会给出假绿**。

## 6. 与设计的偏差 / 假设记录

| # | 偏差或歧义 | 处理 | 理由 |
|---|---|---|---|
| D1 | 任务书写注入包"8 段"、架构方案写"9 段" | spec 记 **9 段 = L0 + 8 可选段**，L0 用 `kind: "l0"` 单列、`trimRank` 最低（最后才降级为 `l0_min`） | 实测 `INJECTION_SECTIONS` 是 8 项且 `sections` 参数**不能关 L0**；两种口径都对了一半，以代码为准 |
| D2 | 任务书写"21 种事件" | spec 收 **27 种声明**（源码调用点）+ 标 `observedInDb`，其中观测 21 种为判据 | 21 是**观测值**（03 号文档同口径）；丢掉另外 6 种会让 spec 无法覆盖"从未触发/运维脚本写入"的分支。生成脚本对"观测数≠21"和"库内出现未登记类型"双向硬失败 |
| D3 | `spec/conformance/`（架构方案） vs `test/conformance/`（任务书） | 放 `test/conformance/`；`spec/` 只放契约数据 | 任务书优先；双跑器是"测试手段"，不是"契约内容" |
| D4 | 宿主版本探测不到时怎么办（架构方案未定） | 默认**放行**并记 `hostVersionSatisfied=false`；`strictHostVersion: true` 改为拒绝 | 影子期插件可能被装进没有 `@deepseek-ai/dsh` 的进程（验收脚本）；"探测不到"不等于"版本不对"。两档都可见，不是静默 |
| D5 | T01 是否要实现 inject/recall/whoami 的 TS 侧 | **不实现**，显式 `pending` + allowlist（owner T02 / 到期 2026-11-15） | 这三条的实质是 `domain/{inject,memory,identity}`，是 T02 的判据（≥120 fixtures 0 diff）。T01 写一版临时实现只会制造"两份会漂的真相"，且让 `diff=0` 失去意义 |
| D6 | 双跑器的"真 0 diff"从哪来 | 用 `cut`（能力块 #12，纯函数、生产代码 `src/layout/cut.ts`） | 需要一个**两边都是生产实现**的 op 来证明管线不空转；注入/检索的对拍留给 T02 |
| D7 | `pnpm` 11 的构建白名单 | `pnpm-workspace.yaml` 的 `allowBuilds`（`esbuild: true`，其余 5 个 `false`） | package.json 的 `pnpm` 字段在 11 已不被读取 |
| D8 | 判据②如何证明"指纹校验没被删掉" | 加 `gate.fingerprint_negative_test`：把 spec 复制到临时目录、改坏一个字符（并同步摘要），断言门禁抛错 | 正向校验在"校验代码被删"后仍是绿的；只有负向用例能把"校验还在不在"变成可观测事实（M1 靠它变红） |

## 7. 副作用与清理（对工作区之外只碰了这些）

| 动作 | 路径 | 状态 |
|---|---|---|
| 意外创建（`dsh plugin add` 未指定 `DSH_HOME` 时默认落家目录） | `C:\Users\15596\.dsh\profiles\probe` | **已删除**（4 个条目：`.plugin-manager` / `cordis.patch.yml` / `package.json` / `pnpm-workspace.yaml`） |
| 判据 4 的干净 profile（显式 `DSH_HOME`） | `dsh-fish\.tmp\dsh-home\profiles\probe` | 已创建、已验证、**已删除** |
| pnpm 临时 shim（本机 pnpm 不在 PATH） | `%TEMP%\pnpm-shim\pnpm.cmd` | 留在 TEMP（不在仓库内） |
| `lib/`、`node_modules/`、`.tmp/` | `dsh-fish/` 内 | `node_modules`/`.tmp` 已 gitignore；`lib/` **入库**（GitHub 安装不跑 build） |

## 8. 遗留与风险（交给 T02 与后续）

1. **conformance 的 3 条 pending 必须在 2026-11-15 前清零**（否则 `diff.mjs` 直接报红）——这是刻意的到期机制。
2. **`link:` 安装会把中文路径写成乱码**：`dsh plugin add <本地目录>` 生成的 profile `package.json` 里路径变成
   `澶ц偉楸肩敓鍛界郴缁?`（GBK/UTF-8 混淆）。说明"本地 link 装插件"在非 ASCII 路径下不可靠，
   **正式分发必须走 `pnpm pack` → npm/tarball**（或 tarball 直装），不要依赖 `link:`。
3. **`spec/schema.sql` 里含 `presence` 表的中文注释**（来自 p0 的 DDL 原文）——这是正常内容，但提醒：`schema.sql` 是快照，读它的人不要以为可以手工改库。
4. **`init`/`migrate`/`backup` 等写路径全部未实现**（影子期按设计不做）；换心期需补 `maintenance` 检测与 `openReadWrite`。
5. **宿主版本探测依赖 `@deepseek-ai/dsh` 可解析**；若将来 dsh 包结构变化（`exports` 不含 `./package.json`），探测会退化到 `unknown`——已用 `hostVersionSatisfied` 让它可见。

## 9. 需要主理人补充（不阻塞本批，但影响下一批）

1. `kinganyi/fish` 远端是否已有 `dsh-fish/` 分支约定（本轮未联网查证）。
2. `spec/` 是否作为**发布产物**随包分发（现在已进 `files`）；若远端要求 spec 由 CI 生成，需同步流水线。
3. T02 的 fixtures 规模（架构方案写 ≥120）是否按能力块分摊，以及 allowlist 到期日是否统一为 2026-11-15。
