# QA 独立验证 · T03a（语料侧候选与 Ranker 对齐）

> 验证者：严过关（QA）｜不采信工程师自报，全部自行复跑
> 仓库：`learn/fish-push`｜分支 `dev/fish-push-261027`｜**HEAD `3fe39a0`**｜基线 `e8acaf4`
> 首测时刻：2026-10-27 21:03–21:44（+08:00）｜Python 3.11.9 / Windows
> 只读声明：生产树 `大肥鱼生命系统/p0` 一行未改；`E:\Obsidian` 未碰；真库只在**临时只读副本**上读过；
> 本文件是本次唯一新增文件（`git status` 只多这一条 untracked）。
> 判据面结论：**T03a 无源码缺陷**；C5 迁移**接受**（附一项捕获能力缺口）；发现 2 项宜在 T03b/T04 处理的语义/稳健性缺口。

---

## 0 · 结论（先行）

| 项 | 读数 |
|---|---|
| **Total / Passed / Failed** | 全量 **1424**（含 1 skip）：Round 1 首跑 **1422 pass / 1 skip / 1 fail**（环境假红，隔离复跑 3/3 绿）→ 同 HEAD 复跑 **1423 pass / 1 skip / 0 fail**；T03a 专项 21/21；契约专项 27/27；变异 3/3 |
| **Routing Decision** | **NoOne**（无源码 bug；不存在的"测试断言错"也不适用：我没有改任何测试；2 项缺口登记见 §8） |
| **IS_PASS（T03a 范围内）** | **YES**，但两处必须按 §8 转记（C5 绝对延迟 → T04 红项；`score` 语义 → T03b） |
| **C5 裁决** | **接受迁移（有证据）**——见 §3（附条件：绝对延迟的捕获能力**确实有损**，须登记为 T04 红项，不能当"已达标"） |
| **既有断言被改/放宽** | **无**（`test_corpus_push.py` / `test_corpus_search.py` 与基线**逐字节相同**；唯一被改的 `test_b3_round2.py` 属基线提交 `dd76909`，是去 flake 不是放宽，见 §1.3） |
| HEAD / 时刻 | `3fe39a0` / 全部读数截止 2026-10-27 21:42:23 +08:00 |

---

## 1 · 必查 1：契约不变（逐条给证据）

### 1.1 六条契约的复跑读数

`cd p0 && python -m unittest test_corpus_push -v` → **Ran 27 / OK / 4.607s**（我本机）。

| 契约 | 守它的既有用例（**未改一行**） | 读数 |
|---|---|---|
| `kind == "conv_related"` | `CandidateTest.test_candidate_is_produced`（:143） | 绿 |
| `conv:<host>:<sid8>#<loc>` | 同上（:142）+ `test_rendered_line...`（:151） | 绿 |
| 120 字预算 | `test_rendered_line_is_within_budget_and_has_pointer`（:150） | 绿 |
| 单批 1 条 | `test_only_one_item_per_batch`（:170） | 绿 |
| `score > 0` | `test_every_candidate_has_positive_score`（:161） | 绿 |
| 24h 去重 | `test_duplicate_within_24h_is_suppressed` | 绿 |

`PUSH_DUP_WINDOW_HOURS = rank_mod.DUP_WINDOW_HOURS = 24`，`conv_push_for_session` 只是把字面量 `24` 换成这个常量
（C1 判据 `assertEqual(seen[0][0], 24)` 钉住）——窗口值未变。

### 1.2 既有测试文件是否被改/放宽

```
git diff --stat e8acaf4..3fe39a0 -- p0/test_corpus_push.py p0/test_corpus_search.py p0/fish.py p0/fish_corpus_store.py p0/test_corpus_governance.py
（空）
逐行比对：test_corpus_push.py 508→508 行、差异 0 行；test_corpus_search.py 571→571、差异 0 行
```

**结论：既有断言零改动、零放宽。**

### 1.3 唯一被改的既有测试（归属基线提交，非 T03a）

