# QA-T03b-R2 · 推送合流 **收口轮终验**（独立复跑 · 2026-10-28）

> 被验对象：`learn/fish-push`，分支 `dev/fish-push-261027`
> **HEAD `0166b0d`（代码 `c276705`，父 `a06d988`），基线 `6a7f4e6`**
> 验证时刻：**2026-10-28T00:50–01:16 +08:00**（Windows / Python 3.11.9，`C:\Users\15596\AppData\Local\Programs\Python\Python311`）
> 依据：`docs/主理人裁决-T03b.md`（D1–D12）、`docs/评审报告-T03b.md`（S1–S4 / W1–W8 / G1–G4）、
> `docs/QA-T03b.md`（首轮 D/F）、`docs/T03b-推送合流-交付.md`（R2）、`52/54/34` 三份口径文件。
> 方式：**只读代码 + 自己写探针复跑**；受跟踪文件**零改动**（唯一写入 = 本文件）。
> ⚠️ 验证起点发现共享工作树被**另一会话的变异运行**占据（`p0/fish_push_rerank.py` 连续被改写、
> mtime 每 20–40s 跳一次、`git diff` 形状在 `1+/3-` 与 `1+/1-` 之间反复）。为保证读数不受污染，
> **本轮全部判据在 `git archive HEAD` 的隔离副本上跑**（`learn/_qa-r2` / `_qa-r2mut` / `_qa-r2m15` /
> `_qa-r2base`），四个副本的 `git hash-object` 与 HEAD blob 逐一相等（`fish_push_rerank.py` =
> `b06efe52c8d5`）。该会话随后自行还原，工作树已干净（只剩未跟踪文档）。

---

## 0 · 结论（先行）

| 项 | 读数 |
|---|---|
| **全量回归**（干净树、无并发） | `discover -s . -p "test_*.py"` → **Ran 1532 / OK (skipped=2) / 273.675s**（wall 277.5s；exit 0） |
| **T03b 判据** | `python -m unittest test_push_rerank` → **Ran 34 / OK / 26.9s**，**0 skip**（R6b/R6d/R8b/R14 全部判得到） |
| **变异自证（开发方）** | `mutation_push_rerank.py` → **9/9 CAUGHT / exit 0 / 306s**；前后 sha256 相同（`7f055869b677…`），还原逐字节 ✓ |
| **变异自证（我自己 6 个）** | **6/6 被抓**，对照用例仍绿，两文件 sha 逐字节还原 ✓ |
| **默认关等价** | 与 `6a7f4e6` 真 blob 对拍 **218 条断言全绿**（6 池 × 3 配置态 × 4 参数组 ×「等价/同一性/零改动」） |
| **对抗面** | **45 条全绿**（top_k 14 例 / breaker 7×2 / 别名环 / links 6 例 / kb 无 freshness / 生成器 / 脏候选 / 缺列库） |
| **生产树** | `.py` 151 个 sha **本轮期间逐字节未变**；**无** 4 个 T03b 面文件；最新 mtime `2026-10-27T19:49:01`（早于 T03b 开工） |
| **判定** | **PASS**（FAIL 0；非阻断登记 5 条，见 §10） |
| **Routing** | **NoOne**（未发现源码 bug；不把文档/口径项打回工程师） |

**Total 1532 ｜ Passed 1530 ｜ Failed 0 ｜ Skipped 2 ｜ 独立探针 269 条断言 269 PASS**
（覆盖率估算≈口径内可测面：T03b 新面 34 条判据 + 269 条独立断言 + 9 个官方变异体 + 6 个自建变异体；未做行覆盖统计，如实标注。）

---

## 1 · 首轮问题闭合（逐条，含"行为是否真改"判定）

### 1.1 评审 4 🟠

