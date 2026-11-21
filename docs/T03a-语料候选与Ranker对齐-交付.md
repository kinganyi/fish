# T03a · 语料侧候选与 Ranker 对齐 · 交付（2026-10-27 · **R2 收口轮**）

> 任务书：`54-方案B-开发AI-推送合流-20261027.md` §二（Wave 1）；
> 口径：`52-并行开发协同总纲-20261027.md`（冲突以总纲为准）；
> 设计依据：`34-方案设计-索引离线化与维护管道-20261027.md` §3.3/§3.4/§3.5。
> 工作区：`learn/fish-push`（分支 `dev/fish-push-261027`，基线 `e8acaf4`，R2 起点 `3fe39a0`）。
> **生产树一行未改**；`data/` `_audit/` `_golden/` 只读（真库只以 `mode=ro` + `tempfile` 快照读过）。
> 上一轮：`3fe39a0`（首轮交付，评审 `docs/评审报告-T03a.md` 判 🟠×5 / QA `docs/QA-T03a.md` F1–F4）。
> 本轮 = **按首轮 🟠 与 QA F1–F4 逐条闭合**，逐条对照见 §10。

---

## 0 · 结论（先行）

| 项 | 结论 |
|---|---|
| **IS_PASS** | **YES**（R2 范围内；未闭合项见 §6，其中"绝对 50ms → T04 红项"是**明示未达标**，不是待办） |
| 提交 | `fix(T03a): global dup count + dict freshness + structural C5 + semantic_kind (R2)`（读数见 §9） |
| 判据 | C0–C7 共 **31** 条：`Ran 31 / OK (skipped=1)`（未设真库时那 1 条 skip = PerfTest，属"判不到"） |
| 变异自证 | **M10 / M11 / M12 / M13 / M14 / M15 全被抓（6/6）**，逐字节还原 sha256 一致，其余判据无新红 |
| 契约 | `test_corpus_push` **27/27 OK**（既有断言零改动、零放宽） |
| 全量回归 | `python -m unittest discover -p "test_*.py"` → **Ran 1434 / OK (skipped=1) / 478.5s**（HEAD `d1172aa` 上复跑，2026-10-27 22:28→22:35 +08:00；提交前同代码另一次 453.7s） |
| 未闭合 | ①**54 C5 绝对 <50ms 未达**（本轮自测 8 查询里 3 个超线，最大 306.7ms）→ **T04 红项**；②`dup_penalty` 的真实接线在 T03b（`fish.py` 不在本批写作用域）；③R3（分项和==score）与 `score > 0` 门槛口径归 T03b |

---

## 1 · 口径登记（先读这一节 · 与 54 号原文的差异都在这里）

### 1.1 `kind` 与 `kind_class` 分家（主理人已裁决，不变）

54 号 §2.2 要求候选带 `{kind:"corpus", …}`，而 `test_corpus_push.py:143` 锁死
`kind == "conv_related"`。按裁决执行：`kind` **保持** `conv_related`；新增语义层
字段 `kind_class = "corpus"`。R2 补上"配套"（首轮 🟠 S1 的缺口）：

- 常量只有一份：`fish_corpus.PUSH_KIND_CLASS = fish_corpus_rank.KIND_CLASS_CORPUS`；
- **归一入口** `fish_corpus_rank.semantic_kind(cand)`：`kind_class` 优先 → `conv_related`
  → corpus → `kb`/`kb_related` → kb → 其余（含未知/空）→ **mem 且不抛**（可传 `notes`
  记账）。三类映射各有一条判据，未知 kind 一条判据（C6）。

### 1.2 门牌一字不变

`conv:<host>:<sid8>#<loc>[.<part>]` 仍由 `store.push_pointer` **唯一**生成
（`fish_corpus.py` 里没有任何一处自己拼 `conv:`）。C4 不新造锁，直接**跑**既有用例
`test_corpus_push.CandidateTest.test_candidate_is_produced`，并补 `result.skipped` 检查
（首轮 QA F6：内层用例被跳过 ≠ 通过）。

### 1.3 `score` 与 `score_breakdown` 分属两个阶段（首轮 🟠 S2 的收口）

