# 25b · 交接文档：给 `fish-主AI-3`

> **生成时间**：2026-10-25 22:10（Asia/Shanghai）
> **交接人**：`fish-主AI-2`（公民 `17928633205208049678A4A154`）
> **接手人**：`fish-主AI-3`
> **项目根**：`I:\app\DSH-Home\workspace\learn\大肥鱼生命系统\`
> **代码根**：`p0\`　**数据根**：`p0\data\`

---

## 〇、先读这三段（最重要）

### 1. 这个项目是什么

**fish 是一个「AI 记忆与协作系统」**，不是普通应用。它给多个 AI 提供：

- **记忆**（写入 → 编码 → 检索 → 衰减 → 归档）
- **身份**（公民 / 会话 / 凭证）
- **在场**（看板：此刻谁在动哪块）
- **主动推送**（在 AI 没想到时就把它该知道的推给它）

**用户原话（贯穿全项目的最高判据）**：
> 大肥鱼生命系统是给 AI 用的记忆与协作系统。它的价值不在"存了多少"，在"AI 在需要的时候能不能拿到、拿到的是不是对的"。

### 2. 三条不可动的铁律

| # | 铁律 | 出处与后果 |
|---|---|---|
| **1** | **事件表是真相，一切投影可重建** | `code_index` / `memory_fts` / 正文文件 / 看板 markdown **全是投影**，删了能重建。**动投影前先想"重建会不会丢"** |
| **2** | **正文文件是唯一副本** | 用户 2026-10-25 明确接受这条边界：`memory.recorded` 事件的 payload **只有 `body_ref`（路径），没有 body**。所以**人类删了正文文件 = 正文永久丢失**（标题/类型/标签/事件仍在）。系统做到"不崩溃"，但**做不到"不丢数据"**——这是已裁定接受的现实，**不要再当 bug 去修** |
| **3** | **归属只有一处来源：token** | `code_index.owner` 只由 `_owner_value(ident)` 决定。**没有"猜"**。token 没有权限语义（全库 `permission` 命中 0 行），它只是"署名笔" |

**铁律 1 的硬证据**（我实测过，不是听来的）：

```sql
-- events 主键是 seq（不是 id）
CREATE TRIGGER ev_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;
CREATE TRIGGER ev_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;
```

实测（在库副本上真跑）：

```
UPDATE events SET type='HACKED' WHERE seq=?   → 被拒绝：events are immutable
DELETE FROM events WHERE seq=?                → 被拒绝：events are immutable
```

### 3. 当前健康状态

> **⚠ 读数时刻 = 2026-10-25T22:18+08:00（`git` 无版本，请以你自己的实测为准）**
> **显式声明**：数据量类读数**会随每次写入漂移**。我在写完本文档后登记了一条记忆，
> 事件就从 578 变成 580、记忆 236→237、active 198→199。
> **所以这一节的数字只用来判断"量级对不对"，不要拿它当判据。**
> 真正该跑的是：`python -m unittest discover -p "test_*.py"` → 期望 `OK`。

| 项 | 读数 | 怎么复核 |
|---|---|---|
| 测试 | **443 条全绿 / 39 个测试文件** | `python -m unittest discover -p "test_*.py"` |
| 代码 | `fish.py` **8767** 行 / `mcp_server.py` **1516** 行 / 全 p0 **73 个 .py / 28220 行** | `node -e "..."` 或自己数 |
| 事件 | **580**（= 578 + 我登记的 1 条 ×2） | `SELECT COUNT(*) FROM events` |
| 记忆 | **237**（active **199**） | `SELECT COUNT(*), status FROM code_index GROUP BY status` |
| 已归档正文 | **38** | `SELECT COUNT(*) FROM code_index WHERE file_path LIKE '%/archive/%'` |
| 公民 | **36 活跃 / 0 归档** | `SELECT COUNT(*) FROM citizens WHERE archived_at IS NULL` |
| 凭证 | **33 张，有效 2 张** ← 见「注意 1」 | `SELECT COUNT(*) FROM session_tokens WHERE revoked_at IS NULL AND expires_at IS NULL` |
| 看板 | **4 块全 open / 在场 6 行** | `SELECT COUNT(*) FROM boards` |
| 项目前缀 | **16 个** | `SELECT COUNT(*) FROM code_registry` |
| MCP 工具 | **16 个** | `len(mcp_server.TOOLS)` |

**⚠ 注意 1**：今天把 `session.token_ttl_hours` 从 **24 改成 0（永不过期）**。所以**历史上签发的 24h token 大部分已过期**（33 张里只有 2 张有效）。**这是预期状态**，不是故障——新会话会拿到永不过期的新 token。

---

## 一、你必须先理解的 6 个核心概念

### ① 编码（Code）

格式 `<前缀>-<YYMMDD>-<NN>`，例：`大肥鱼生命系统-261025-15`。

**关键设计**：
- **前缀可含任意字符**（含 `-`、中文）——靠**右锚定解析**（`-(\d{2})\d{4}-(\d+)$`），不是左切分
- **编码是内容的稳定锚**：文件名是 `<编码>__<标题slug>.md`，所以**人类改了文件名/挪了位置/改了项目夹名，系统都能按编码找回来**（见 `locate_body`）
- **编码永不回收**（ADR-003）

### ② 公民（Citizen）

**一个 AI 会话 = 一个公民**（1:1，按 `host_session_key` 绑定）。

- 表：`citizens`，视图：`citizens_all`（含归档，保证 owner 永不悬空）
- 形态：`visitor` 会话级 / `role` 项目级 / `resident` 永久
- **归档 ≠ 删除**：`archived_at` 置位，身份仍在 `citizens_all` 里

### ③ 凭证（Token）

`fsh_` 前缀随机串，**明文只在签发时回显一次**，库里存 `sha256`。

**2026-10-25 新语义（用户裁定）**：

> **token 的生命周期 = 会话的生命周期。会话活着它就有效；重签时旧的就吊销。**

- **默认不过期**（`expires_at = NULL`）
- **重签吊销旧的、公民不变**（轮换）
- **显式 `ttl_hours > 0` 仍可过期**（留给子代理等临时场景）
- **手动吊销**（`fish session end`）仍然有效

**`evidence` 三级**：`machine`（来自进程 env，最可信）/ `inherited`（继承父 token）/ `self-reported`（AI 自报，最弱）。
**铁律**：MCP 面**不得签发 `machine` 级**（防 AI 自称"机器事实"）。

### ④ 看板（Board）

答案：**此刻谁在动哪块**（不是"发生了什么"，那是记忆）。

- 三张表：`boards` / `presence` / `board_posts`
- **三级 scope**：`project`（主力）/ `root`（全局聚合，给人看）/ `branch`（暂搁置）
- **真相在表，文件是渲染**（`projects/<项目>/看板-YYYYMMDD.md`，可重建）
- **未登记 = 未发生**（这是协作协议，不是礼貌）

**已实现的防线**：
- **占用面重叠检测**（`surface` 归一化 + 目录前缀语义 + 只算有效行 + 去重）
- **软门**（写记忆时若未签到 / doing 超时 → 回执带提示，**不阻断**）
- **`status=left`**（收工下板）
- **`simulate` + `assume_surface`**（单人也能验证撞车检测）

### ⑤ 推送（Push）

把"AI 该知道但没想到"的东西主动推给它。**当前存在但未接线**（`fish_push_hook.py` 已注册 `PostToolUse`）。

- 四类候选：`rule` / `association` / `situational` / `serendipity`
- 打分：`benefit - cost`，再乘静默降权
- 防茧房：意外类至少 1 条

### ⑥ 注入包（Injection Pack）

会话开始时注入 AI 上下文的那段文本。预算 **1600 token**。

**段结构**（按顺序）：
```
L0 身份（铁律）→ L1 状态 → 看板 → 规矩 → 注意事项 → 错题本 → 候选 → L2 索引
```

**裁剪顺序**（预算不够时先裁谁）：
```
候选 → L2 → 注意事项 → 错题本 → 规矩 → 看板 → L1条目 → L0铁律
```
**依据**：**可离线重建的让位于不可重建的**。看板是"此刻"，过了查不回来；错题本/规矩 `recall` 一下就有。

---

## 二、目录与文件地图

### 代码（`p0\`）

| 文件 | 行数 | 作用 |
|---|---|---|
| **`fish.py`** | **8767** | **全部核心逻辑 + CLI**（单文件，改动最频繁） |
| **`mcp_server.py`** | **1516** | **MCP 工具面（16 个工具）** |
| `daily_report.py` | — | 每日任务：报告 + 推送收口 + 看板归档 + 备份 |
| `fish_dsh_hook.py` | — | **SessionStart hook**（签发 token + 注入） |
| `fish_push_hook.py` | — | PostToolUse hook（推送） |
| `fish_posttool_hook.py` | — | **绕路提醒（已写但未注册）** |
| `fish_session_hook.py` | — | WorkBuddy 侧 hook（**含硬编码 FISH_ACTOR 的问题**） |
| `test_*.py` × **39** | — | **443 条测试**（见下节） |
| `mutation_*.py` × 11 | — | 变异自证脚本（证明判据有判别力） |
| `gov_*.py` / `merge_cdql.py` / `rename_bodies_to_slug.py` | — | 一次性治理脚本（**已完成，勿重跑**） |

### 数据（`p0\data\`）

```
index.sqlite          ← 唯一数据库（events + 投影 + 看板 + 凭证）
projects/<项目>/*.md   ← 记忆正文（唯一副本！）
projects/<项目>/archive/<年>/*.md   ← 已归档正文（38 条）
看板-YYYYMMDD.md       ← 根看板渲染
_session/             ← （待建）会话映射
_audit/               ← 审计日志（detours / hook-calls / tool-calls）
_reports/             ← 日报
```

### 文档（项目根 `*.md`，**39 份**）

见下方「三、文档索引」。

---

## 三、文档索引（按"先读什么"排序）

### 🔴 必读（依次）

| 文档 | 内容 | 为什么必读 |
|---|---|---|
| [`30-项目全景-v2-收敛版.md`](30-项目全景-v2-收敛版.md) | 项目全景 + 五条拍板 | **最高层的"这是什么、要往哪走"** |
| [`07-实现与规格偏离对账.md`](07-实现与规格偏离对账.md) | 实现与设计的每处偏离 | **别信设计文档，以它为准** |
| [`01-决策记录-ADR.md`](01-决策记录-ADR.md) | 全部架构决策（**实测 ADR-001~006 全部存在**） | 动了架构先查这里 |
| [`16-最终架构-v1-整合裁决.md`](16-最终架构-v1-整合裁决.md) | 最终架构裁决 | 看板/推送/记忆层的分界 |
| [`23-错题本-开发过程.md`](23-错题本-开发过程.md) | 踩过的坑 | **能省你一整天** |

### 🟠 分模块深入

| 文档 | 内容 |
|---|---|
| [`38-看板-意义设计与实施.md`](38-看板-意义设计与实施.md) | 看板设计 + 实施 + **附录 C（与旧台账的归属）** |
| [`31-P1a-推送系统-开发方案.md`](31-P1a-推送系统-开发方案.md) | 推送系统方案 |
| [`13-推送系统设计-v0.1.md`](13-推送系统设计-v0.1.md) | 推送系统设计原始稿 |
| [`18-身份层实现设计-P1b.md`](18-身份层实现设计-P1b.md) | 身份层（session 命令族） |
| [`22-入职包设计.md`](22-入职包设计.md) | 新 AI 接入时该收到什么 |
| [`37-正文文件名与可发现性-调研与方案.md`](37-正文文件名与可发现性-调研与方案.md) | 文件名 slug、归档、可发现性 |
| [`33-编码方案研究与研判.md`](33-编码方案研究与研判.md) | 任意字符前缀的编码方案 |

### 🟡 数据治理历史（已完成，了解即可）

| 文档 | 内容 |
|---|---|
| [`32-数据治理-项目改名与混杂清理-方案.md`](32-数据治理-项目改名与混杂清理-方案.md) | ANYI → anyi-studio 拆分方案 |
| [`36-数据治理-执行报告与项目自审.md`](36-数据治理-执行报告与项目自审.md) | 治理执行结果 + 附录 A/B |
| [`34-ANYI归属判定清单.md`](34-ANYI归属判定清单.md) | 119 条记忆归属判定清单 |
| [`30-问题报告-项目改名后核查-20261025.md`](30-问题报告-项目改名后核查-20261025.md) | 改名后核查发现的问题 |

### 🔵 外部评估（**交接后第一优先级**）

| 文档 | 内容 |
|---|---|
| [`20-外部评估-综合评估报告-20261024.md`](20-外部评估-综合评估报告-20261024.md) | 外部 AI 综合评估 |
| [`21-外部评估-产品体验报告-20261024.md`](21-外部评估-产品体验报告-20261024.md) | 外部 AI 产品体验 |
| [`22-外部评估-复核-20261025.md`](22-外部评估-复核-20261025.md) | 一日复核 |
| [`26-审查报告-fish主AI-2-20261025.md`](26-审查报告-fish主AI-2-20261025.md) | 对我（主AI-2）交付的审查 |
| [`28-回应审查-20261025.md`](28-回应审查-20261025.md) | 我对审查的逐条回复 |

### ⚪ 历史交接与记录

| 文档 | 内容 |
|---|---|
| [`06-维护交接-20261022.md`](06-维护交接-20261022.md) | 上一任（主AI-1）的交接 |
| [`25-交接文档-给fish-主AI-2.md`](25-交接文档-给fish-主AI-2.md) | 主AI-1 → 主AI-2 的交接 |
| [`24-会话全记录-卷0-索引.md`](24-会话全记录-卷0-索引.md) | 会话全记录索引（卷1/2 各 3.3MB / 500KB） |
| [`29-项目全景-v1.md`](29-项目全景-v1.md) / [`29-项目全景-目标阶段与内容.md`](29-项目全景-目标阶段与内容.md) | 全景 v1（v2 已取代） |

### 📐 规格与设计（早期）

`00-工作纪律-协议对齐.md` / `02-数据模型与最小接口规格.md` / `03-规格补充与修订-v0.2.md` /
`04-设计深挖-v0.3-存在形态与遗忘.md` / `05-WorkBuddy-MCP接入核对.md` /
`08-草案-ADR-005与006-正文检索.md` / `08-问题全景与改进方向-20261023.md` /
`09-PRD-平台化蓝图-v0.1.md` / `10-架构设计-平台化-v0.1.md` / `10-架构设计-平台化-v0.2.md` /
`11-评审报告-架构设计-v0.1.md` / `12-架构方案-对照版-主理人.md` /
`14-调研-人脑机制与再巩固.md` / `15-调研-推送时机与静默失败.md` /
`17-新增表登记-ADR建议.md` / `19-BUG报告-fish-DSH会话凭证接入-20261024.md` /
`20-系统全景-推送开发前的盘点.md` / `21-待讨论-命名空间治理.md` /
`26-待办处理-阶段A.md` / `27-待办处理-阶段BCD.md` / `30-项目全景-v2-收敛版.md`

---

## 四、测试体系（443 条，39 个文件）

**跑法**：
```powershell
cd I:\app\DSH-Home\workspace\learn\大肥鱼生命系统\p0
python -m unittest discover -p "test_*.py"
```

**读法**（按模块）：

| 模块 | 测试文件 |
|---|---|
| **P0/P1a 基础** | `test_p0.py` `test_p1a.py` `test_status_filter.py` `test_dedup.py` |
| **身份** | `test_identity_p1a/p1b/p1c.py` `test_identity_host_and_mentions.py` `test_session_start_tool.py` `test_token_lifecycle.py` `test_rename_citizen.py` `test_name_lifecycle.py` |
| **看板** | `test_board*.py`（6 个）+ `test_root_board.py` |
| **注入包** | `test_injection_blocks.py` `test_injection_status.py` `test_onboarding.py` |
| **推送** | `test_push_p0.py` `test_push_p1a_candidates.py` `test_push_stats.py` `test_profile_s1.py` |
| **归档/命名** | `test_archive.py` `test_body_naming.py` `test_rename_readpath.py` `test_rebuild_rename.py` |
| **韧性** | `test_resilience.py`（人类随意改文件不崩） |
| **工作区 / 项目解析** | `test_workspace_map.py` `test_workspace_normalize.py` `test_session_cwd.py` `test_fallback_project.py` `test_stint_multi.py` |
| **编码 / 别名** | `test_code_prefix.py`（右锚定）`test_alias_rollout.py`（别名三路径一致） |
| **MCP** | `test_mcp.py` |

**变异自证**（证明判据有判别力）：`mutation_*.py` × 11，跑法 `python mutation_xxx.py`。

---

## 五、未闭合项（接手后的工作队列）

### 🔴 最高优先级（EVAL 已证实的真缺陷）

| # | 问题 | 状态 |
|---|---|---|
| **1** | **`EVAL-261025-19`**：hook 硬编码 `FISH_ACTOR=workbuddy-main`，注入包宣称的身份与会话 token 的身份**不一致** | **未修** |
| **2** | **`EVAL-261025-20`**：看板**只在 SessionStart 注入一次**，之后无任何变更推送；多 AI 陆续开工时失效 | **未修** |
| **3** | **`EVAL-261025-21`**：token 缺失时 `remember` 照成、归属永久错、**无修正通道** | 部分（token 不再过期已缓解根因） |
| **4** | **`EVAL-261025-17`**：`add_project` 拒绝已存在且正在使用的前缀（含 `-` 的存量） | **未修** |
| **5** | **`EVAL-261025-18`**：长标题时文件名被静默截断（`title` 字段完好），像数据损坏 | **未修** |

### 🟠 用户已提出但未完成的大需求

| # | 需求 | 当前进展 |
|---|---|---|
| **6** | **"token 自动续上"**：会话 AI 带着机器码接入，MCP 自动识别身份，**AI 不再手传 token** | **已查清根因**（DSH `scrubbedParentEnv()` 删 `DSH_*` 与含 `KEY/PASSWORD/SECRET/TOKEN` 的名字，源码 `packages/subprocess/subprocess/src/index.ts:47,66-70`）。**方案待定**：用户明确"共用工作目录"+"按启动时间挑最近的不可靠"，**需要新思路** |
| **7** | **按工作流重写 16 个 MCP 工具 description**（用途/用法/**时机**） | 未开始 |
| **8** | **hooks 补全**：让"AI 要记得调的"变成"系统自动触发的" | 未开始（`fish_posttool_hook.py` 已写未注册） |
| **9** | **看板优化**（EVAL-261025-20 建议：变更推送 / 裁剪降级不清空 / 标注注入时刻） | 未开始 |
| **10** | **`fish-主AI-2` 待查**：一个**未接入 fish 的会话**收到了 fish 信息且是**乱码** | **未查清**（可能是全局 hooks + 编码问题） |

### 🟡 已识别但优先级低

- `session.archive_after_days` 配置项已加（默认 0 = 不自动归档），**归档执行逻辑未实现**
- 分支级看板（`scope='branch'`）**能力在、无入口**（`09` Q17 未决）
- `distill`（记忆提炼）、`project brief`（项目简报）——**设计存在、未实现**
- MCP `session_start` 工具的价值在 `EVAL-261025-12` 被部分反证（token 不区分通道），保留但动机修正为"少切一次进程"

---

## 六、我（主AI-2）这一任做了什么（2026-10-20 ~ 10-25）

### 主要交付

| 批次 | 内容 | 判据 |
|---|---|---|
| **T1–T10 待办** | 身份层 / 注入包 / 推送 / 看板等 | 逐项 |
| **P1-a 推送系统**（A–E 批） | 候选打分 / 静默降权 / 防茧层 / 统计 | `test_push_*.py` |
| **数据治理** | ANYI → `anyi-studio` + `fish` → `大肥鱼生命系统`（162 条改名映射） | `36` 号文档 |
| **正文文件名 slug** | `slug_title()` + `body_relpath()` 唯一入口 + 373 个文件重命名 | `37` 号 + `test_body_naming.py` |
| **看板 P1-b** | 三表 + 三级 scope + 容量纪律 + MCP 工具 + 根板聚合 | `38` 号 + 6 个测试文件 |
| **正文归档** | `archive/<年>/` + 只换目录不改名 + rebuild 不变 | `test_archive.py` |
| **文件系统韧性** | 删/改名/移动/乱码都不崩 + 按编码自愈 | `test_resilience.py` |
| **token 生命周期** | 默认不过期（会话级） | `test_token_lifecycle.py` |
| **看板三批反馈修复** | EVAL 09–16 共 12 条 | `test_board_feedback*.py` |

### 我踩过的最大的三个坑（**别重蹈**）

**① 把"实现"当成"验证"**

我有一次在 7 条判据全绿的情况下让全文搜索彻底失效——因为 `rebuild` 绕过了 `fts_text`。**判据绿 ≠ 功能活着**。

> **纪律**：每条判据都要能**失败**。判断"这段代码有没有用"，要跑真实路径，不要看测试绿。

**② 思考空转 + 正文稀薄**

用户两次指出我"什么都没交付"，而我的 reasoning 里全是"执行/写/好"的循环。**这不是格式问题，是把 thinking 当成了待办清单在刷**。

> **纪律**：**每轮先想"这一轮要给用户什么结论"，再想"下一步调什么工具"。** 工具调用是手段，不是产出。

**③ Windows 大小写是把杀器**

`CDQL-261024-01` 与 `cdql-261024-01` 的 `file_path` 只差大小写 → **同一个物理文件**。`os.path.exists()` 是**大小写不敏感**的，`os.rename` 会静默覆盖。

> **纪律**：Windows 上任何涉及"文件是否存在"的判断，都要**显式比对大小写**（`exact_exists` / `case_insensitive_twin`）。

---

## 七、接手后建议的前三步

### 第 1 步：建立你自己的身份（30 分钟）

```powershell
cd I:\app\DSH-Home\workspace\learn\大肥鱼生命系统\p0
# 1) 看你现在是谁
python fish.py whoami
# 2) 如果你要一个固定名字
python fish.py citizen-list          # 找到你的 citizen_id
python fish.py owner <你的 citizen_id>
# 3) 跑一次自检
python fish.py healthcheck
# 4) 看注入包长什么样（这是 AI 每轮看到的）
python fish.py inject --project 大肥鱼生命系统
```

**然后**：在 MCP 面调 `board(action="checkin", doing="…", surface="p0/fish.py,p0/mcp_server.py", project="大肥鱼生命系统")` —— **这是协作协议**。

### 第 2 步：跑一遍全量测试，确认基线（2 分钟）

```powershell
python -m unittest discover -p "test_*.py"
# 期望：Ran 443 tests ... OK
```

**如果不到 443 或不全绿**：说明环境有问题，**先查环境再看代码**（`01-决策记录` 里有一整节讲"测试环境认知铁律"）。

### 第 3 步：读三份文档 + 修一个 EVAL 缺陷（半天）

读：`30-项目全景-v2` → `07-实现与规格偏离对账` → `23-错题本`

然后从「五、未闭合项」里挑 **`EVAL-261025-17`（add_project 拒绝已存在前缀）**——它最小、边界清楚、且是**用户能立刻感知的体验问题**。拿它练手，走一遍"取证 → 改 → 判据 → 全量绿"的完整闭环。

---

## 八、工具面速查（16 个 MCP 工具）

| 工具 | 一句话 | 时机 |
|---|---|---|
| `remember` | 记一条记忆 | 学到新东西 / 做完一件事 |
| `recall` | 检索记忆（关键词，非语义） | 需要回忆时 |
| `neighbors` | 邻近记忆（四策略） | 想找关联 |
| `resolve_code` | 校验编码 | 不确定某条是否存在 |
| `read_body` | 读正文 | `resolve_code` 给的是元数据 |
| `add_project` | 注册项目前缀 | **新项目开工前** |
| `supersede` | 标记替代 | 新结论取代旧结论 |
| `whoami` | 我是谁 | 开场 / 怀疑身份 |
| `register` | 登记身份 | 第一次接入 |
| `rename_citizen` | 改自己的显示名 | 用户说"你是 X" |
| `close_project` / `reopen_project` | 项目三态 | 项目完结 / 重启 |
| `inject` | 权威当下（注入包） | 开场 / 换项目 |
| `healthcheck` | 自检 | 怀疑数据有问题 |
| `session_start` | 签发凭证 | 拿 token |
| `board` | 看板（show/checkin/post/simulate） | **开工前必 checkin** |

**CLI 入口**：`python fish.py --help`（`fish.py` 自带完整 CLI，与 MCP 共用同一套核心函数）。

---

## 九、给接手人的最后几句

**这个项目最难的从来不是写代码，是判断"什么才算做完了"。**

- 判据必须能失败（自己先证明它会红）
- 判据不能锚定易过期的数字（写 `EXIT 0 + FAIL 0`，不写"固定 95 条"）
- 三态分明：**绿 / 跳过 / 判不到**——**缺证据不是通过**
- 声明你跑的是**哪一刻的树**（在制品会漂）

**还有一件事**：用户对**诚实**的要求高于对**进度**的要求。做不到就说做不到，有边界就说边界。他会记住你说过的话，并在很久之后拿它对照你的实际行为。

---

---

## 十、本文档自身的核验状态（**请据此判断可信度**）

我（主AI-2）在交付后**逐条核验**了这份文档里的可验证断言。结论：

### ✅ 已用代码/实测验证（可信）

| 断言 | 验证方式 | 结果 |
|---|---|---|
| `fish.py` 8767 行 / `mcp_server.py` 1516 行 | 数文件 | ✅ 一致 |
| 全 p0 = 73 个 `.py` / 28220 行 | 数文件 | ✅ 一致 |
| 39 个测试文件 | 数文件 | ✅ 一致 |
| 16 个 MCP 工具 | `len(mcp_server.TOOLS)` | ✅ 一致 |
| `session.token_ttl_hours = 0` / `archive_after_days = 0` | `get_config` | ✅ 一致 |
| 注入预算 1600 | `get_config` | ✅ 一致 |
| 已归档正文 38 条 | SQL | ✅ 一致 |
| **铁律 1：events 不可改** | **在副本上真跑 UPDATE/DELETE** | ✅ **都被 `RAISE(ABORT)` 拒绝** |
| **铁律 2：正文不在事件里** | 遍历 237 条 `memory.recorded` payload | ✅ **带 body 的 0 条** |
| 文档索引里列的 26 个 `.md` | 逐个 `Path.exists()` | ✅ 全部存在 |
| hooks 只有 `SessionStart` / `PostToolUse` | 读 `hooks.json` | ✅ 一致 |
| `fish_posttool_hook.py` 已写未注册 | grep 配置 | ✅ 一致 |
| **EVAL-19 未修**（hook 仍硬编码 `workbuddy-main`） | grep `fish_session_hook.py` | ✅ 确认未修 |
| **EVAL-18 未修**（文件名截断处无省略号） | 读 `slug_title` | ✅ 确认未修 |
| **EVAL-20 未修**（无 hook 处理看板） | grep 6 个 hook 脚本 | ✅ 确认未修 |
| **EVAL-21 未修**（无 owner 修正通道） | grep `fix-owner` / `fix_owner` | ✅ 确认未修 |
| **EVAL-17 未修**（`add_project` 未先查库） | 读字符校验段 | ✅ 确认未修 |

### ⚠️ 已修正的错（初版写错了，这里是修正后的值）

| 初版写的 | 实际 | 原因 |
|---|---|---|
| 事件 578 / 记忆 236 / active 198 | **580 / 237 / 199** | **我写完文档后又登记了一条记忆**——数据量会漂，所以改成了"读数时刻 + 复核命令" |
| 测试分组只列了 35 个文件 | **39 个**（漏了 `test_alias_rollout` / `test_code_prefix` / `test_fallback_project` / `test_stint_multi`） | 分组时凭印象，没数全 |
| 铁律 1 只说"有触发器锁" | 补上了 **触发器的 SQL 原文 + 实测拒绝输出** | 断言要有证据 |

### ❌ 无法验证（**读者须知**）

| 内容 | 为什么无法验证 |
|---|---|
| **各文档"内容"一列的概括** | 我读的是标题和规模，**没有逐篇重读全文**。概括可能不准——**以文档原文为准** |
| **16 个工具的"时机"一列** | 那是我根据**使用经验**写的，**不是代码里的东西**。代码里只有 `description`，没有"时机"字段 |
| **"三个最大的坑"** | 是我的**亲身经历与归因**，不是可复现的断言 |
| **"三条铁律"这个提法** | **是我起的名字**。铁律 1/2/3 的内容都有代码依据（见上表），但"三条铁律"这个编号体系**系统里没有** |
| 各节里"为什么这样设计"的叙述 | 多数能从 `fish.py` 的注释里找到对应，但**我没逐条定位行号** |

**一句话**：**能验证的我都验了并给了方式；不能验证的我都标了"无法验证"，请不要把我的概括当成事实。**

---

**交接完成。** 有问题先查文档，文档没写就查代码（`fish.py` 里的注释密度很高，很多设计理由是写在代码里的，不是写在文档里的）。

---

---

# 十一、勘误 · 2026-10-25 23:40（接手核验 + 修复后追加）

> 由 **fish-主AI-3** 在接手核验并完成一轮修复后追加。**原文一个字未改**（历史快照保留），本节只列「原文读数 → 实测」的差异与状态变更。所有读数带时刻；数据量类会漂，复核命令见每行说明。

## 11.1 读数勘误（对比原文 §〇「当前健康状态」表）

| 项 | 原文（22:10 快照） | 实测（2026-10-25 23:40） | 复核命令 / 说明 |
|---|---|---|---|
| 测试 | 443 条 / 39 文件 | **467 条 / 41 文件**（23:40 快照为 463；收尾 +2 归属边界判据，会漂） | `python -m unittest discover -p "test_*.py"`；新增 `test_hook_encoding.py`（8）+ `test_eval_fixes.py`（16） |
| 代码行数 | fish.py 8767 / mcp_server.py 1516 / p0 28220 | **约 8.9k / 约 1.52k / 约 28.8k（76 个 .py）**；会随改动漂，复核：`node -e` 或 `read` 按 `split(/\r?\n/)` 去尾空行 | 原文用 `split('\n')` 计数（每文件多算 1 行）；**本轮修复批新增约 190 行**（`fix_owner` / `board_seen` / 重建重放等），不要拿旧读数对比 |
| 事件 | 580 | **约 590**（漂移） | `SELECT COUNT(*) FROM events`；本轮新增事件类型 `memory.owner_fixed`（第 20 种） |
| 记忆 | 237（active 199） | **约 240（active 约 201）** | 本轮写入 `-17`/`-18` 两条 + 测试夹具 |
| 公民 | 36 | **37** | `SELECT COUNT(*) FROM citizens WHERE archived_at IS NULL` |
| 凭证 | 33 张 / 有效 2 张 | **34 张 / 有效 22 张** | 原文「有效 2」只算 `expires_at IS NULL`；按「未过期」口径应含 `expires_at > now` |
| 档期 | 0 行 | **9 行** | 各会话已 `session start` 开档；`SELECT COUNT(*) FROM project_stints` |
| 看板 | 4 块 / 在场 6 行 | **4 块 / 在场 6 行**（空壳 board 4 已删，新增 board 5「大肥鱼生命系统」项目板） | `SELECT id,scope,scope_key,status FROM boards` |
| 项目前缀 | 16 | 16 | `SELECT COUNT(*) FROM code_registry` |
| MCP 工具 | 16 | 16 | `len(mcp_server.TOOLS)` |

## 11.2 事实勘误

1. **`fish_posttool_hook.py` 并非「已写未注册」**：DSH 侧 `hooks.json` 确实未注册（这半句对）；但 **WorkBuddy 侧 `settings.json:1378-1400` 已注册 PostToolUse（matcher `Read|Bash|Glob|Grep|Write|Edit`）**——它是**活链路**。若照原文当死代码删掉，会破坏 WorkBuddy 侧真实运行的绕路提醒。（QA 2026-10-25 实测；已记 fish `大肥鱼生命系统-261025-18`）
2. **`test_board*.py` 是 5 个（不是 6 个）**：`test_board.py` / `test_board_archive.py` / `test_board_feedback{1,2,3}.py` 共 5 个以 `test_board` 开头，加 `test_root_board.py` = 6 个看板测试文件。
3. **`project_stints` 已非 0**：原文 §〇 与 §五 V1 的「恒 0」已过期（现 9 行）——`--project` 与 `--cwd` 都会开档期。

## 11.3 未闭合项状态更新（对应原文 §五）

| # | 项 | 状态（2026-10-25 23:40） |
|---|---|---|
| EVAL-261025-17 | `add_project` 拒绝已存在前缀 | **已修**：MCP 与 CLI 统一「先查库、再校验字符」；存量含 `-` 前缀报「已存在」，新前缀仍禁 `-`（判据 `test_eval_fixes`） |
| EVAL-261025-18 | 长标题文件名静默截断 | **已修**：`slug_title` 截断加 `...` 标记且总长 ≤24；`title` 字段完整（判据 `test_eval_fixes`） |
| EVAL-261025-19 | hook 硬编码 `FISH_ACTOR=workbuddy-main` | **已修**：`fish_session_hook.py` 改为按会话键（`codebuddy:<id>`）查已有公民；查不到诚实降级，不再冒充（判据 `test_eval_fixes`） |
| EVAL-261025-20 | 看板只在 SessionStart 注入一次 | **已修**：新增 `board_seen` 快照表 + 看板变更推送候选（注入记录快照，变更出候选）（判据 `test_eval_fixes`） |
| EVAL-261025-21 | 归属永久错、无修正通道 | **已修（通道）**：新增 `fish owner-fix --code <编码> --citizen <公民>`，追加 `memory.owner_fixed` 事件 + 更新投影（判据 `test_eval_fixes`） |
| #10 乱码 | 未接入 fish 的会话收到 fish 信息且乱码 | **根因已定位并修复**：PostToolUse 推送 hook stdout `ensure_ascii=False`（机制：Python 在 Windows 管道下 stdout=gbk，写出 GBK 字节；消费方按 UTF-8 读入）；已收敛到 `hook_io.emit()` + 8 条守护测试（`test_hook_encoding`） |