| # | 首轮问题 | 闭合证据（我复跑） | 行为是否改动 |
|---|---|---|---|
| **S1** prompt 路径项目 | `_with_ctx_project` 在 `build_prompt_candidates` 的 **5 个 return 点**（L476/652/1041/1044/1091）贴 `_ctx_project`，`_infer_project` 优先它；`select` 里 `now=now or now_utc()` | **真改**（不是文档）。`R12a` 开→全池 `_ctx_project=TESTA`、关→零新增字段；`R12b` 3 条 TESTB + 1 条 TESTA → 跨项目 **0.0**、当前项目 **1.0**；`R12c` 候选塞 2 天前 `now` + 30h 前推送 → `dup_penalty=0.0`（若采信旧 now 会 0.2）。三条我都在隔离树复跑绿 |
| **S2** kb/corpus `project_match` 假特征 | `_project_match_of` 对 `kb`/`corpus` 直接 `return PROJECT_NEUTRAL(0.5)`，docstring 写明"+0.15 白拿 + freshness 0.15 = ≈+0.25 结构性先手" | **真改**。`R9` 读数 `1.0/0.0/0.5/0.5/0.5/0.5/0.0` 我复跑一致；另用自建变异 **X5**（别名组传 None）抓 `R9` ✓ |
| **S3** `link_strength` 0 覆盖 + 5 个静默变异 | `_link_strengths` 真实现：`resolve_project_names` 别名展开、**双向**取边、候选自己是锚点不计数、`sqlite3.Error → {}`；判据 `R8/R8b/R8c` + 变异 `M10–M14` | **真改**。`R8` 原始 `{strong:0.8, weak:0.4}`、归一 `1.0/0.5/0/0/0`；`R8b` 真库 49 条候选 **0 条有边**（合法空集）；`R8c` 别名读数一致。另用自建 **X4**（只取单向）抓 `R8` ✓ |
| **S4** `fish_push.py` 贴死 J4 | 拆出 `fish_push_rerank.py`；`fish_push.py` **1,246**（窗口 900–1500，**余 254 行**）、新模块 **396**（自设 ≤400，**余 4 行**） | **真改**。`test_split_contract` 13 条 OK（J8 skip，见 §9）。⚠️ 脆性被"搬"而非"消"：新模块 396/400，见 §10-① |

### 1.2 QA 首轮 D/F 项

| # | 项 | 闭合证据 | 判定 |
|---|---|---|---|
| **D-1** | `top_k<1` 文档 vs 实现 | 代码 `<1 → 1`、`>500 → 500`、解析失败 → 50；文档已按实现改写（交付 L113/L347）；判据 `R1b` | ✅ 闭合（**文档型**：行为本来就是 1，未改行为，但**新增判据钉住**，不算"只改字"） |
| **D-2** | R6 归因错误（`executescript+migrate` 0.6–0.9s） | 我独立拆段：解释器 **0.085s**、`import fish` **0.283s**、hook 依赖 **0.184s**、`connect+executescript(SCHEMA)+migrate` **0.009s** → 交付 L129/L348 的订正数字**成立** | ✅ 闭合（**纯文档订正**，但被实测背书；行为无需改） |
| **F-1** | 生成器入参被吃光 | `select_push_candidates` 首行 `cands = list(cands)`（L1149）+ `_tag_kind_class` 返回值接回；`R13` 2/2 条；自建 **X6**（删掉 `list(cands)`）抓 `R13` ✓ | ✅ 闭合（**真改**） |
| **F-2** | R3 只有同源恒等 + 单点 oracle | 新增 `R3b` 组合 oracle（手算 **0.625 / 0.22**，六项全非缺省）；`M15` 与我的 **X1**（freshness 恒 1.0、breakdown+score 一致篡改）都只被 `R3b` 抓、`R3` 和式仍绿 | ✅ 闭合（**判据型**，判别力实测成立） |
| **F-3** | `dup_penalty` 生产几乎恒 0 | 行为未变（仍是**全局 24h**、`min(count,5)/5`）；交付 §11-3 明确写"R4 绿 ≠ 生产防重复到位"，裁决 D5 记分工 | ⚠️ 闭合但是**纯文档登记**：没有把"特征贡献有限"冒充成"已解决"，属诚实登记，可接受 |
| **D-3** | 真语料被判成"判不到" | `R14` 真语料只读副本 → `conv:workbuddy:c48fd810#line=87036`、`kind_class=corpus`（我复跑两次都产出同一 code） | ✅ 闭合（**真改**：新增判据 + 真库只读路径） |