`p0/test_b3_round2.py::test_write_id_suffix_is_case_insensitive`（+12/−2 行，提交 `dd76909 test(baseline): deterministic write_id probe + gitignore runtime data/`）。

- 改法：原版一次性写一条笔记并断言 `write_id.upper() != write_id`；新版**重抽到含字母的 write_id**（最多 10 次），
  10 次都抽不到就 `self.fail`。原断言（大写形态可 rollback、`removed` 为真、文件消失）**全部保留**。
- 判定：**不是放宽**。原版有 2.3%（(10/16)^8）概率抽到纯数字 write_id 而假红；新版 10 连纯数字的概率约 1e-16，
  且"大写不敏感"的捕获能力仍在（`assertTrue(back["removed"])`）。
- ⚠️ 但**交付报告 §2 的改动清单没登记这一条**（报告只列 5 个文件，分支 diff 实际是 7 个）。属登记遗漏，非代码问题。

---

## 2 · 必查 2/3/4：C1–C4

`FISH_CORPUS_PERF_DB=<我的只读副本> python -m unittest test_corpus_rank_align -v` → **Ran 21 / OK / 14.712s**。

| 判据 | 我核到的实现证据 | 读数 |
|---|---|---|
| **C1** 候选带 `kind_class=="corpus"`/`score`/`score_breakdown{relevance,freshness,project_match,dup_penalty}`/`reason`/`freshness` | `fish_corpus.py:1191-1208`（无 scorer 路径）+ `:1230-1232`（scorer 路径 `setdefault` 只补不改） | 绿 6/6 |
| C1 附加：`score` 不被候选层改写 | scorer 路径**没有** `setdefault("score")`；`cand["score"]` 逐字等于 `_cand` 产物 | 绿 |
| `conv:` 前缀一字未变 | `fish_corpus.py` 内**零处**手工拼 `conv:`（只有 docstring/scope 解析出现该字面量），生成点唯一在 `store.push_pointer` | 绿 |
| **C2** DefaultRanker 两遍一致 | `DefaultRanker.rank` 无随机源；`rank(cands,q,{...,"now":NOW})` 两遍逐字相等 | 绿 2/2 |
| **C3** 热替换 + 未知名报错 | `register_ranker` 后下一次 `search(ranker=NAME)` 立即生效；未知/空名/非可调用 → `RankerError`（`ValueError` 子类，不静默回落） | 绿 2/2 |
| **C4** 门牌与既有锁一致（不另造） | `test_c4_code_round_trips_through_push_pointer` 用 `store.push_pointer` 反算 + 正则 `^conv:dsh:[0-9a-f]{8}#(seq\|line)=\d+$`；`test_c4_existing_lock_still_passes` **内嵌跑既有用例**（0 fail/0 error） | 绿 2/2 |

> 口径澄清（不构成缺陷）：54 §2.2 要 `kind:"corpus"`，而 `test_corpus_push:143` 锁 `conv_related`；工程师按主理人裁决
> 拆成 `kind`（既有锁）+ `kind_class`（语义层）。**契约不变的要求优先**，这个拆法正确。另：34 §3.4 示例里的键名是
> `breakdown`，54 与实现用 `score_breakdown`——T03b 按 `score_breakdown` 取，别按 34 的示例。

---

## 3 · 必查 5：C5 裁决（最关键）

### 3.1 我的独立测法与读数（不跑工程师的测试）

只读副本：生产 `corpus.sqlite` 经 `sqlite3.backup` 一次性快照 → 临时目录（源库 `mode=ro`），
**50,767 段 / 167 会话 / 31 项目**（与工程师报的段数一致；但**非空项目只有 1 个**：`anyi-studio` 35,976 段，
其余 14,791 段 `project=''`——这直接影响"查询谱"的覆盖面，见 §9）。