- 候选带 **`score_stage="first_phase"`** 与 **`rerank=None`**；
- T03a 的 `score` **就是** `fish.py._cand` 的 `(benefit − cost) × weight`（无 scorer 路径
  时是检索 relevance 的占位）——**不是** `score_breakdown` 四项的加权和。两者量纲不同，
  **禁止互相验算**；34 §3.5 的 R3 只在重排**之后**成立；
- T03b 的合同：重排后**原子替换** `score`/`score_breakdown`，把 `score_stage` 置
  `SCORE_STAGE_RERANK="rerank"`，并把 `rerank` 从 `None` 写成重排器的名字。
  阶段名只在 `fish_corpus_rank` 定义一次（`SCORE_STAGE_FIRST/RERANK/SCORE_STAGES`），
  `fish_corpus.py` 与 T03b 都不许再写字面量；
- 判据 C7 用**独立 oracle**（候选自带的 `benefit/cost/weight` 复算首阶段公式）钉住
  "score 来自哪条公式"，**没有**任何 "sum(breakdown) == score" 形态的断言；
- `combine_features` 的值域与门槛口径写明（首轮 🟡 Y4）：正权 0.95 / 负权 −0.30 →
  值域 **[−0.30, +0.95]**、**不夹紧**；若 T03b 拿它当最终 `score`，`score > 0` 门槛
  （`fish.py:8758`）的口径必须显式拍，且 R3 会退化成恒真复述（要用手算 oracle 判）。

### 1.4 `freshness` 与 mem 侧**同型**（首轮 🟠 S4 的收口）

- 候选字段 `freshness` = **dict** `{factor, band, age_days}`，**与 `fish.py._cand`
  逐键相同**（`_cand` 就是按这三个键写的）；T03b 可以直接读 `cand["freshness"]["factor"]`；
- `score_breakdown["freshness"]` 保留**数值 factor**（明细要能复算/相加），两个读数
  **出自同一次计算**（`freshness_reading` 一次算完，两个出口），不是各算一遍；
- 刻度与分档：`exp(-age/180)`；band 阈值 0.7 / 0.35（与 `fish.memory_freshness` 同组数，
  那边是函数内字面量，`fish.py` 不在本批作用域，所以这里抄常量 + 判据钉口径）；
- 时间缺失/坏 → `factor=1.0`、`band="fresh"`、`age_days=None`（"没记时间"≠"很旧"，
  `None` 也**不是** 0：0 会被读成"就是今天"）；
- "同型"的判据是 **dict 或 没有这个键**：kb 候选（`_kb_push_candidates`）不写
  `freshness`，合流侧按 `None` 处理；**绝不许出现 float**（C1 一条判据在真实链路 +
  提示词池子里双向量）。

### 1.5 `dup_penalty` = **全局重复度**（首轮 🟠 S3 的收口）

- 参数改名，语义写明：`conv_push_candidates(..., dup_counts=None)`、
  `conv_push_for_session(..., dup_counts_of=None)`——回调回答"同一 code 在 24h 内被
  **任意会话/公民**推送过几次"；
- **本会话去重仍由 `recent_of` 负责**（挡：24h 内推过就不产）。两条语义分开：
  挡 = "别再给我看同一条"，罚 = "它在池子里已经出现过很多次"。首轮把两者做成同一回调/
  同一会话键，导致被 `exclude` 的 code 恰好就是被数到的 code → 活下来的候选恒 0；
- 归一 `min(count,5)/5`（负号在权重 −0.30 上）；
- **与 `_silence_weight` 的分工**（写进 docstring）：`dup_penalty` = 重复度/新颖性；
  `_silence_weight` = 历史忽略率（按 code 跨会话的 ignored/total，下限 0.15）。两者是
  不同现象：重复但每次都被采纳 → dup 高、silence=1.0；推一次就没人理 → dup=0.2、silence 低。
  只有"又重复又被忽略"的那一小撮会同时进两个读数，方向不同且各有上下限，不构成
  "同一现象罚两次"。**若调用方只想量"别的会话推过没有"**（把本会话的去重完全留给
  `recent_of`）：回调里排除本会话即可（`exclude_session=session_key`）——这条口径有
  独立判据 `test_c1_dup_penalty_counts_other_sessions_only`（同一张 `push_log`：全局 3 次
  → 0.6；排除本会话 → 0.0）；