> **"只改文档不改行为"的三条**（按要求点名）：**D-1**（文档随实现改写 + 补判据）、
> **D-2**（纯归因订正，已被我的 P1 拆段实测背书）、**F-3**（纯口径登记，残余限制如实写在 §11-3）。
> 三条都**不是**"用文档掩盖行为缺陷"：D-1 补了 `R1b`，D-2 是可复现的读数更正，F-3 主动把
> "判据绿 ≠ 生产有效"写进已知限制。**未发现**"只改文档、行为其实没修"的假闭合。

---

## 2 · 独立复跑（不复用开发方脚本作判据）

| 项 | 我的跑法 | 读数 |
|---|---|---|
| 全量 | `discover -s . -p "test_*.py"`（隔离树，无其它进程） | **1532 / OK (skipped=2) / 273.675s** |
| T03b 34 条 | `-v`，取打印行 | `[R6a] 0.004s`、`[R6a2] N=10 min 1.217/中位 1.550/max 2.152，<2s 8/10`、`[R6b] 0.485/0.698s`、`[R6d] 中位 **+0.014s**（样本 `[0.02,−0.062,0.014]`）`、`[R8b] 49 条→0 条有边`、`[R14] conv:workbuddy:c48fd810#line=87036` |
| 契约 | `test_split_contract` | **Ran 13 / OK (skipped=1)**（J8：拆分前备份不存在，与基线同源） |
| 变异（开发方） | `mutation_push_rerank.py`（隔离树） | **9/9 CAUGHT，exit 0，306s**；每个变异体后 `sha256 一致 ✓`；前后文件哈希 `7f055869b677…` 不变 |
| 变异（我自建 6 个） | `qa/qa_mut_indep.py`（我挑靶点） | **6/6**：X1 freshness 恒 1.0→`R3b`；X2 top_k 上限→`R1b`；X3 `_infer_project`→None→`R10`；X4 links 单向→`R8`；X5 别名组 None→`R9`；X6 去掉 `list(cands)`→`R13`。对照用例（`R3`/`R8`/`R9` 互斥项）全绿，还原逐字节 ✓ |

---

## 3 · 默认关等价（C1–C4，218 条）

方法：`git show 6a7f4e6:p0/fish_push.py` 取真 blob → 落盘成独立模块 `fish_push_baseline`
（sha256[:12] **`7a87fba903cb`**，与交付 §7 自述一致），同一 conn、同一候选内容分别喂新旧实现。

* **池 6 组**：mixed（mem/kb/conv/rule/serendipity）/ negative_zero / dup_codes / empty / ties / extra_fields（含 `freshness`、`notes`、残留 `_ctx_project`）；
* **配置 3 态**：未设置 / `"false"` / `"0"`；**参数 4 组**：`limit=2/4`、`prefer_kind=prompt_related/kb`、`limit=99`；
* 每组核 **C1 元素+顺序+逐字段 JSON（sort_keys）**、**C2 返回的是入参 dict 对象本身**、**C3 入参零改动**。

读数：**216/216 一致**；另 `C4`：显式 `rerank=True` 与关闭态**结果不同**（证明 C1 不是"两边都不干活"的假等价），
`C4b`：开启态才出现 `kind_class` / `score_stage` / `score_breakdown` / `first_phase_score` / `rerank`。

---

## 4 · R6 终验（要求 4）