脚本：`%TEMP%\qa-t03a\probe_c5.py`（8 个真实四字查询 + 1 个零命中对抗样本；每查询 5 轮，**隔轮交错**量
`search(count=False)` / `conv_push_candidates` / `search(count=True)`，各取 min；另用 `set_trace_callback` 做 SQL 级取证）。

| query | 命中 | search(off).min | conv.min | delta(min) | count=True 增量 |
|---|---|---|---|---|---|
| 间顺序分 | 3 | 76.2 | 65.5 | −18.0 | +34.8 |
| 交与重复 | 3 | 82.8 | 72.1 | −72.6 | +89.6 |
| 正文被后 | 3 | 42.4 | 38.0 | −17.1 | +26.9 |
| 结论全部 | 3 | 439.9 | 498.2 | −125.4 | +252.7 |
| 是注释层 | 3 | 54.4 | 52.2 | −11.6 | +43.2 |
| 市工信局 | 3 | 9.1 | 7.9 | −2.9 | −0.1 |
| 同一份结 | 3 | 61.1 | 56.3 | −5.2 | +47.6 |
| 核实后的 | 3 | 47.4 | 48.6 | −12.6 | +38.4 |
| 龘靐齉爩（对抗） | 0 | 0.1 | 0.1 | −0.4 | +0.1 |

- **裸 `fish_corpus.search`**：**9.1–439.9ms**，其中 **5/8 > 50ms**（本机有负载；工程师机上为 40.4–58.5ms，同样有 >50 的样本）。
- **`conv_push_candidates`**：**7.9–498.2ms**，5/8 >50ms、**1/8 >300ms**（498.2ms）。
- **候选层增量**：我交错量的 8 个查询**全为负**（−2.9…−125.4ms）；工程师的顺序固定测法 7 次为
  `18.0 / −19.0 / 0.8 / 2.8 / 13.4 / 18.8 / −2.5`（最差 18.8，**距 20ms 上限只剩 1.2ms**）。
- **SQL 级结构取证（确定性，不靠钟）**：同一查询下 `conv_push_candidates` 的**用户级 SQL 序列与 `search(count=False)` 完全相同**
  （0 条额外语句）；`count=True` 恰好多一条 `SELECT COUNT(*) ... MATCH`（= FTS 再全扫一遍）。三条路径的语句数示例：
  `search(off) 用户级 2 条（PRAGMA+SELECT）/ conv 1 条（同一 SELECT）/ search(on) 2 条（多 COUNT）`。
- **M12 的抓取面**：`count=True` 的增量**依查询权重而变**——重查询 +26.9…+252.7ms（7/8 ≥20ms），
  轻查询（`市工信局`，FTS 内部语句仅 103 条）**−0.1ms**。

### 3.2 三问逐条回答

1. **原 54 C5「<50ms」能否稳定满足？** **不能。** 5 万段库上**未改动的裸 `search` 自己就 5/8 >50ms**（我 9.1–439.9ms；工程师 40.4–58.5ms）。
   它测的不再是"候选层开销"，而是既有 FTS postings 并集成本 + 机器负载 → 每次运行都可能随机红。
2. **改成「增量<20ms + 量级<300ms」是否保留了原判据的捕获能力？** **两类退化分开看**：
   - 「候选层自己查库 / 逐条 count / 全量扫描」→ **保留**。证据：M12 被抓住（增量 +26.9…+252.7ms）；更硬的是结构取证
     ——候选层用户级 SQL 序列与 `count=False` 检索**逐条相同**，逐条 count 会多出语句，全量扫描会多出一条扫全表的语句。
   - 「绝对延迟」（原 50ms 真正想守的上界）→ **不保留**：上限放宽到 300ms（6 倍），且**没有替代判据**；
     一个 50→280ms 的检索退化不会被任何判据发现。
3. **M12 是否真能抓住 `count=True`？** **能，但只在重查询上稳**（我复跑变异脚本：M12 红 ✓，sha256 逐字节还原 ✓）。
   若 `_pick_query` 换到轻查询（`市工信局`：增量 −0.1ms），**同一条变异不会红**——抓取能力依赖"被测查询有多重"。