- **缺省（不给回调）也记账**：`dup_penalty=0.0` 并计一次 `skipped["dup_unknown"]`
  （值 = "几次取数没拿到"，不是条数）。为什么缺省也记：负向特征读不到时推送会变多，
  而"变多"在推送日志里看不出来（每条都长得一样）。

### 1.6 性能判据：主闸换成**结构**，墙钟只留兜底（首轮 🟠 S5 / QA F1 的收口）

54 号 §2.2 C5 = "`conv_push_candidates` 单次 < 50ms"。R2 的处置：

| 判据 | 形态 | 为什么 |
|---|---|---|
| ① **结构闸（主闸，常开）** | 用 `conn.set_trace_callback` 收集候选层的**用户级 SQL**，断言 ⊆ `search(count=False)` 的 SQL（允许参数不同）；且不得出现 `COUNT(*)` / 额外 `MATCH` / 不带 `MATCH` 的整表扫描 | "候选层不许自己再查一遍库"是**结构**问题：确定性、零噪声。首轮"增量 <20ms"的阈值与噪声同量级（QA 实测 −2.9…−125ms，本轮自测 −4.1…+8.8ms），M12 抓不抓得到是抽签 |
| ② 合成 1k 墙钟兜底 | ≤ **200ms** | 每次都会跑的回归闸，守"秒级退化" |
| ③ 真库绝对读数 | **只记录、不判定** | 见 §5.2 / §6.1：50ms 已**未达标**，根因在既有 FTS，属 T04 |

**结构闸放在合成 1000 段夹具里**（`CandidateSqlShapeTest`），所以它**不需要真库、不设
环境变量也每次都跑**——首轮"CI 里等于没有性能闸"（QA Y7）因此消失。真库那条
（`PerfTest`）保留同样的结构断言 + 读数留档，未设 `FISH_CORPUS_PERF_DB` 时
`skipTest` 并写明"判不到"（**SKIP 不等于 PASS**）。

### 1.7 `search()` 的 `count=True`（不变）

候选路径传 `count=False`：候选只要 top-K，"全库命中总数"是给人看的读数。5 万段库上
那一次 `COUNT` 就是多扫一遍 FTS。`count=False` 时 `meta.total_matched` 回 `None`
（**不是 0**：0 会被读成"一条都没命中"）。默认仍 `True`，既有调用面/字段集未变。

---

## 2 · 改动清单（`git diff --numstat` 实测）

| 文件 | 性质 | 行数 | 内容 |
|---|---|---|---|
| `p0/fish_corpus_rank.py` | 改 | **+127 / −5** | `SCORE_STAGE_*`、`KIND_CLASS_*`、`FRESHNESS_BAND_*`；`freshness_band` / `freshness_reading`（同型 dict）/ `semantic_kind`（三类归一）；`combine_features` 补值域与 R3 说明；模块头改口径（阶段一共用**常量**而非同一公式，首轮 Y1） |
| `p0/fish_corpus.py` | 改 | **+82 / −35** | `_dup_count_map` 语义改全局 + 缺省也记 `dup_unknown`；`conv_push_candidates(..., dup_counts=)` 产出 `freshness` dict + `score_stage`/`rerank`；`conv_push_for_session(..., dup_counts_of=)`；常量别名指向 rank 模块 |
| `p0/test_corpus_rank_align.py` | 改（重写） | 1047 行（+518/−114） | C0–C7 共 31 条；新增结构闸、全局重复度、freshness 同型、三类归一、阶段解耦 |
| `p0/mutation_corpus_rank_align.py` | 改 | 268 行（+122/−50） | M10–M15 六个变异体；判定改为"认具体用例 + FAIL/ERROR 分清"；多靶点支持；去掉"判不到也算 exit 0" |
| `docs/T03a-语料候选与Ranker对齐-交付.md` | 改 | 本文 | §1.6/§6 按新口径重写 + §10 逐条闭合表 |