| 判据 | 要求 | 我的读数 | 结论 |
|---|---|---|---|
| 决策路径 | < 2s | 合成夹具 **0.004s**；真 vault 决策路径 off/on = **0.391 / 0.408s**（N=5 中位，真库副本） | **达标** |
| 配对增量 | 中位 ≤ 0.2s | `R6d` 三份独立读数：**+0.014 / +0.032 / +0.020s**（样本均跨零，如 `[−0.062,+0.085]`） | **达标**（在噪声内） |
| hook e2e 分布 | N≥10，只记分布 | 我的三组：`R6a2` ①N=10 min 1.217 / 中位 1.550 / max 2.152，**8/10<2s**；②N=10 min 1.221 / 中位 1.460 / max 2.056，**9/10<2s**；③**真库副本** N=8 min 1.365 / 中位 1.427 / max 1.487，**8/8<2s**；④合成根 N=12 min 0.924 / 中位 0.958 / max 1.050，**12/12<2s** | 记录成立：**绝对值随夹具/负载漂移**（0.92–2.15s） |
| 是否 pre-existing | 不得把既有问题记成 T03b 达标 | 同条件 A/B：**HEAD vs `6a7f4e6` 基线 `fish_push.py`** —— 真库副本 N=8：HEAD 中位 **1.427** / 基线 **1.416**；合成根 N=8：HEAD **0.990** / 基线 **0.963** → **T03b 增量 ≈ +10~27ms（噪声）**。交付 §11-9 / §5 / D9 原文写"绝对值 37/73 <2s，pre-existing，真因是两次解释器启动 + `import fish` + hook 依赖，断路器/重排不背锅" | **如实登记 ✓ 没有把 pre-existing 记成 T03b 达标** |

归因实测（D-2 订正后数字的可复现性，各 5 次取中位）：解释器 **0.085s**、`import fish` **0.283s**、
hook 依赖 **0.184s**、`connect+schema+migrate` **0.009s**；`rerank_candidates` 本身 **0.375ms**。
→ 二次解释器 + `import fish` + hook 依赖 ≈ **0.55s 固定成本**，与"2s 线不稳"的因果链**成立**。

**R6 终验结论：条件式达标。**（决策路径硬线 + 配对增量达标；e2e 绝对值只记分布，且**已如实**交 A/T05 认领进程模型。）

---

## 5 · 新模块与判据面（要求 5）

| 项 | 读数 |
|---|---|
| `fish_push_rerank.py` 是否 import `fish` / `fish_push` | **没有**：仅 `datetime/hashlib/json/sqlite3/time/pathlib` + `fish_corpus_rank` + `fish_common`；`R15` 干净子进程断言复跑绿 |
| `fish_push.rerank_candidates` 可取 | ✓ `callable`，且 `is` `fish_push_rerank.rerank_candidates` |
| `__all__` 是否变 | **未变**：与基线模块**逐项逐序相同**（33 项）；`rerank_candidates` 不在其中；`fish` 侧无该名字 |
| 行数 | `fish_push.py` **1,246**（≤1500，余 254）；新模块 **396**（≤400，余 4） |
| J4/J6/J7 | `test_split_contract` **13 OK**（J8 skip）；J4 窗口 900–1500 未改；J7 双向（`__all__` 不含、`fish` 不含、`fish_push` 可取）成立 |

---

## 6 · 对抗面（要求 6，45 条全绿）

| 面 | 覆盖 | 读数 |
|---|---|---|
| `top_k` | `0/-5/-10⁹/1/8/10⁹/1e9/True/None/"abc"/"3"/"3.9"/""/999.9` + 池 600 | `<1→1`、`>500→500`、`None|"abc"|"3.9"|""`→50、`True→1`、`"3"→3` |
| 断路器 | `0 / -1 / -1e9 / 0.0001 / 1e9 / "abc" / 未设置` ×「切断/照推 + 诊断写/不写」 | `≤0` 关闭、极小正必切、坏值退 3.0（照推）；**决定与诊断行严格同步** |
| 别名环 | `QAX→QAY→QAX` 成环 + `QAZ→QAZ` 自环 + 悬空 | 不死循环（`_walk_alias` 8 跳上限），返回 `[{'QAX','QAY'}]`，重排照常产出 |
| links | 双向累加 `0.5+0.25=0.75`；抑制边 / `removed_at` / 零权 / **自环** / 只连**休眠锚点** | 只有双向累加有读数；其余 0（零权边留 `0.0` 键，特征值仍 0，不影响排序） |
| kb/corpus 无 freshness | `_freshness_of` 缺省 **1.0**、`project_match` **0.5**、总分独立遍历 Σw·f 一致（0.675） | 与 D6/D7 口径一致 |
| 生成器入参 | `rerank=True` / `False` | 2 条 / 2 条（F-1 不回归） |
| 脏候选 | `None / "x" / 42 / 缺 code / 缺 score` | 不抛，只留 dict |
| 库坏（缺列） | `_link_strengths` / `_access_counts` / `_dup_counts` / `_cfg_any` | 全部退 `{}` / 默认，不抛（`build_push_block` 不会整包丢） |
| `score_breakdown` **同时**篡改 | 我的 X1（freshness 恒 1.0，分项与总分一致篡改） | 和式仍绿、**`R3b` 红** ✓（M15 同结论） |

