# QA-T03b · 推送合流独立验证（六特征重排 + 三类语义统一 + 断路器）

> 被验对象：`learn/fish-push` 分支 `dev/fish-push-261027`
> **HEAD `a06d988`（代码 `f8d4679`，父 `6a7f4e6`）**
> 验证时刻：**2026-10-27T23:50 – 2026-10-28T00:15 +08:00**（本机 Windows / Python 3.11.9）
> 验证方式：只读代码 + 自己复跑（**未修改任何受跟踪文件**；全部夹具在 `_qa_tmp/T03b/` 与 `tempfile` 下）
> 关键输入：`52-并行开发协同总纲`（判据纪律）、`54-方案B` §三/§四、`34-方案设计` §3.3–3.5、
> `docs/T03b-推送合流-交付.md`（含 §2 偏差 2 的 R6 口径、§9 待 A 登记）

---

## 0 · 结论（先行）

| 项 | 结论 |
|---|---|
| **判定** | **PASS（附 2 条口径/文档订正 + 4 条 🟡 非阻断发现）** |
| 独立判据 | **27/27 PASS**（C1–C13；不含复用开发方判据） |
| 开发方判据复跑 | `python -m unittest test_push_rerank -v` → **Ran 15 / OK**（0 skip） |
| 全量回归 | `discover` → **Ran 1513 / OK (skipped=2) / 245.4s**（基线 1498 + 15） |
| 变异自证 | **M8 / M9 = 2/2 CAUGHT**，两轮 sha256 逐字节还原一致，其余判据无新红 |
| **R6 裁决** | **T03b 增量达标，但绝对值未稳（升级 A/T05）** |
| Routing | **NoOne**（源码无 bug；不给 Engineer 打回，只把口径与文档订正交给 A） |

---

## 1 · 逐条判据读数（我自己的脚本，非复用）

脚本：`_qa_tmp/T03b/qa_unit.py`（21 条）、`qa_extra.py`（6 条）。
默认关等价性用 **`6a7f4e6` 的真 `fish_push.py`**（`git show` 取 blob，`git hash-object` =
`861e8e6…`）加载成 `fish_push_old` 对拍，不是"对着同一份代码自证"。