**没碰**：`fish.py`、`mcp_server.py`、`fish_kb.py`、`daily_report.py`、
`test_corpus_push.py`（只读复用）、`test_b3_round2.py`、`data/` `_audit/` `_golden/`。

---

## 3 · 判据读数（C0–C7）

`cd p0 && python -m unittest test_corpus_rank_align -v` → **Ran 31 / OK (skipped=1)**

| 判据 | 条数 | 内容 | 读数 |
|---|---|---|---|
| **C0** | 9 | 六特征纯函数（名字/权重逐字、180 天半衰期、`dup_penalty` 封顶 5、`access_ratio`/`link_strength` 归一、`project_match` 三档）+ **`freshness_reading` 同型形态**（180 天 → 0.367879 **硬编码期望**）+ `freshness_band` 三档边界 + `combine_features` 分项和/值域/未登记名要响 | 绿 9/9 |
| **C1** | 9 | 候选字段齐全（`kind_class`/`score`/`score_stage`/`rerank`/`score_breakdown` 四项/`freshness` dict/`reason`）；`score` 不被 scorer 改写（只新增 5 键 + 既有 `relevance`）；**全局重复度**（3→0.6、0→0、5/6/50 封顶）；**只取其他会话**口径；`dup_unknown` 缺省/失败/形态错；薄壳口透传；**freshness 三类同型** | 绿 9/9 |
| **C2** | 2 | `get_ranker("default")` 两遍**逐字**一致；自定义 ranker 同样确定且与默认不同 | 绿 2/2 |
| **C3** | 2 | `register_ranker` 注册即生效；未知 ranker / 空名 / 非可调用工厂 → `RankerError` | 绿 2/2 |
| **C4** | 2 | 门牌 = `store.push_pointer` 唯一生成 + 正则 + 可原样读回；**既有锁复用**（内嵌跑 + `skipped` 必须为空） | 绿 2/2 |
| **C5** | 1 | **结构闸（常开）**：候选层用户级 SQL ⊆ `search(count=False)`；无 `COUNT(`、无额外 `MATCH`、无裸整表扫描；候选非空（防空转） | 绿（1 条 SQL vs 1 条，多出 0 条） |
| **C5a** | 2 | 真库（50,767 段副本）：结构闸 + **绝对读数只记录** | 见 §5 |
| **C5b** | 1 | 合成 1,000 段墙钟 ≤ 200ms | 绿（16.3ms） |
| **C6** | 3 | 三类映射各一例（corpus/kb/mem 五种 kind）；`kind_class` 优先 + 候选双字段不打架；未知 kind → mem 且不抛（`notes` 记账） | 绿 3/3 |
| **C7** | 2 | `score` == 首阶段公式（独立 oracle）；明细手算 oracle == `combine_features`；阶段常量与 `rerank=None` 合同 | 绿 2/2 |

### 3.1 三态（绿 / 跳过 / 判不到）分开写

- **绿**：31 条中的 30 条（未设真库时）；设了 `FISH_CORPUS_PERF_DB` 时 **31+1**（PerfTest 2 条也跑）。
- **跳过（skipTest，判不到）**：`PerfTest`（1 条 class 级）——因为需要人工指定真库副本；
  三种"判不到"原因分别写清：未设 `FISH_CORPUS_PERF_DB` / 库不存在 / 段数 < 4 万 / 挑不出
  可命中的 (project, query)。
- **不存在"性能闸被跳过"的洞**：结构闸（C5）**不需要真库**，全量跑里始终在跑。

---

## 4 · 变异自证（M10–M15）

`cd p0 && python mutation_corpus_rank_align.py` → **exit 0，6/6 全抓**，每轮 sha256 逐字节一致。