---

## 7 · 生产树（要求 7）

* `大肥鱼生命系统/p0`：**无** `fish_push.py` / `fish_push_rerank.py` / `test_push_rerank.py` / `mutation_push_rerank.py`；
* 151 个 `.py` 的 sha 在我开工前/收工后**逐字节相同**（本轮对生产树零写入）；最新 mtime `2026-10-27T19:49:01`；
* 与 `.wave0-manifest.json`（327 文件）比对：**318 同 / 0 缺 / 9 不同**，9 处与 QA 首轮同一批
  （`51/52/53` 三份文档、4 个 `fish-home` 看板、`p0/test_b3_round2.py`、`SESSION_STATE.md`）——
  **无一属 T03b 面**（首轮已定性为其他任务写入，本轮未新增差异）；
* 本批 diff 面：`git diff --name-only 6a7f4e6..HEAD` = **5 个文件**，**无** `fish.py` / `fish_common.py` / 任何 hook / 任何既有测试。

---

## 8 · 三态

| 态 | 内容 |
|---|---|
| **绿** | 全量 **1532 OK**；T03b **34/34**（0 skip）；契约 **13 OK**；变异 **9/9 + 我 6/6**；默认关等价 **218/218**；对抗 **45/45**；R6 决策路径与配对增量；`__all__`/J2/J4/J6/J7；生产树零写入 |
| **SKIP（2，与基线同源）** | ① `test_split_contract` 的 **J8**（拆分前同名函数 AST 对拍：拆分前备份不在）；② `test_corpus_rank_align.PerfTest`（真语料 5 万段墙钟：需 `FISH_CORPUS_PERF_DB`，未设 → 判不到）。**不是** T03b 引入 |
| **判不到（如实标注）** | ① **"含解释器启动的 2s"能否稳定达标**：本机静默时 8/8 <2s（1.37–1.49s），首轮并发负载下 37/73 → 只能判"随负载漂移"，不能判恒达标/恒不达标；② **真库三类同池**：真数据里 kb 候选常被 24h 排除，三类同池仍只在合成夹具（`R7`）；③ **真语料上的断路器慢路径**未在真库复现（断路器是后置判定，省不了墙钟） |

---

## 9 · 最易假绿 3 条

1. **`R6d` 的 N=3 配对增量**：三份读数的样本都**跨零**（`[−0.062,+0.085]`、`[−0.02,+0.032,+0.085]`），
   中位 ≤0.2s 只证明"这次没超"，不证明"重排不慢"。机器负载一变就可能翻面——判据本身对（钉的是 T03b
   能负责的那一段），但**统计力弱**，别把它读成"性能达标"。
2. **`R4`/`R3b` 里的 `dup_penalty`**：判据用合成 `push_log` 造 5 次推送 → 绿；生产 prompt 路径上同会话/
   同 kb 候选**已被 24h 排除**，该特征几乎恒 0（交付 §11-3 已登记）。"R4 绿"≠"生产防重复到位"。
3. **`R8b` 的真库 links**：49 条候选 → **0 条有边**，判据只能证"不抛、无非正权重"。真数据上
   `link_strength` 是**常数 0**，所以"六特征"在生产实际只 4–5 条能动；合成夹具里的 `1.0/0.5` 是构造出来的。
   （同理 `R7` 三类同池是合成夹具，别读成"生产三类都活着"。）

---

## 10 · 非阻断登记（5 条，不打回工程师）

1. **J4 脆性被搬到新模块**：`test_R15b` 自设 `≤400` 行，现 **396**，只余 4 行——加 4 行注释就红。
   建议 A 合并时把该自设阈值与 J4 一起复议（或允许随注释漂移），否则 S4 只是换了个文件脆。