### 3.3 裁决（三选一）

> **接受迁移（有证据）。**
> 依据：①50ms 原判据在现库上对**未改动的检索**就不可满足（§3.1），保留它不是"守门"而是"必然随机红 + 归因错误"；
> ②候选层的退化形态仍被守住（M12 + SQL 结构取证双重）；③量级上限仍挡住秒级退化。
> **附条件（不可省）**：迁移只对"候选层增量"给了证据，**"绝对延迟"的捕获能力确实有损**（50→300ms）。
> 因此 54 C5 不能记为"达标/已迁移完成"，必须把"绝对延迟"作为 **T04（索引/检索）** 的红项重新登记，判据建议：
>
> ```python
> # T04 红项（现在就是红）：同一只读副本、同一组查询，search(count=False) 的 min 必须 < 50ms
> # 我的实测：8 查询里 5 个 >50ms（9.1 / 42.4 / 47.4 / 54.4 / 61.1 / 76.2 / 82.8 / 439.9）
> ```
>
> 另建议把 C5a 的"增量"改成**结构判据**做主闸（确定性、零噪声）：候选层的用户级 SQL 序列必须与
> `search(count=False)` 相等（不等即红），再把 20ms 计时降为"留档读数"。这样 M12 在轻查询上也不会漏。

---

## 4 · 必查 6：反例 / 对抗

| # | 对抗点 | 结论 | 证据 |
|---|---|---|---|
| 6.1 | `score_breakdown` 与 `score` 会不会不一致？候选层有没有偷偷改 score？ | **不偷偷改**（无 scorer 时 `score` 就是 `breakdown["relevance"]`，同一份数值；scorer 路径不覆盖）。但**两者不可对账**：scorer 路径的 `score` = `_cand` 的 `(benefit−cost)×weight`，与四项按 `FEATURE_WEIGHTS` 加权和不是一回事 | `fish_corpus.py:1188-1208/1230-1234`；`fish.py:7952-7963`。34 §3.5 R3（分项和==score，1e-6）在 corpus 候选上**当前必红**，T03b 必须重算 score（交付报告 §8 已写） |
| 6.2 | `dup_penalty` 归一是否 0–1？ | **是**：`min(max(count,0),5)/5 ∈ [0,1]`，负号在权重上（−0.30） | `fish_corpus_rank.py:dup_penalty`；C0 用例 0/1/2/5/6/500/−3 全覆盖 |
| 6.3 | `freshness` 是否与 `_cand` 的 double-discount 纪律冲突？ | **不冲突**：`fish.py:8065` 传给 `_cand` 的仍是 `freshness=None`（我逐行核过现状）；item 的 float freshness 只做观测值。但**该纪律仍无自动化判据**（交付报告 §6.5 自己也说了） | `fish.py:8059-8065`；建议加判定：monkeypatch scorer 断言 `kwargs["freshness"] is None` |
| 6.4 | `kind_class` 会不会漏加？ | **两条路径都加**（无 scorer 显式写，scorer 路径 `setdefault`）；`scorer` 返回 None 或 `score<=0` 的候选被 skip，不会漏出 | `fish_corpus.py:1206/1218-1233`；C1 两条用例钉住 |
| 6.5 | `freshness` 类型跨类不一致 | 🟡 **真问题**：mem/kb 候选的 `freshness` 是 **dict**`{factor,band,age_days}`（`fish.py:7966/8737`），corpus 候选是 **float**（C1 断言 `assertIsInstance(..., float)`）。合流池里同名字段两种类型 → T03b 若按 dict 取会炸 | `fish.py:7964-7966` vs `fish_corpus.py:1207/1231` |
| 6.6 | `recent_counts` 回包形态 | 失败/`None`/字符串 → 按 0 且计 `skipped["dup_unknown"]`；集合 → 按推过 1 次；dict → 取次数。**字符串特判是对的**（否则会按字符迭代造"每个字推过一次"的假读数） | C1 三条用例 + `_dup_count_map` 实现 |