| 变异体 | 改坏什么 | 必须红的判据 | 读数 |
|---|---|---|---|
| **M10** | `score_breakdown` 删掉 `project_match` | C1 结构齐全 | **红 ✓** |
| **M11** | `DefaultRanker.rank` 加 `uniform(0, 0.05)` | C2 两遍一致 | **红 ✓** |
| **M12** | 候选层 `count=False` → `count=True` | **C5 结构闸** | **红 ✓**（多出一条 `SELECT COUNT(*) … MATCH`） |
| **M13** | 候选层把重复度读数写死 0 | C1 全局重复度 | **红 ✓** |
| **M14** | 候选 `freshness` 退回 float（= 首轮原状，两个出口一起回退） | C1 freshness 同型 | **红 ✓** |
| **M15** | `semantic_kind` 忽略 `kind_class` | C6 三类归一 | **红 ✓** |

三个"R2 才修掉"的自证问题（首轮 QA Y6）：

1. **M12 不再依赖真库**：结构闸在合成 1000 段夹具里常开 → "抓没抓到"是确定的，不是墙钟抽签；
2. **判定认具体用例**：必须出现 `FAIL:`/`ERROR:` 且行里就是那条指定用例（`rc != 0`
   不再够用；被跳过的用例 rc 也是 0），并打印 FAIL/ERROR 类别；
3. **没有"判不到也算 exit 0"**：本轮无环境依赖变异体，出现没跑即非零退出。

---

## 5 · 性能读数（真库副本 50,767 段 / 16 项目 / 1 个非空项目 `anyi-studio`）

测量方式：生产库 `sqlite3.backup` → 一次性只读快照 → 副本上**交错**测量（保证不偏向
后测的那一批），各取 min。

### 5.1 结构闸（主闸，确定性）

```
[C5] 候选层用户级 SQL 1 条 / 裸检索 1 条；候选层多出 0 条
```

即：候选层执行的只有那条 `SELECT … FROM corpus_fts … MATCH ?`，与
`search(count=False)` **逐条相同**（内部 FTS 语句以 `-- ` 前缀标记，不计入）。
M12（打开 `count`）在这条上必红。

### 5.2 绝对读数（**只记录，不判定**）

`PerfTest` 单查询（真库挑最长段的中段四字）：

| 读数 | 值 |
|---|---|
| `conv_push_candidates`（min / 3 次交错） | **40.2ms**（45.4 / 44.5 / 40.2） |
| 同查询裸 `search()`（min） | 40.4ms（44.6 / 40.4 / 41.6） |
| 增量 | **−0.3ms**（噪声带内摆动，不作判据） |

**8 个真实四字查询的绝对谱**（同一副本，交错 3 轮取 min，`conv` 列）：

| query | 间顺序分 | 交与重复 | 正文被后 | 结论全部 | 是注释层 | 市工信局 | 同一份结 | 核实后的 |
|---|---|---|---|---|---|---|---|---|
| conv min (ms) | **59.5** | 44.5 | 26.1 | **306.7** | 47.9 | 7.3 | **63.2** | 40.6 |
| search min (ms) | 50.7 | 47.6 | 30.2 | 306.4 | 46.6 | 9.9 | 58.7 | 38.0 |
| delta (ms) | +8.8 | −3.1 | −4.1 | +0.2 | +1.3 | −2.6 | +4.5 | +2.6 |

→ **8 个查询里 3 个 ≥ 50ms（最大 306.7ms）；增量的符号完全由测量顺序/负载决定**。
这就是"墙钟不能当闸"的实证，也是"54 C5 绝对 50ms 未达"的自证（§6.1）。

### 5.3 合成 1,000 段（C5b 兜底）

| 读数 | 值 | 判据 |
|---|---|---|
| `conv_push_candidates` best / 3 轮 | **16.3ms**（20.2 / 18.0 / 16.3） | ≤ 200ms ✅ |

---

## 6 · 已知限制 / 未闭合（按"红项 / T03b / 已登记"分层）

### 6.1 【**未达标·红项**】54 号 C5「绝对 < 50ms」→ 登记 T04

- **现象（本轮自测）**：50,767 段副本上 8 个真实查询里 **3 个 ≥ 50ms**（59.5 / 306.7 /
  63.2ms）；裸 `search()` 自己就是 50.7 / 306.4 / 58.7ms。