| # | 判据 | 读数 | 结论 |
|---|---|---|---|
| **C1** | 默认关（未设置 / `false`）与 T03b 前**逐字节等价**（3 组候选：混合 kind/负数分/并列分/20 条池；各自带 `prefer_kind`） | 元素、顺序、每条的 JSON（sort_keys）全等；返回的是**入参对象本身**（id 序列一致）；候选零新增字段；入参零改动 | PASS |
| **C1b** | 关闭态不产副本/不改字段 | `[id(c)]` 与旧实现逐项相同 | PASS |
| **C2a** | **R1** 100 候选 / `top_k=50` | 只 50 条 `score_stage=rerank`；尾部 50 条原分、原顺序、无 `score_breakdown`；入参未改 | PASS |
| **C2b** | **R1b** `push.rerank_top_k=2`（config 表） | 只 2 条被重排 | PASS |
| **C3** | **R2** 权重来自 config | 默认 hot(0.90) 第一 0.4575 → 改权重后 fresh 第一 **1.2**；坏 JSON/缺键/未知键/坏值各自退默认或补默认 | PASS |
| **C4** | **R3** 分项和 == score（1e-6）、六键齐全、`score_stage=rerank`、`first_phase_score` 留档、手算 oracle 0.75 | 全中（`round` 前求和的恒等式亦复核） | PASS |
| **C4b** | **R3 篡改**：只改 breakdown / 只改 score → 和式必红；breakdown+score **一致地**改 → 和式查不出（被单点 oracle 抓） | 三种篡改行为与预期一致 | PASS（并暴露 R3 的独立判据只有 oracle 一个点，见 §4） |
| **C5** | **R4** dup 计数：0/5 次差 **0.30**；**跨会话**推的也算（3 次 → 0.6）；25h 前推的不算；10 次封顶 1.0（不是 2.0） | 全中 | PASS |
| **C5b** | **R4** `dup_penalty` 权重 0 → 两条相等 | 相等（1e-9） | PASS |
| **C5c** | **R4 与 `_silence_weight` 是否二次惩罚（判断）** | **部分重叠，不是同一计数的重复扣分**：`dup` 数**次数**（全局 24h，封顶 5），`silence` 看**ignored 比例**（按 code 全历史，下限 0.15）。同一现象（反复推且被忽略）会被两条通道叠加：实测 5 次 ignored → `weight=0.15` 且 `dup_penalty=1.0`（另加 `_cand` 的 `false_p` 成本项）。**建议**：开启 rerank 前用真实采纳率校准 `dup_penalty` 权重，否则"被忽略过的条目"可能被压得过死 | PASS（结论已给） |
| **C6** | **R5** 同一池 top-3 前后变化；`QAA-261027-23`（同项目+新鲜+被读过）升第一 | 前 `[21,22,23]` vs 后 `[23,21,22]` | PASS |
| **C6b** | **R5 补**：不是"只算不用" | 选择器返回的分**就是**重排分（带 `score_stage`）；`build_push_block` 真把 `project`/`now` 透传给选择器（mock 捕获） | PASS |
| **C7** | **R7** mem/kb/corpus 三类同池 `kind_class` 各 ≥1；`kind` 一字不改；`conv:` 前缀不变（既有锁 `test_corpus_push.py:142` 原样） | `{mem,kb,corpus}` 齐；`kind=prompt_related/kb/conv_related`；`conv:dsh:3931d23f#seq=10` | PASS |
| **C8a** | **断路器 · kb**：慢 0.2s / 阈值 0.05s → 候选 0 条；诊断**恰 1 行**，键恰为 `{ts,component,seconds,query_hash}`，`component=kb`，`query_hash=sha256(query)[:12]`，**文件内无 query 原文** | 全中 | PASS |
| **C8b** | **断路器 · conv** | `component=conv`，同样切断 | PASS |
| **C8c** | 断路器边界：`0`=关闭（慢也不切）；坏阈值 `abc`→默认 3.0（不切）；kb/conv 抛异常时**记忆候选照常递出**；诊断写不动（root 指向文件）**仍切** | 全中 | PASS |
| **C9a** | **对抗 · `_cfg_any`**：未登记/`true/false/1/0/yes/on/TRUE/abc`/`"   "`/坏数字/未知键；模拟登记后 `get_config` 赢过 config 表、`KeyError` 回落表原值 | 全部不抛、按类型退默认 | PASS |
| **C9b** | **对抗 · top_k 畸形**：`0→1`、`-5→1`、`10**9→500`（此池 100 条全重排）、`None→50`、`"abc"→50`、`"7"→7`、`True→1` | 全中（**与交付文档"<1 退 50"不符**，见 §3-①） | PASS（行为） / 文档订正 |
| **C9c** | **对抗 · `rerank_candidates` 边界**：`[]`/`None`→`[]`；单条/缺 `code`/缺 `score`/`freshness={"factor":"abc"}`/`project` 非串/非 dict → 不抛；`score="abc"` → ValueError（记录） | 均可判 | PASS（🟡 见 §4-②） |
| **C9d** | **分数量纲混排**（§10.3）：`top_k=2`、权重全 0 → 头部新分 0.0、尾部首阶段分 0.9，最终排序把尾部顶到头部之前 | 独立复现 | PASS（已知限制确认） |
| **C9e** | **§10.4 负分门槛**：开启后负分候选被 `score>0` 挡掉 | `picked=[]` | PASS（已知限制确认） |
| **C10** | 项目推断（多数票 / 并列取字典序）、推不出→中性 **0.5**、同别名组→**0.5** | 全中（别名走 `_walk_alias` + `name_aliases` 真表） | PASS |
| **C11** | `__all__` 与 T03b 前**逐项相同**（新函数不进去） | 相同 | PASS |
| **C11b** | `fish.py` 的 re-export 仍指到 `fish_push` 实现（J7 一侧） | 8 个名字 `is` 全等 | PASS |
| **C11c** | 新函数**未**被 `fish.py` 意外 re-export（J7 双向相等另一侧） | `fish` 无 `rerank_candidates/_cfg_any/...` | PASS |
| **C12** | rerank 开启后配额语义不变：serendipity 必占一格；`prefer_kind` 仍优先 | 两者都成立 | PASS |
| **C13** | 默认 `top_k=50` > 生产池上限 12 → 全池重排（尾部不存在） | 12/12 | PASS |