---

## 5 · 变异自证（独立跑）

`FISH_CORPUS_PERF_DB=<副本> python mutation_corpus_rank_align.py` → **exit 0，3/3 CAUGHT**，每轮 `sha256 一致 ✓`：

| 变异体 | 必须红的判据 | 我的读数 |
|---|---|---|
| M10（删 `project_match`） | `CandidateShapeTest.test_c1_item_has_kind_class_score_and_breakdown` | **FAIL ✓** |
| M11（`DefaultRanker` 注入随机） | `RankerDeterminismTest.test_c2_default_ranker_is_deterministic` | **FAIL ✓** |
| M12（候选层 `count=True`） | `PerfTest.test_c5_real_corpus_candidate_layer_overhead_under_20ms` | **FAIL ✓** |

每个变异体期间 `test_corpus_push` 保持绿（脚本内 `others` 跑过）；还原后 `test_corpus_rank_align` 复跑绿。
跑完 `git status --porcelain` **空**；298 个受跟踪文件 **sha256 逐个比对：0 个变化**。

---

## 6 · 必查 7：全量回归

| 轮 | 命令 | 读数 |
|---|---|---|
| Round 1 首跑 | `python -m unittest discover -p "test_*.py"`（21:08:58→21:20:01） | **Ran 1424 / FAILED (failures=1, skipped=1) / 659.0s** |
| Round 1 复跑（同一 HEAD、无 env） | 同上（21:34:18→21:42:23） | **Ran 1424 / OK (skipped=1) / 481.3s** |

唯一红：`test_corpus_governance.EraseLatencyTest.test_erase_caps_busy_timeout_and_stays_fast`
→ `AssertionError: 5.6745 not less than or equal to 5.0 : 擦除等了 5.7s`。

**判定：环境/负载假红，不是 T03a 回归。** 四条证据：
1. 被测实现与用例**与基线逐字节相同**（`git diff e8acaf4..3fe39a0 -- p0/test_corpus_governance.py p0/fish_corpus_store.py` 为空）；
2. 隔离复跑 **3/3 OK**（`python -m unittest test_corpus_governance.EraseLatencyTest`）；
3. 该断言的余量本就只有 1s（`busy_timeout` 2s × 2 轮 ≈ 4s vs 5.0s 上限），是典型的负载敏感断言；
4. 首跑整轮 659s vs 复跑 481s（+37%），同机同代码——首跑期间机器明显更慢。
`skipped=1` = C5a（未设 `FISH_CORPUS_PERF_DB`，全量跑时按设计 SKIP）。

---

## 7 · 三态（绿 / SKIP / 判不到，分开写）

- **绿（实跑通过）**：C0 7 + C1 6 + C2 2 + C3 2 + C4 2 + C5a 1 + C5b 1 = **21/21**（设了真库副本）；`test_corpus_push` **27/27**；变异 **3/3**；全量 **1424（1423 pass + 1 skip）**。
- **SKIP（跳过，不算 PASS）**：**C5a** 在未设 `FISH_CORPUS_PERF_DB` 时 `skipTest`（全量跑里那 1 条）。本报告的 C5 结论**不是**靠跳过得出的：我显式指向了副本，并额外做了 SQL 级结构与 9 查询计时。
- **判不到（我明确声明判不了）**：
  1. **54 C5「50ms 稳定满足」**——不是"没测"，而是**测出来不满足**（§3.2-1）；"稳定"一词在现库上无意义。
  2. **hook 端到端 <2s / 真实推送链路的 `dup_penalty`**——属 T03b（`fish.py` 尚未接线，`dup_penalty` 在真实链路恒 0，交付报告 §6.2 已披露）。
  3. **`access_ratio` / `link_strength` 的取数与分项和**——T03a 只给纯函数，R3 属 T03b。
  4. **跨平台**：本机 Windows/Python 3.11 单平台；`exp/round` 浮点行为在其他平台未验证（影响 ≤1e-6，非阻塞）。