- **根因**：`store.search_segments` 的 FTS5 `MATCH` 对 3 个 bigram 做 OR 并集，命中
  postings 全都要算 `bm25`；`project` 过滤在另一张表上，**无法下推进 FTS**。语料从
  设计时的 43.5k 段长到 50,767 段（+17%），50ms 预算因此失守。
- **本轮处置**：把主闸换成**结构判据**（§1.6），绝对读数只留档；**没有**把 50ms 改成
  一个更松的墙钟阈值冒充达标。
- **判据归属**：T04（索引/检索）红项——建议 `corpus_fts` 增加 `project` 列做同表过滤，
  或按项目分片建 FTS；`fish_corpus_store.py` 的 DDL 不在 T03a 写作用域（会与 C 的索引
  批次撞车）。**T03a 不记"达标"**。

### 6.2 `dup_penalty` 的真实链路取值仍在 T03b（本轮只修语义与判据）

`fish.py` 不在本批写作用域，生产调用（`fish.py:8059-8065`）只传了 `recent_of` 与
`scorer`，所以真实链路里 `score_breakdown["dup_penalty"]` 仍是 `0.0`，同时会多一条
`skipped["dup_unknown"]`（缺省记账，这是有意的）。**T03b 的接线点**：

```python
recent_of=lambda hours, when: _recent_pushed_codes(conn, session_key, hours=hours, now=when),
dup_counts_of=lambda hours, when: _global_push_counts(conn, hours=hours, now=when,
                                                     exclude_session=session_key),
```

（`exclude_session` 是"只取全局其他会话"的口径；不加就是"任意会话/公民"。两种都合法，
差别与理由见 §1.5，判据已覆盖两者。）

### 6.3 `access_ratio` / `link_strength` 只有纯函数

两条刻度已在 `fish_corpus_rank.py` 定义并被 C0 钉住，但**没有任何取数**（T03b 补）。
因此 `score_breakdown` 现在只有四项；**R3（分项和 == score）属于 T03b**，且只有在
"重排后原子替换 score/breakdown"之后才成立（§1.3）。

### 6.4 `PerfTest` 需要人工指定真库副本

默认 `SKIP`（判不到）。结构性能力已由常开的 C5 覆盖，所以这条 SKIP 不再等于"没有闸"。

### 6.5 `freshness` 与 `_cand` 的分工仍无自动化判据

"传给 `_cand` 的 `freshness` 必须是 `None`"（避免双重折扣）目前靠代码阅读 + 既有
`test_corpus_push` 的分数断言间接守；T03b 接权重时顺手加一条（未闭合提醒，不是缺陷）。

### 6.6 T03b 必须显式拍的两条口径（不改代码也要写进它的判据）

1. `score > 0` 门槛（`fish.py:8758`）在 `combine_features` 值域 [−0.30, +0.95] 下怎么落；
2. 34 §3.5 R3 的判别力：若 `score := combine_features(...)`，R3 恒真 → 必须用**手算 oracle**。

---

## 7 · 回滚

```powershell
cd I:\app\DSH-Home\workspace\learn\fish-push
# 只回滚 R2（保留首轮）
git revert <R2-commit>
# 或者整体回退到基线
git reset --hard e8acaf4
```

判据面回滚后应回到基线读数：`Ran 1403 / OK`（生产侧无感知：本轮没有改任何运行时默认值，
`dup_counts`/`dup_counts_of` 都是**仅新增的可选参数**；`freshness` 由 float 变 dict 只影响
候选结构，`fish.py` 不读它）。

---

## 8 · 交接给 T03b（接口清单 · R2 版）