### 回归 / 边界（任务书第 10 条）

| 项 | 读数 |
|---|---|
| 全量 `discover -p "test_*.py"` | **Ran 1513 / OK (skipped=2) / 245.418s**（目标 1513+ 达成；2 条 skip 与基线同源：`test_split_contract` J8 + 语料 Perf） |
| 开发方 T03b 判据 | `Ran 15 / OK`；打印 `[R6a] 0.004s`、`[R6a2] 1.030s`、`[R6b] 0.430/0.382s` |
| 变异 | `mutation_push_rerank.py` → **M8 / M9 2/2 CAUGHT**，还原后 `git hash-object p0/fish_push.py` = `84d691f…` 与 HEAD blob 一致，工作树 `git status` 干净，其余 5 个既有判据无新红 |
| `fish_push.py` 行数（J4 口径 `len(split("\n"))`） | **1499 ≤ 1500**（剩 1 行）；实际 1498 行 + 尾换行 |
| 改动面 | `git diff --name-only 6a7f4e6..HEAD` = **4 个**：`p0/fish_push.py`(M)、`test_push_rerank.py`(A)、`mutation_push_rerank.py`(A)、`docs/T03b-…md`(A)。**无** `fish.py` / `fish_common.py` / 任何 hook / 任何既有测试改动 |
| `conv:` 锁 | `test_corpus_push.py:142` 未改（diff 面证明） |
| **生产树** | 生产 `大肥鱼生命系统/p0` **不存在** `fish_push.py` / `test_push_rerank.py` / `mutation_push_rerank.py`（T03b 面未落生产 = "一行未改"在 T03b 作用域成立）。⚠️ 与 Wave 0 manifest（327 文件）比对有 **9 处 sha 不同**：`51/52/53` 三份文档、4 个 `fish-home/*/看板-261027.md`、`p0/test_b3_round2.py`、`SESSION_STATE.md` —— 全部属于**其他任务/会话**的写入，无一是 T03b 面（数据表 `data/` 不入 manifest） |
| 待 A 登记 | 四个键确实**不在** `fish_common.CONFIG_DEFAULTS`（实测 `config_defaults_push` 无 `rerank_*`/`breaker_seconds`），当前全走 `_cfg_any` 未登记退路 |

---

## 2 · R6 · 真实 hook 端到端（**最关键**）

### 2.1 方法（为什么这样测）

三套代码树**只差 `fish_push.py`**：`old` = `6a7f4e6` 的 `fish_push.py`（hash `861e8e6…`），
`new_off` / `new_on` = HEAD 的 `fish_push.py`，`fish.py` 与全部 hook 完全相同。
数据 = **生产 `data/index.sqlite` 的 sqlite backup 快照**（`mode=ro` 只读打开，生产一行不写）。
因此：`old → new_off` = T03b 在默认关态引入的增量；`new_off → new_on` = 打开 rerank 的增量。
三轮：① 冷/热各 5 次顺序跑；② **轮转交错**（每轮换起点，抵消顺序漂移）+ 含 `corpus.sqlite`
探针；③ **同一热目录严格 off/on 交替 8 对**。共 **73 次真 hook 子进程**（每次真递出/真算完，
记录 stdout 长度，非空才算"真递出"）。

### 2.2 读数（墙钟，秒）