2. **`_infer_project` 的残余口径张力**：`project` 解析不出时（cwd 未登记）仍用**记忆众数**当"当前项目"，
   跨项目池里"多数派项目"会拿 1.0，而裁决 D6 说"当前项目未知 → 0.5"。真正 unknown 只在"众数为空"时成立。
   默认关无影响；A 若在 `fish.py:9405` 补 `project=`（§9-2 建议项）可彻底消除。
3. **零权 links 边会留 `{code: 0.0}` 键**：特征值仍 0、不影响排序；只是 `_link_strengths` 的返回集合比
   "只含有边的候选"略宽（`R8` 夹具里没有零权边，故未被钉住）。
4. **`int("3.9")` 抛错 → 退 50**：与"数字串照常按 N"的口径不齐（`"7"` 可以、`"3.9"` 不行）。
   文档写"解析失败退 50"可覆盖，但写配置的人可能意外。
5. **仓库混行尾**：`fish_push.py` 为 **CRLF**、新模块与判据为 **LF**。变异脚本用 `mutation_source.to_lf`
   处理、逐字节还原不受影响；但任何"按行 count 靶点"的新脚本都要注意（我自己就先踩了一次）。

---

## 11 · 复现清单（全部在 `learn/_qa-r2*`，不写受跟踪文件）

```powershell
# 隔离副本（sha 已核 = HEAD blob）
git archive --format=zip -o _qa-r2.zip HEAD   # → learn/_qa-r2、_qa-r2mut、_qa-r2m15、_qa-r2base
# 基线树：把 _qa-r2base/p0/fish_push.py 换成 6a7f4e6 的 blob（sha256[:12]=7a87fba903cb）

cd learn/_qa-r2m15/p0
python -m unittest discover -s . -p "test_*.py"      # 1532 / OK (skipped=2) / 273.7s
python -m unittest test_push_rerank -v               # 34 / OK（打印 R6a/R6a2/R6b/R6d/R8b/R14）
python -m unittest test_split_contract -v            # 13 / OK (skipped=1)

cd learn/_qa-r2mut/p0
python mutation_push_rerank.py                       # 9/9 CAUGHT / exit 0

cd learn/_qa-r2/p0
python ..\qa\qa_equiv.py                             # 218 条（对 6a7f4e6 真 blob）
python ..\qa\qa_adv.py                               # 45 条对抗
python ..\qa\qa_perf.py <new_root> <base_root>        # R6 归因 + 决策路径 + hook 分布
python ..\qa\qa_hook_ab.py   <new_root> <base_root>   # 合成根 hook A/B（HEAD vs 6a7f4e6）
python ..\qa\qa_hook_real.py <new_root> <base_root> 8 # 真库副本 hook A/B
python ..\qa\qa_mut_indep.py <被测树根>                # 我自建 6 个变异体
```

证据文件：`qa/qa-perf-result.json`、`qa/qa-hook-ab.json`、`qa/qa-hook-real.json`、
`log-rerank.txt`、`log-discover.txt`、`_qa-r2mut/log-mutation.txt`、`_qa-r2m15/log-discover-clean.txt`、
`prod-p0-sha-before.txt`。

---

## 12 · 结论

**PASS / Routing: NoOne。**
首轮 4 🟠（S1–S4）与 QA D-1/D-2/D-3、F-1/F-2/F-3 **逐条闭合**，其中 D-1/D-2/F-3 属文档型闭合
（已点名，且各有实测或判据背书，未发现"只改字不改行为"的假闭合）；默认关与 `6a7f4e6` **逐字节等价**；
9 个官方变异体 + 我自建 6 个**全部被抓**且逐字节还原；全量 **1532 OK**；生产树**零写入**。
R6 终验为**条件式达标**：决策路径与配对增量达标，e2e 绝对值**如实**记为 pre-existing 并交 A/T05。
仍待 A 处理（与交付 §9 一致）：4 个 config 键登记、`fish.py:9405` 是否补 `project/now`、
`rerank_candidates` 是否 re-export、进程模型认领、J4/R15b 行数阈值复议（见 §10-①）。

**验证人**：QA 严过关（独立复跑，未使用开发方脚本作判据） · 2026-10-28T01:16 +08:00 · HEAD `0166b0d` / 代码 `c276705`