| 交接物 | 形态 | 用法 |
|---|---|---|
| 类别 | `cand["kind_class"]`（corpus 侧已填）；**归一**用 `fish_corpus_rank.semantic_kind(cand)` | 合流池按三类分组，别自己写映射表（mem 侧五种 kind 由它归 mem） |
| 阶段 | `cand["score_stage"] == "first_phase"`、`cand["rerank"] is None` | 重排后**原子替换** `score`/`score_breakdown`，置 `"rerank"`，并写 `rerank=<名字>` |
| 明细 | `cand["score_breakdown"]` = `{relevance, freshness, project_match, dup_penalty}`（`freshness` 是**数值**） | 补 `access_ratio`/`link_strength` 后按 §1.3 的口径决定 score |
| 观测值 | `cand["freshness"]` = **dict** `{factor, band, age_days}`（与 mem 同型） | 直接读 `["factor"]`；**不要再乘进 rel**（`_cand` 收到的仍是 `None`） |
| 重复度 | `conv_push_for_session(..., dup_counts_of=…)`（**全局**，可排除本会话） | 主库侧提供 `{code: 24h 次数}`；缺省会记 `dup_unknown` |
| 权重 | `fish_corpus_rank.FEATURE_WEIGHTS`（唯一身份源） | 写进 config 时从这里取默认值 |
| 阶段名 | `SCORE_STAGE_FIRST` / `SCORE_STAGE_RERANK` / `SCORE_STAGES` | 不许写字面量 |
| 性能 | 结构闸常开；候选路径 `count=False` | 合流后**不要**再对 corpus 侧加全库统计 |
| 阈值 | `PUSH_DUP_WINDOW_HOURS = 24`（与去重**有意共用**同一个数） | 权重可调，窗口别另写 |

---

## 9 · 提交与读数

```
fix(T03a): global dup count + dict freshness + structural C5 + semantic_kind (R2)
```

读数一行：
`C0-C7 31 tests OK (skipped=1); C5 structural 1 SQL vs 1 SQL, 0 extra; real copy conv=40.2ms search=40.4ms delta=-0.3ms (8-query spectrum: 3/8 >= 50ms, max 306.7ms -> T04 red); C5b 16.3-37.6ms; M10-M15 6/6 CAUGHT; full suite Ran 1434 OK (skipped=1) / 478.5s`

---

## 10 · 首轮问题逐条闭合（评审 🟠 / QA F1–F4）

### 10.1 评审 🟠（`docs/评审报告-T03a.md` §2）

| ID | 首轮问题（一句话） | R2 处置 | 证据 |
|---|---|---|---|
| **S1** | `kind_class` 只有语料侧有，"按 `kind_class or kind` 分三类"不成立（会得 7 类），下游各猜映射 | 加 `semantic_kind` 单点归一（`kind_class` 优先；`conv_related`→corpus；`kb`/`kb_related`→kb；其余→mem，未知不抛但记账）+ 常量化 `KIND_CLASS_*` + 候选双字段不打架判据 | C6 3 条；§1.1 |
| **S2** | `score` 与 `score_breakdown` 是两个阶段的数，"不改分"与"`score=combine_features`"互斥，留给 T03b 临场拍 | 加 `score_stage="first_phase"` + `rerank=None`；`score` 明确等于 `_cand` 的 `(benefit−cost)×weight`；阶段名常量化并写明 T03b 的"原子替换"合同；`combine_features` 补值域/R3 说明 | C7 2 条；§1.3；全文无 "sum(breakdown)==score" 断言 |
| **S3** | `dup_penalty` 结构性恒 0（去重与罚同会话同窗口），且与 `_silence_weight` 罚同一现象两次 | 改成**全局重复度**（任意会话/公民），`recent_of` 只管挡；参数改名 `dup_counts`/`dup_counts_of`；分工写进 docstring；给"只取其他会话"口径并测；缺省也记 `dup_unknown` | C1 4 条（3→0.6 / 0→0 / 封顶 / 其他会话口径）；§1.5 |
| **S4** | `freshness` 同池两种类型（corpus float / mem dict）→ 合流必炸或静默错算 | corpus 改 dict `{factor, band, age_days}`（与 `_cand` 逐键同）；明细保留数值 factor；两类读数同源 | C0 2 条 + C1 1 条（含真实链路与池子）；§1.4 |
| **S5** | C5 下修不等价且替代判据统计上不成立；50ms 被"换掉"而非登记红项 | 主闸换**结构判据**（SQL ⊆ 裸检索，常开），M12 改为结构红；50ms **登记为 T04 红项**（本轮自测 3/8 ≥ 50ms），真库读数只记录；去掉墙钟增量判定 | C5 结构闸 + M12 CAUGHT；§5.1/§5.2/§6.1 |