| 场景 | 样本 | min | 中位 | max | <2s |
|---|---|---|---|---|---|
| 第 1 轮 `old`（T03b 前） | 10 | 1.235 | 1.383 | 1.628 | 10/10 |
| 第 1 轮 `new_off` | 10 | 1.380 | 1.477 | 1.594 | 10/10 |
| 第 1 轮 `new_on` | 10 | 1.227 | 1.305 | 1.521 | 10/10 |
| 第 2 轮（轮转）`old` | 6 | 1.883 | 2.138 | 2.811 | 2/6 |
| 第 2 轮（轮转）`new_off` | 6 | 1.785 | 2.101 | 2.396 | 3/6 |
| 第 2 轮（轮转）`new_on` | 6 | 2.156 | 2.401 | 2.986 | 0/6 |
| 第 2 轮（含 corpus）三场景 | 9 | 1.825 | 2.209 | 2.751 | 2/9 |
| 第 3 轮（同目录交替）`new_off` | 8 | 2.123 | 2.349 | 3.017 | 0/8 |
| 第 3 轮（同目录交替）`new_on` | 8 | 2.260 | 2.520 | 3.936 | 0/8 |
| **合计** | **73** | **1.227** | **1.993** | **3.936** | **37/73** |

配对差（同轮/同目录内）：
* `new_on − new_off` = `[+0.189, +0.247, +0.796, +0.304, +0.694, −0.079]` → 中位 **+0.275**（散布 0.9，**无法与噪声区分**）
* `new_off − old` = `[−0.516, −0.118, −0.098, −0.141, −0.281, +0.149]` → 中位 **−0.130**（默认关**没有**变慢）
* `prompt-push` 单进程（关/开交替 6 对）→ 中位差 **−0.026**
* 决策路径 in-process（真 vault，关/开交替 10 对）→ 中位 **0.703 / 0.668**，差中位 **−0.031**
* `rerank_candidates` 单独（2 条候选，真库）→ **0.5–0.95 ms**

### 2.3 归因（拆段，各 5 次）

| 段 | 中位 |
|---|---|
| `python -c pass`（一次解释器启动） | **0.174s** |
| `import fish`（748KB 门面 + 依赖） | **0.508s** |
| hook 侧 `import hook_io, fish_activity` | **0.352s** |
| `connect + executescript(SCHEMA) + migrate`（真库快照） | **0.011s** |
| 决策路径（真 vault，含 kb 检索，关/开） | **0.703 / 0.668s** |

hook ≈ 0.174（hook 进程）+ 0.352（hook 依赖）+ 0.174 + 0.508 + 0.011（fish 子进程）+ 0.67（决策路径）+ I/O ≈ **1.9–2.1s**；再叠加负载/杀软/缓存，实测 1.23–3.94s。

### 2.4 裁决（三选一，不含糊）

> **T03b 增量达标，但绝对值未稳（升级 A/T05）。**

* **T03b 增量**：`rerank` 计算本身 **0.5–0.95 ms**；决策路径差 **−0.03s（噪声）**；单进程/整 hook 配对差 straddle 0。**T03b 不是超线原因**。
* **绝对值**：73 次里 **36 次 ≥ 2s**（中位 1.993s），且同一机器同一天三轮的中位从 **1.38 → 2.14 → 2.39s** 漂移 —— **54 号"hook 端到端 < 2s"不稳定满足**。
* **是不是 pre-existing**：**是**。T03b 前的 `old` 代码在第 2/3 轮同样 1.88–2.81s；大头是**两次解释器启动 + `import fish`（0.51s）+ hook 依赖导入（0.35s）+ 真 vault 决策路径（0.67s）**，与 T03b 无关。
  ⚠️ 交付文档把大头记成"`executescript(SCHEMA)+migrate ≈ 0.6–0.9s`"——**实测 0.011s**，该归因不成立（见 §3-②）。

---

## 3 · 失败 / 弱化 / 订正（ID、复现、实际 vs 期望、归属）

