<div align="center">

# fish · 大肥鱼生命系统

**给 AI 助手的长期记忆与协作层**

单机 · 单仓 · 事件溯源 —— SQLite + FTS5 + MCP + CLI + 宿主 hooks

![tests](https://img.shields.io/badge/tests-1%2C647%20passed-brightgreen)
![mutants](https://img.shields.io/badge/mutants-27-blue)
![python](https://img.shields.io/badge/python-3.11%2B-3776ab)
![license](https://img.shields.io/badge/license-proprietary-lightgrey)

[它解决什么问题](#它解决什么问题) · [特性](#特性) · [架构](#架构) · [快速开始](#快速开始) · [质量与判据](#质量与判据) · [已知限制](#已知限制)

</div>

---

## 它解决什么问题

多个 AI 会话一起干活时，有三件事反复发生：

1. **记不住** —— 换一个会话，之前定过的约定、踩过的坑、做过的决定全丢了；
2. **记太多** —— 把什么都塞进上下文，模型反而抓不住重点；
3. **互相踩** —— 两个会话同时改同一块，谁也不知道。

fish 的答案是：**记忆落库、召回算分、推送看价值、协作看看板**。
它不追求"记得多"，追求**在正确的时刻递上正确的一条**。

## 特性

| 层面 | 能力 | 关键设计 |
|---|---|---|
| **记忆** | 编码式长期记忆 | `<前缀>-<YYMMDD>-<序号>`；正文 Markdown 落盘，索引进 SQLite；事件溯源可重建 |
| **召回** | 两段式检索 | 先 AND 精确命中，无命中再 OR 回退，并**如实标记** `meta.fallback`（可观测，不假装命中） |
| **推送** | 三通道 | `inject`（会话开场）/ `tool_after`（工具调用后）/ `prompt`（用户消息边界） |
| **去重** | 内容感知 | 读过的、过期的**永久不再推**；内容变了（指纹不同）才推新版 |
| **排序** | 可插拔四件套 | RRF 跨路融合 / 三因子 `recency×importance×relevance` / MMR 多样性 / 价值门 |
| **协作** | 看板 | 谁在动哪块、有没有撞车 |
| **语料** | 历史会话 | 切成可检索的段，不重跑全量 |
| **知识库** | 离线索引 | 对 Markdown 库做 FTS，复用同一套两段式召回 |

> 四个排序开关（`push.rrf_enabled` / `push.factors_enabled` / `push.mmr_enabled` /
> `push.value_floor`）**默认全关**——它们会改变"推哪几条"，观察后再开。

## 架构

```mermaid
flowchart LR
    A[AI 会话] -->|SessionStart<br/>UserPromptSubmit<br/>PreToolUse / PostToolUse| H[hooks]
    H --> C[(SQLite<br/>events 事件库)]
    H --> T[(tool_activity<br/>动作采集)]
    C -->|投影| P[(code_index)]
    C -->|索引| F[(memory_fts<br/>FTS5)]
    P --> R[召回 / 推送]
    F --> R
    KB[(知识库<br/>离线 FTS)] --> R
    CO[(语料<br/>会话切段)] --> R
    R -->|注入包 · 提示词| A
    P --> M[Markdown 正文<br/>projects/项目/编码__标题.md]
```

**依赖方向由判据钉住**：领域模块不得 `import fish`（J2/J6）、导出面双向对账（J1b）、
行数窗口防膨胀（J4）、函数体 AST 等价（J8）。

## 快速开始

```bash
# 1. 依赖（只有一个可选依赖；缺了会让部分测试静默少跑）
python -m pip install -r p0/requirements.txt

# 2. 初始化
python p0/fish.py init

# 3. 注册项目前缀（编码需要它）
python p0/fish.py project-add demo --namespace "示例项目"

# 4. 记一条，再搜回来
python p0/fish.py remember demo "第一条记忆" --body "正文"
python p0/fish.py recall 第一条

# 5. 健康检查
python p0/fish.py healthcheck
```

要求 **Python ≥ 3.11**（用到 `tomllib` / `zoneinfo`）。

**接入 MCP**（Claude Desktop / Codex / WorkBuddy 等）：把 `p0/mcp_server.py` 注册为 stdio server 即可，参数见 `p0/install_mcp.py` 的示例。

## 质量与判据

| 项 | 读数 |
|---|---|
| 测试 | **1,680 passed / 9 skipped**（118 个测试文件；`python -m pytest -q -p no:cacheprovider`）。另有 2 条需私有 `data/` 目录的判据在本仓库里会失败——它们测的是"在生产库只读副本上复现缺陷"，不设 `FISH_PROD_DB` 就判不到 |
| 变异体 | **27 个**（`p0/mutation_*.py`）——用来证明判据**真的会失败**，不是摆设 |
| 代码规模 | `fish.py` **12,984 行** + **29 个** `fish_*.py` 领域模块。2026-11-11 拆包抽出四个：`fish_trust`（身份锚/授权）、`fish_rebuild`（重建投影）、`fish_schema`（全部 DDL + `connect`）、`fish_db`（建表/补列一族） |
| 跟踪文件 | 343 |

> 读数口径（2026-11-11 校准）：上面是**本仓库**的读数；在生产树（带私有 `data/`）里跑
> 全量是 **1,686 passed / 5 skipped / 0 failed**，两边总数一致。引用测试数时请说清
> 是**在哪棵树上跑**的——这个项目以前就因为没说清，出现过 1,166 / 1,573 / 1,647 / 1,651
> 四个互相冲突的公开值。

三条纪律写在代码里：

1. **判据必须能失败** —— 每个关键判据配一个变异体：改坏实现，判据必须红；还原后必须绿。
2. **不静默降级** —— 拿不到证据就标"判不到（SKIP）"，绝不写成"通过"。
3. **默认关闭新行为** —— 任何会改变排序/推送的开关默认关。

## 目录结构

```
p0/                主体（单文件编排 + 领域模块）
  fish.py            编排层：CLI / 领域函数 / 事件 / 投影（14.4k 行）
  fish_push.py       推送：候选生成 / 门槛 / 去重
  fish_push_rank.py  融合排序：RRF / 三因子 / MMR / 价值门
  fish_corpus*.py    语料层：采集 → 切段 → 检索 → 推送候选
  fish_kb*.py        知识库离线索引
  fish_common.py     配置 / 关键词 / 注入预算
  mcp_server.py      MCP 服务（stdio，19 个工具 + 2 个资源）
  hook_fastpath.py   hook 快速通道（关闭态早退 + 按 hook 冷却）
  mutation_*.py      27 个变异体
  test_*.py          113 个判据文件
docs/              设计 / 评审 / 对账文档（40 篇）
*.md               各阶段的设计与评审报告
```

## DSH 插件（`dsh-fish/`）

`dsh-fish/` 是 fish 在 **DeepSeek Harness（DSH）** 上的原生 TypeScript 插件。

**2026-11-09 起定位调整为「只读 + 本体能力补充」**——不再追求与 Python 侧逐字节等价，只做 DSH 里读得到、看得见的那部分。

| 维度 | 内容 |
|---|---|
| 形态 | 影子期（只读）：不注册模型可见工具、不注入上下文、不写 fish 库 |
| 能力 | `/fish shadow presence \| recall \| whoami \| health` + `conversation.view` 槽位的 canvas 面板 |
| 体量 | 2026-11-09 三轮减重：**16.2 MB → 1.78 MB**（git 跟踪体积）；`lib/index.js` 458 KB → **328 KB** |
| 判据 | `typecheck 0` / `build 0` / `unit 54/54` / `check-spec 30/30` |
| 与 Python 的关系 | Python 本体是**权威**；插件只读同一个 SQLite 库。写操作（`rebuild` / `remember` / `push` 等）一律给指引，不代执行 |

细节见 [`dsh-fish/README.md`](dsh-fish/README.md) 与 [`dsh-fish/docs/减重清单-20261109.md`](dsh-fish/docs/减重清单-20261109.md)。

## 已知限制

- **`visibility=private` 目前只是标签**：写入可选、默认 `shared`；执行层未完成——
  只有 `search_memories` 一条路径按 owner 过滤，另有 10 条读路径仍返回 private
  （`read_body` 全文、`resolve_code` 元数据、`recall` 回退列表、`list`、`neighbors`、
  `inject` 注入包、`recall --by relevance`、推送管线、`fish://index` 资源面）。
  **不要**打开 `privacy.visibility_enforced`（打开后连 owner 自己也看不到）。
- **单机单用户**：多 AI 会话共享同一个 OS 账户与数据库文件，逻辑层的"可见性"
  不构成存储层边界（正文是明文 Markdown）。
- **位置敏感**：极少数判据（`test_workspace_map`）假定仓库不在 `%TEMP%`/`AppData` 下。
- **历史文档**：`docs/` 与根目录的 md 保留了写作当时的环境与判断，属项目记录，未做追溯改写。
- **`fish.py` 仍是单文件巨石（约 14,500 行）**：拆包是已知的最大结构债（错题本 E-11）。
  2026-11-11 已把身份锚 / 合并链 / 写入鉴权抽到 `fish_trust.py`，`pytest.ini` 里也有
  行数窗口守着它不再长——但"拆开"本身还没做完，不要以为它已经模块化了。
- **脱敏的明确边界**：裸 base64 只在 **≥400 字符**时才脱敏。低于这个长度、又没有键名
  或 PEM 标记的长串**不会**被掩——那种长度无法与普通高熵文本区分，强掩会大面积损伤
  语料可用性。这条是有意的取舍，不是漏实现（判据 `test_corpus_contract` B1–B5）。
- **`session.token_ttl_hours` 出厂值是 0（永不过期）**：这是 2026-10-25 的**用户裁定**，
  不是缺陷。旋转与吊销链路是完整的（退役/合并会真的吊销），但"到期自动失效"不启用。
- **本机文件的 ACL 未收紧**：`data/index.sqlite` 与 `data/_audit/` 继承的是
  `BUILTIN\Users:(F)`——同机其他 OS 用户可读。逻辑层的归属与凭证校验不构成存储层边界。

## 更新记录

### 2026-11-11

一轮以「先修根基、再谈功能」为口径的债务清理（对照一份外部审查的 12 条致命项逐条核
当下状态，结果见 `67-对账-外部审查红项当下状态-20261111.md`）：

- **身份**：无凭证绑定路径的分界线从「该公民签发过任何 token」改为**本机身份锚的会话
  白名单**。前者实际上让"知道 citizen_id 就能接管"对所有真实身份成立（含主人）。
- **数据完整性**：`rebuild` 现在重放 `citizen.merged` / `citizen.retired{migrated_to}`。
  此前一次 rebuild 会**静默撤销**人工合并，把归属退回到已退役公民。
- **写入鉴权**：`rule` / `identity` 需要有效凭证，`fact` / `episode` / `lesson` /
  `artifact` 保持免凭证（分级口径，策略只有一份实现）。
- **知识库**：索引"新鲜度"不再只看 `MAX(indexed_at)`（它只在行变更时才动，会让
  "索引正确 + 维护跑成功"被判陈旧并静默关掉召回）；`fish healthcheck` 现在会报索引
  三态与断路器的后果。
- **hooks**：`pretool` / `posttool` 的连续超时冷却此前**从未被写入**（检查永远返回 0），
  现在按自测耗时记账并真的退避。
- **脱敏**：补上裸 base64（≥400 字符）规则，并明确低于该长度的边界。
- **测试口径**：新增 `pytest.ini`（此前从生产目录跑 `pytest` 会把备份目录里的 227 个
  测试一起收进来，导致"测试数"有多个互相冲突的公开值）。
- **结构债（E-11）**：`fish.py` 从 **14,541 → 12,984 行（−10.7%）**，拆出四个模块——
  `fish_trust` / `fish_rebuild` / `fish_schema` / `fish_db`。分层现在是
  `fish_common ← fish_schema ← fish_db ← fish_kb / fish_rebuild / fish_trust ← fish.py`，
  新模块一律禁止反向 `import fish`（J2 静态判据守着）。拆完把行数窗口**往下调**而不是放宽。

### 2026-10-29

- **推送**：结案（读过 / 过期）**永久去重**；`content_hash` 内容感知去重（内容变了才重推）；分通道时效门（fact/episode/artifact 过硬门槛，rule/lesson 免疫）
- **排序**：RRF 跨路融合、三因子、MMR 多样性、价值门——全部可配、默认关
- **身份**：绑定需证明（含历史 token，吊销 7 天内有效）、退役/合并接吊销、事件事务边界、`rebuild` 保留 visibility
- **安全**：路径穿越拦截、语料按公民过滤、私钥整段脱敏、hook 关闭态早退（0.2s）
- **诚实性**：SKIP/MISS 用退出码表达、金标缺文件 fail-open、变异体覆盖到 27 个

## 版权

© 2026 安逸。内部项目公开源码，**未附开源许可证**；引用或复用请先联系作者。