### 10.2 评审 🟡/⚪ 中随本轮清掉的（顺手，非要求项）

| ID | 处置 |
|---|---|
| **Y3** | `PUSH_DUP_WINDOW_HOURS` 与 `DUP_WINDOW_HOURS` 同对象的"可独立调"说法删掉，改成"有意共用同一段时间" |
| **Y4** | `combine_features` docstring 写明值域 [−0.30, +0.95]、不夹紧的原因、R3 会退化成恒真 → 要手算 oracle |
| **Y5** | C0 的 `freshness_factor`/`freshness_reading` 断言改成**硬编码期望值**（0.367879…），C7 的明细和用硬编码权重手算（不复调被测函数） |
| **Y6** | 变异脚本：认具体用例 + 区分 FAIL/ERROR、去掉"判不到也算 exit 0"、打印 skip 计数 |
| **Y7** | C4 增 `result.skipped` 断言；C5a 补"候选非空"防空转；结构闸常开（不再只在设 env 时才有闸） |
| **O1** | 行数按 `git diff --numstat` 实测（§2） |
| **O3** | 34 号示例 `score: 0.78` 与其权重复算（0.62）不符 → 以公式为准，本报告登记该文档不一致（文档 owner 修） |

### 10.3 QA（`docs/QA-T03a.md` §8）

| ID | 首轮问题 | R2 处置 | 证据 |
|---|---|---|---|
| **F1** | C5a 的 delta 判据噪声带 ≥ 阈值（顺序测法 18.8ms / 交错全负） | **删掉墙钟 delta 作为主闸**，改结构判据；墙钟只留 C5b 200ms 兜底 | §1.6/§5.1；本轮 8 查询 delta −4.1…+8.8ms |
| **F2** | 绝对延迟 50→300ms 无替代判据、能力有损 | 50ms **登记为 T04 红项**（不写"达标"），并给本轮自测谱（3/8 ≥ 50ms，max 306.7ms） | §6.1 |
| **F3** | `score` 与 `score_breakdown` 不可对账 | 加 `score_stage`/`rerank`，明确两者分属两个阶段、禁止互相验算，R3 归 T03b | §1.3；C7 |
| **F4** | `freshness` 跨类两种类型 | corpus 改 dict，同型判据覆盖 mem/kb/corpus 三态 | §1.4；C1 |
| F5（⚪） | C1 的 `project_match == 1.0` 恒真（检索已按项目下推） | 未改（QA 自己说刻度已由 C0 覆盖）；本轮在 C0 用硬编码三档覆盖 | C0 |
| F6（⚪） | C4 嵌套 `TestResult` 不查 `skipped` → 假绿 | **已修**：`assertEqual([skipped], [])` | C4 |
| F8（⚪） | C5a 不断言命中非空（0 命中照样绿） | **已修**：结构闸与真库读数都先断言候选非空 | C5 / C5a |
| F9（⚪） | "热替换"实为"新注册立即生效" | 判据标题改成"注册即生效"，语义写清 | C3 |
| F10（⚪） | `_cand(freshness=None)` 的双折扣纪律无自动判据 | 仍未闭合（§6.5，归 T03b） | — |

### 10.4 本轮**仍未闭合**（明示，不粉饰）

1. **54 C5 绝对 <50ms 未达** → T04 红项（§6.1，含本轮自测读数）；
2. `dup_penalty` 的真实取值与采纳率效果 → T03b（§6.2）；
3. `access_ratio`/`link_strength` 取数、R3、`score > 0` 门槛口径 → T03b（§6.3/§6.6）；
4. `_cand(freshness=None)` 的自动化判据 → T03b（§6.5）；
5. 34 号设计示例 `score: 0.78` 的文档不一致 → 文档 owner（§10.2 O3）；
6. 跨平台浮点（Windows/Py3.11 单平台）→ 影响 ≤1e-6，非阻塞（沿用 QA §7 的口径）。