| ID | 级别 | 复现 | 实际 vs 期望 | 归属 |
|---|---|---|---|---|
| **D-1** | 🟠 文档 | `_qa_tmp/T03b/qa_unit.py`（C9b）；或 `python -c "…rerank_candidates(conn,c,top_k=0)"` | 交付 §2 偏差 5 写"`<1`/解析失败退 **50**"；实际 `<1`（含 0/负）→ **只重排 1 条**（`max(int(top_k),1)`），解析失败才退 50。行为可辩护（重排 0 条无意义），**文档与代码不符** | 交给 A（改文档，不改码） |
| **D-2** | 🟠 归因错误 | `_qa_tmp/T03b/qa_breakdown.py` | 交付 §6 写 "`executescript(SCHEMA)+migrate` ≈ 0.6–0.9s"；实测 **0.011–0.019s**。真实固定成本：两次解释器启动 0.35s + `import fish` 0.51s + hook 依赖 0.35s。R6 的论证前提失真（结论方向仍成立：超线不是 T03b） | 交给 A（订正 §6 与 R6 论据） |
| **D-3** | 🟡 真语料覆盖被低估 | `qa_corpus_real4/5.py`（生产 `corpus.sqlite` 只读快照） | 交付 §10.2 说"corpus 真库判不到（data/ 下没有 corpus.sqlite）"——**生产树有**（153MB，`corpus_fts` 50,767 行）；真路径能产 `conv:workbuddy:c48fd810#line=87036` 且 `kind_class=corpus`（与 mem 同池）。分支工作树缺 corpus.sqlite 才判不到 | 交给 A（可补真语料判据） |
| **F-1** | 🟡 潜在 API 陷阱 | `select_push_candidates(conn, (c for c in pool), rerank=True)` | 生成器/一次性可迭代入参在 **rerank=True** 时被 `_tag_kind_class` 吃光 → 返回 **0 条**；`rerank=False` 时正常 2 条。`_tag_kind_class` 的返回值被丢弃（`_tag_kind_class(cands)` 未赋值）。当前 3 个调用点都传 list → 生产不可达 | 工程师（T05 顺手改：`cands = _tag_kind_class(cands)`） |
| **F-2** | 🟡 判据强度 | `qa_unit.py` C4b | "分项和==score" 是**同源恒等式**（先写 breakdown 再 combine）；只有单点 oracle（0.75）独立。**一致地**篡改 breakdown+score 可绕过 sum（C4b 已复现），oracle 只钉一个特征组合点 | 判据设计（建议再加 1–2 个不同特征组合的硬编码期望） |
| **F-3** | 🟡 R4 生产可达性 | 读码 + `qa_real.py` | 真 prompt 路径上 `dup_penalty` **几乎恒 0**：kb 候选按 **全局 24h** 排除、mem/conv 按**会话 24h** 排除 → 只有"别的会话 24h 内推过同一条"才会非 0。R4 绿 ≠ 特征在生产活跃；开启 rerank 后它对"防重复"的实际贡献有限 | 设计口径（A 决定是否接受） |
| **F-4** | 🟡 透传不一致 | 读 `fish.py:9405` vs `fish_push.build_push_block` | `cmd_prompt_push` 调 `select_push_candidates` **未传** `project`/`now`（fish.py 属 A 作用域），开启 rerank 后 prompt 路径 `project_match` 走**推断**、`now` 取当前时刻；`build_push_block` 则透传。默认关无影响 | 交给 A（合并时决定是否补传） |

无 🔴：**未发现源码 bug**，判据语义与 PRD/设计一致。

---

## 4 · 三态（绿 / SKIP / 判不到）

| 态 | 内容 |
|---|---|
| **绿** | C1–C13（27 条）；R1–R5、R7（合成夹具三类同池）；断路器 kb/conv 两侧 + 诊断隐私 + 异常隔离；全量 **1513 OK**；变异 **2/2**；J4 **1499≤1500**；diff 面 4 文件；生产树 T03b 面无文件 |
| **SKIP** | 全量里 **2 条**（与基线同源，非本批引入）：`test_split_contract` 的 J8（拆分前备份不在）+ 语料 Perf 用例 |
| **判不到** | ① **含解释器启动的 2s 硬线在"稳定环境"下的复现**：本机三轮中位 1.38→2.39s 漂移，只能判"不稳"，不能判"恒不达标"；② **真库三类同池**：真语料侧拿到 mem+corpus（`kind_class=corpus` 已验），kb 因 24h 排除未同轮出现 → 三类同池只在合成夹具判到；③ **真语料上的断路器/预算**：断路器只在合成夹具造慢（真慢路径未在真库复现，断路器是后置判定、省不了墙钟） |

---

## 5 · 最易假绿 3 条（+2 补充）