---

## 8 · 弱化 / 缺口登记（判据 ID、复现、实际 vs 期望、归属）

| ID | 严重度 | 问题（实际 vs 期望） | 复现命令 | 归属 |
|---|---|---|---|---|
| **F1** | 🟠 判据稳健性 | **C5a 的 delta 判据噪声带 ≥ 阈值**：顺序固定测法 7 次读数 `18.0/−19.0/0.8/2.8/13.4/18.8/−2.5`（最差 18.8，余量 1.2ms）；我交错测法 8 查询**全为负**（−2.9…−125.4）。期望"只对代码变化敏感"，实际"正负由谁后测/机器负载决定" | `$env:FISH_CORPUS_PERF_DB=<副本>; python -m unittest test_corpus_rank_align.PerfTest -v`（连跑 5 次）；对照 `%TEMP%\qa-t03a\probe_c5.py` | 建议 T03b：增量改**结构判据**（用户级 SQL 序列相等）做主闸，20ms 降为读数 |
| **F2** | 🟠 捕获能力缺口 | **绝对延迟**：原 50ms → 现 300ms（6 倍），无替代判据。期望"检索变慢仍可见"，实际"50→280ms 的退化无人发现" | 同一副本上量 `search(count=False)`：我 8 查询 5 个 >50ms | **T04 红项**（交付报告 §6.1 已提"待办"，但未给判据；本条补上判据） |
| **F3** | 🟡 语义陷阱 | `score` 与 `score_breakdown` **不可对账**（scorer 路径 `score=(benefit−cost)×weight`，四项加权和 ≠ score）。34 §3.5 R3 在 corpus 候选上当前必红 | `p0` 下：`python -c "import fish_corpus_rank as r; print(r.combine_features({'relevance':0.62,'freshness':0.75,'project_match':1.0,'dup_penalty':0.2}))"` → **0.4815**（而 `_cand` 对同一条给出的 `score` 是 `(benefit−cost)×weight`，实测量级 0.1–0.3） | **T03b**（rerank 里必须重算 `score`） |
| **F4** | 🟡 类型不一致 | `freshness` 跨类两种类型：mem/kb = dict，corpus = float | 读 `fish.py:7964-7966` / `fish_corpus.py:1207` | **T03b**（合流池按 `kind_class` 分支取，或统一成 dict） |
| **F5** | ⚪ 弱判据 | C1 的 `project_match == 1.0` 在真实数据上**恒真**（检索已按项目下推；真库非空项目只有 1 个） | `p0/test_corpus_rank_align.py:203` | QA 记录；刻度已由 C0 覆盖，不必改 |
| **F6** | ⚪ 假绿面 | C4 的 `test_c4_existing_lock_still_passes` 用嵌套 `TestResult` 判定：**内层若被 skip** 会 testsRun=1 且 failures 空 → 假绿 | `p0/test_corpus_rank_align.py:451-460` | QA 记录（T03b 可加 `assertEqual(result.skipped, [])`） |
| **F7** | ⚪ 文档漂移 | 交付报告 §2 记 `test_corpus_rank_align.py` **633 行**，实际 **643 行**；§2 改动清单**未登记** `test_b3_round2.py`（在基线提交 `dd76909`） | `git show --stat 3fe39a0` / `wc -l p0/test_corpus_rank_align.py` | 交付报告（建议 A 合并时补记） |
| **F8** | ⚪ 假绿面 | C5a **不断言命中非空**：`_conv_once` 丢弃返回值；0 命中查询 `delta=−0.4ms` 照样绿 | 对抗样本 `龘靐齉爩`（我的 probe 第 9 行） | QA 记录（建议 `assertTrue(res["items"])`） |
| **F9** | ⚪ 语义澄清 | "热替换"实际是"**新注册立即生效**"：`register_ranker` 重名直接报错，**无法**替换已登记的 `default`（要换得改 `RANKERS`）。C3 标题的"热替换"与实现语义不完全同义 | `p0/fish_corpus_rank.py:368-377` | QA 记录（T03b 改权重走 config，不走替换 ranker） |
| **F10** | ⚪ 未闭合提醒 | `_cand(freshness=None)` 的 double-discount 纪律**仍无自动化判据**（我自己逐行核过现状成立） | `p0/fish.py:8059-8065` | T03b（顺手加一条） |