1. **R6 口径被改写**：判据守的是"决策路径 <2s + 子进程 5s 守卫"，而 54 原文要"hook 端到端 <2s"。
   判据绿 ≠ 原意达标——我实测 73 次里 **36 次 ≥2s**，且交付文档把超线归因于 `executescript+migrate`
   （实测 0.011s），真实大头是两次解释器启动 + `import fish` + 真 vault 决策路径。
2. **R4 的 dup_penalty 在真 hook 路径上几乎恒 0**（24h 去重先挡掉了同会话/同 kb 候选）。
   判据用合成 push_log 造出 5 次 → 绿；生产上"重复推送防不住"的功效远小于判据观感。
3. **R3 的分项和==score 是同源恒等式**：改坏 breakdown 或 score 会被抓，但"一致地"改两者仍绿；
   独立判据只剩单点 oracle（0.75）。同理 **R1 绿 ≠ 只有 top-K 影响结果**：`top_k` 小于池时尾部
   首阶段分与新分混排（§10.3，C9d 复现）。
4. （补充）**R7 的"三类同池"是合成夹具**：真库同轮没同时出现三类（kb 被 24h 排除），
   真语料只验到 mem+corpus；不要把"合成夹具绿"读成"生产三类都活着"。
5. （补充）**默认关等价**只有对着 `select_push_candidates` 逐字节比；`build_push_block`/prompt 路径
   的"关态等价"是**读码 + 全量 1498 条既有判据**间接证明（新参数在关态不被使用），没有逐字节对拍整条注入包。

---

## 6 · 复现清单（全部在 `_qa_tmp/T03b/`，不改受跟踪文件）

```
cd I:\app\DSH-Home\workspace\learn\_qa_tmp\T03b
python qa_unit.py        # 21 条：C1–C9（等价/ R1–R5/断路器/对抗）
python qa_extra.py       # 6 条：C10–C13（项目推断/__all__/配额/池上限）
python qa_hook.py        # R6 第 1 轮：冷热各 5 次 × 关/开/旧代码
python qa_hook2.py       # R6 第 2 轮：轮转交错 + 含 corpus 探针 + 决策路径
python qa_hook3.py       # R6 第 3 轮：同热目录 off/on 严格交替 8 对
python qa_breakdown.py   # R6 归因：解释器/import/schema+migrate/决策路径
python qa_real.py        # R5 真数据：limit=1 关/开，16 条 query 里 1 条递出对象改变
python qa_corpus_real5.py# 真 corpus：conv: 候选 + kind_class=corpus（生产 corpus.sqlite 快照）
cd I:\app\DSH-Home\workspace\learn\fish-push\p0
python -m unittest test_push_rerank -v
python mutation_push_rerank.py
python -m unittest discover -s . -p "test_*.py"
```

证据文件：`qa-unit-result.json` / `qa-extra-result.json` / `qa-hook-result.json` /
`qa-hook2-result.json` / `qa-hook3-result.json` / `qa-agg.json` / `qa-breakdown-result.json` /
`qa-real-result.json` / `qa-corpus-real*.json` / `full-regress.log` / `mutation.log`。

---

## 7 · 给 A 的合并前置（3 条）

1. 按 §9 登记四个 config 键；登记后 `get_config` 的 bool 只认 `"true"`，与当前 `_cfg_any` 的
   `"1/yes/on"` 宽容读法**不同**——登记后请复跑 `OffEquivalenceTest.test_default_is_off` 与 R2。
2. 订正交付文档两处（D-1 `<1` 的实际行为、D-2 归因到 import/解释器启动而非 `executescript+migrate`），
   并把 R6 结论改成"增量达标 / 绝对值未稳"，作为 T05 的输入。
3. 生产树落 T03b 时注意：我这边比对 Wave 0 manifest 有 9 处非 T03b 差异（文档/看板/`test_b3_round2.py`/
   `SESSION_STATE.md`），合并前请以 A 自己的 sha 清单为准，别把别人的写入算到本批头上。

---

**验证人**：QA 严过关（独立复跑，未使用开发方脚本作判据） · **2026-10-28 00:15 +08:00** · HEAD `a06d988` / 代码 `f8d4679`