---

## 9 · 最容易假绿的 3 条

1. **C5a 可以"测了个空转"还绿**——测试不断言候选非空，0 命中查询的 delta 恒 ≈0（我实测 −0.4ms）。库/查询一漂，
   判据仍在"通过"，但它什么也没测。**这是本批最危险的一条**（性能判据最容易被读成"已守住"）。
2. **C5a 的 delta 正负由测量顺序决定**——先 conv×5 再 search×5，等于让后测的一批吃缓存红利；我交错测法把同一份代码量出
   −2.9…−125.4ms。也就是说"增量 0.3–4.3ms"这个漂亮读数里含着测法偏置，别当代码性质引用。
3. **C4 的"复用既有锁"可能被 skip 变成绿**——嵌套 `TestResult` 只看 failures/errors，不看 skipped；内层一旦跳过，
   外层照样 build 成功。引用式复用是对的，但"跳过 = 通过"这条缝要堵（一行 `assertEqual(result.skipped, [])`）。

补充（等量风险，未计入前三条）：**M12 的抓取能力依赖被测查询的权重**——轻查询（FTS 内部语句 103 条）上 `count=True`
增量只有 −0.1ms，同一条变异不会红。所以"20ms 增量"不是普适闸门，结构判据才是。

---

## 10 · 复现命令（一条条可跑）

```powershell
cd I:\app\DSH-Home\workspace\learn\fish-push\p0
# 契约（既有断言，未改一行）
python -m unittest test_corpus_push -v                     # Ran 27 / OK

# T03a 判据（真库副本：先用 sqlite3.backup 做一次性只读快照，别指向生产库本体）
$env:FISH_CORPUS_PERF_DB = "<副本>\corpus.sqlite"
python -m unittest test_corpus_rank_align -v               # Ran 21 / OK

# 变异自证（跑完 git status 必须干净、受跟踪文件 sha256 不变）
python mutation_corpus_rank_align.py                        # exit 0，3/3 CAUGHT

# 全量
Remove-Item Env:FISH_CORPUS_PERF_DB
python -m unittest discover -p "test_*.py"                  # Ran 1424 / OK (skipped=1)

# 我的独立 C5 探针（交错计时 + SQL 语句级取证 + 零命中对抗样本）
python %TEMP%\qa-t03a\probe_c5.py <副本>\corpus.sqlite
```

已知环境噪声：本机两次全量 659s / 481s（同代码），性能类断言（C5a 增量、EraseLatency ≤5s）在高负载窗口会晃；
复跑性能判据前请先确认机器空闲。

---

## 11 · 结论与交接

| 项 | 结论 |
|---|---|
| T03a 代码质量 | **通过**。契约零改动；新增字段只增不改；六特征纯函数化、权重单源；`conv:` 门牌仍唯一生成点；性能退化可被变异抓住 |
| 可交付 | **可交 A 合并**（T03a 范围内），合并前按 52 §5 逐 hunk 重放；本报告 §8 的 F1/F2/F3/F4 建议一并带进合并记录 |
| 不宜记"已完成"的 | **54 C5 绝对 50ms**——现状**红**（裸检索 5/8 >50ms），归 T04；**R3 分项和==score**——归 T03b |
| 回滚 | 单提交：`git revert 3fe39a0`（或 `git reset --hard e8acaf4`）；仅撤代码保留报告：`git checkout e8acaf4 -- p0/fish_corpus.py p0/fish_corpus_rank.py` + 删两个新测试文件 |

> 本报告的证据链全部可复跑；凡"判不到"处已单独列在 §7，未混入"绿"。
