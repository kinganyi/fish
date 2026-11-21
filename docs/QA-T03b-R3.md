# Test Report · T03b-R3 收口轮（S1/S2/S3）终验

> 验证人：严过关（软件开发团队 · QA，独立于实现者）
> 对象：`learn/fish-push`，分支 `dev/fish-push-261027`，HEAD **`3fd886d2625de0bab4fd076b70750d7ecf24ab02`**，代码 **`9f74388f92fe3397cc8926d04934145c3e7296f9`**（父 `0166b0d`，基线 `6a7f4e6`）
> 方法：**只读交付树**（未改任何被测文件；探针全部在 `%TEMP%` 的自建临时库/临时目录里跑）→ 自己写独立探针 → 自己跑模块判据/全量/变异 → 最后才对照交付文档口径
> 时刻：2026-10-28 01:57–02:16 +08:00（收工态 02:13 +08:00 = `2026-10-27T18:13:20Z`）
> 环境：Windows / Python 3.11.9；生产树 `learn/大肥鱼生命系统/p0` 与 vault `E:\Obsidian` 只读

---

## Summary

| 项 | 读数 |
|---|---|
| **验项（可证伪检查）** | **Total 8 ｜ Passed 7 ｜ Failed 1（非本批）** |
| **断言层** | 全量 `discover` **Ran 1537**（pass 1534 / fail 1 / skip 2，251.3s）；`test_push_rerank` **39/39 OK**（22.3s，0 skip）；变异 **9/9 CAUGHT**；默认关对拍 **52/52 一致**；S1 探针 6 组（A–F，含 R2 旧版对照）；S3 探针 9 例 + 2 个前提缺口 |
| **覆盖率（估算）** | 关键路径 ~90%（六特征装配/断路器/三类归一/config 容错/关闭态等价都有判据；未装 coverage，故为估算） |
| **Routing Decision** | **NoOne**（T03b 三条收口全部成立，无源码 bug 需打回）｜另记 **1 条非本批 Known Issue**（`test_maintenance` 时钟炸弹，见 §2） |
| **三态** | 源码 bug：**0**（必查 4 类坏值全过）｜ 源码口径观察：**4**（④⑤⑥⑦，均不阻塞）｜ 测试 bug/假绿风险：**3**（①既有测试红 + ②③口径脆性）｜ 环境：**1**（生产 `data/` 被活系统写，非我方） |

**IS_PASS（T03b-R3 三项）：YES**——S1/S2/S3 我逐条独立复现，读数与交付文档一致；全量唯一的红与本批 4 个文件无因果关系（下面给出反证）。

---

## 1 · 必查项逐条

### S1 · `project=None` 的混合池不再造出 1.0 → ✅ PASS

**读法（我自建 temp 库 + 最小候选字典，直读 `score_breakdown`；不引用 `test_push_rerank` 夹具）**

```
cd I:\app\DSH-Home\workspace\learn\fish-push\p0
python -m unittest test_push_rerank.PromptContextTest.test_R12d_unknown_project_mixed_pool_is_neutral -v
python %TEMP%\qa_r3_s1_probe.py      # 我写的独立探针（命令与读数见下表，可原样重放）
```

| 用例 | 输入 | `_infer_project` | `project_match` 读数 |
|---|---|---|---|
| A（评审原始反例） | `project=None`，3×`TESTB` + 1×`TESTA` | **None** | `{TESTA:0.5, TESTB:0.5, TESTB:0.5, TESTB:0.5}`——**全 0.5，无 1.0**；`select_push_candidates(..., rerank=True)` 选出 4/4，同样全 0.5 |
| B 单项目池 | `project=None`，2×`TESTB` | `TESTB` | `{1.0, 1.0}`——**唯一值仍采用**（中性 ≠ 永远 0.5） |
| C 平分混合池 | `project=None`，2×`TESTB` + 2×`TESTA` | **None** | 四条全 0.5，无 1.0 |
| D 生成侧贴标 | 同 A，但每条贴 `_ctx_project=TESTA` | `TESTA` | `{TESTA:1.0, 三条跨项目:0.0}`——可解析时仍走正路 |
| E 非记忆类 | kb + corpus(`conv_related`)，项目未知 | None | `{kb:0.5, corpus:0.5}` |
| **F 判别力对照** | 同 A 的池子喂 **R2 旧实现**（`0166b0d:p0/fish_push_rerank.py` 真 blob） | **`TESTB`** | `{测试三跨:1.0,1.0,1.0, 本项目:0.0}`——**旧 bug 原样复现** |

结论：修法 a（唯一值）+ 修法 b（推不出 → 0.5）都真的落地；探针 F 证明这套读数不是恒绿。R12d 的两半（混合池全 0.5 / 单项目池 1.0）都在判据里，且 M14 变异能把它打红（见 S2 日志第 47–48 行）。

> 顺带记录一次**探针自身的假红**：我第一版把语料夹具写成 `kind="corpus"`（真实是 `conv_related`），被 `semantic_kind` 归成 mem → 读到 1.0；改成 `conv_related` 后 0.5。这说明「未知 kind → mem」是设计行为，夹具写错 kind 会造出"corpus 也 1.0"的假红——不是源码 bug。

### S2 · 变异自证改为隔离副本 → ✅ PASS（含我旁路采样）

**命令**：`cd p0 && python mutation_push_rerank.py`（我另加了一个旁路驱动：每轮 4×`git hash-object`+`git status`，实际约 0.6s 一次，独立于被测脚本观测仓库）

| 阶段 | 我的读数 |
|---|---|
| 门禁 | 4 文件 `git hash-object` == `HEAD:p0/...`：`3afa0fd194b9` / `fceedc0a56fe` / `e3ef57109efc` / `f08851a9281a` 全 ✓（与交付文档一致） |
| 隔离副本 | `%TEMP%\mutation-t03b-copy-*`，**199 个文件**；4 个目标文件与仓库逐字节相同 ✓；排除 `data/ _audit/ __pycache__` |
| 副本基线 | `test_push_rerank` 绿 ✓；**副本内跳过 3**（R6b/R6d/R8b 真库类，脚本自己打印"不算通过"）；R14 经 `FISH_PROD_P0` 仍读到真语料 |
| 变异 | **9/9 CAUGHT**；M9 打红 R4+R6c、**M14 打红 R10+R12d**、M15 单点打红组合 oracle；每次"副本还原 sha256 一致 ✓"；5 个既有文件（`test_push_p0`/`test_push_p1a_candidates`/`test_push_kb`/`test_corpus_push`/`test_split_contract`）**无预期外新红** |
| 收尾 | `[4/5] 仓库受跟踪文件 sha256 全等 ✓ / git status 未变 ✓（前后各 5 行）`；`[5/5] 自测：故意写回 fish_push_rerank.py → 检出 ✓ → 还原后干净 ✓`；`exit=0` |
| **旁路采样（我的独立证据）** | 249 个采样（≈167s，每次 4×`git hash-object`+`git status` 约 0.6s）：**只有 1 个采样**出现偏离，内容正是 `fish_push_rerank.py` + `git status` 多出 `M p0/fish_push_rerank.py`；其余采样全程干净；**跑后** 4 文件与 HEAD 一致、`git status` 与跑前逐行相同（5 行未跟踪文档） |

结论：交付树在**变异阶段完全只读**；唯一一次写回就是设计里的 `[5/5]` 检测器自测（窗口 ~0.5s，`_drift()` 的 git 调用耗时，恰好被我的采样抓到）。R2 那个"交付树在运行窗口内 ≠ 提交内容"的形态**已消除**。

### S3 · config 坏值 → 回默认 + 诊断、不抛；诊断不含 prompt 原文 → ✅ PASS

**命令**：`python -m unittest test_push_rerank.ConfigFallbackTest -v`（4 条）；另跑我自己的探针（未登记 + **在内存里模拟"已登记"**，不动 `fish_common.py`）：

| 坏值 | 未登记态（当前仓库） | 已登记态（评审 Ⅱ1 的炸点，内存改 `CONFIG_DEFAULTS`） |
|---|---|---|
| `push.rerank_top_k="abc"` | `_cfg_any→50`；`select(rerank=True)` **不抛**、5/5 全进 rerank 阶段 | `get_config` 直读**确实抛** `ValueError`（对照）；`_cfg_any→50`；`select` **不抛** |
| `push.rerank_weights="{这不是 JSON"` | 与默认权重**逐条同分** ✓ | 权重 == 默认六项 ✓ |
| `push.breaker_seconds="x"` | `_cfg_any→3.0`；`elapsed=10s` **仍切断**（不是退成 0=关闭）、快路径照过 | `_cfg_any→3.0`（`get_config` 的 ValueError 被接住） |
| `now="昨天下午"` | 回落当前 UTC：刚推过 1 次 → `dup_penalty=0.2`（**不是 0.0**，24h 窗口没被静默关掉） | 同左 |

**诊断文件**：`<data>/_audit/push_config.jsonl`，7 行；键覆盖 `{push.rerank_top_k, push.rerank_weights, push.breaker_seconds, now}`；每行只有 `{ts,key,raw,fallback,reason}`。**我用带标记的 prompt 原文（`PROMPT-原文-不许进诊断-7f3a`）当候选标题/理由并传入，诊断文件里 0 次出现**；生产树 `data/_audit/` 下也**没有** `push_config.jsonl`（探针只写 temp）。

### 全量 / 模块 / 变异（读数）

| 项 | 命令 | 读数 |
|---|---|---|
| 模块判据 | `python -m unittest test_push_rerank` | **Ran 39 / OK（0 skip）**，22.3s |
| 全量 | `python -m unittest discover -s . -p "test_*.py"` | **Ran 1537，251.3s**（墙钟 253s）：pass 1534 / **fail 1** / skip 2 |
| 变异 | `python mutation_push_rerank.py` | **9/9 CAUGHT，exit 0**（≈167s） |
| 默认关等价 | 独立探针（当前 HEAD vs `6a7f4e6:p0/fish_push.py` 真 blob） | **52/52 一致**：5 池 × 3 limit × 3 prefer_kind = 45 组 + 5 组生成器入参 + 2 组显式/config=false。逐条 JSON 相同、**返回同一批对象**（id 相同）、关闭态**零新增字段**；基线 blob sha256[:12] = **`7a87fba903cb`**（与文档一致） |
| J4 | 行数（`split("\n")` 口径） | `fish_push.py` **1248** ≤1500 ✓；`fish_push_rerank.py` **398** ≤400 ✓（**余量只剩 2 行**，见 §3-②） |

`discover` 的 1 红：`test_maintenance.M3LedgerTest.test_M3_readings_are_readable_after_run`（断言 `kb_reindex` 的 `due` 为 False，实际 True）。2 skip：`test_split_contract` 的 J8（"拆分前备份不存在"）、`test_corpus_rank_align.PerfTest`（未设 `FISH_CORPUS_PERF_DB`）——都是环境门控，不是 T03b。

### 回归 / 边界

| 检查 | 读数 |
|---|---|
| `git diff --name-only 6a7f4e6..HEAD`（非 docs） | 只有 4 个：`p0/fish_push.py`、`p0/fish_push_rerank.py`（新）、`p0/mutation_push_rerank.py`（新）、`p0/test_push_rerank.py`（新） |
| 禁改面 | `fish.py` / `fish_common.py` / `fish_prompt_hook.py` / `fish_after_tool_hook.py` / 既有测试（`test_split_contract` 等 5 个）→ **零改动** |
| HEAD 一致性 | 4 个目标文件 `git hash-object` == `HEAD:p0/...` ✓；代码提交 `9f74388` 与文档登记一致；`3fd886d` 仅改 1 份文档 |
| 工作树 | 干净（7 份**未跟踪**文档：5 份既有报告 + 我的 QA 报告 + 另一会话 02:16 落盘的评审报告；报告外的未跟踪/修改 = **0**） |
| 生产树 | 3431 → 3433 个文件；**非 `data/` 文件 0 处差异**（源码/文档全等）；`data/index.sqlite` **主库未变**；`push_config.jsonl` 不存在。14 处差异全在 `data/`：`_audit/hooks/*`（活跃 hook 状态、心跳、`pretool-asks.jsonl`）、`_audit/counters/posttool-push-<本会话 id>.n`、`index.sqlite-wal`，mtime 全部落在我跑动的 02:12–02:13（**活系统自己在写**，含以我 session id 命名的计数器）——不是我的测试写入 |
| vault | `E:\Obsidian` 在 01:55 之后被修改的文件数 = **0**（R6b/R14 只读） |

---

## 2 · Failed Tests

```
test_M3_readings_are_readable_after_run
  Expected: after["kb_reindex"]["due"] is False
  Got:      True
  [p0/test_maintenance.py:286]（文件自 T04b 提交 1394603 起未改，且在基线 6a7f4e6 内）
```

**定性：既有测试的时间炸弹，不是 T03b 回归。** 反证（`%TEMP%\qa_r3_m3diag.py`）：

- 测试 `_Base.run_tasks()` 把 `now` 钉成常量 `NOW = 2026-10-27T12:00:00Z`；而 `maintenance_status()` 用**真实此刻** `_utcnow()` 算 `due`；
- `kb_reindex` 间隔 6h → 真实时钟一过 `12:00Z+6h = 18:00Z`（= 02:00 +08:00）这条必红；
- 实测：不改时钟 → **FAIL**（`AssertionError: True is not false : kb_reindex`）；把 `fish_maintenance._utcnow` 钉回 `NOW`（其余一字不改）→ **PASS**；
- 因果面：该文件与 `fish_maintenance.py` 都不在 `6a7f4e6..HEAD` 的 diff 里；据此推断上一轮"1532 OK"只可能跑在 18:00Z 之前（我这一轮跨过了这条线）。

---

## 3 · 三态与观察（Known Issues）

**必查的 S1/S2/S3 本身 0 缺陷**；下面 7 条是"边界 / 口径 / 环境"记录。除 ①（既有测试会红）外都不阻塞交付，也不该被记成 T03b 的失败。

| # | 三态 | 内容 | 影响 / 建议 |
|---|---|---|---|
| ① | **测试（既有，非本批）** | `test_maintenance.M3` 的固定 `NOW` + 真实时钟（上面已反证）。**过了 2026-10-27T18:00Z 起全量必 1 红**，且会一直红到有人改测试或改判据 | 不阻塞 T03b；建议 A 单独修：`maintenance_status` 接受 `now=` 或测试里 patch `_utcnow`（与本批 4 个文件零交集）。**不要**把它记成 T03b 失败 |
| ② | 测试（脆性） | `test_push_rerank.R15b` 的"新模块 ≤400 行"绝对上限：当前 398，**余量 2 行**（R2 评审 Ⅱ4 建议去掉/替换，未采纳） | 下一次正常演进就可能假红；建议改成比率（如 ≤`fish_push.py` 的 1/2）或直接删 |
| ③ | 测试（口径） | "关闭态逐字节等价"在**仓库内**只有 `OffEquivalenceTest`（同一天平两端：本实现的 on vs off）；**与基线 blob 的真对拍没有进判据**（交付 §0-3 如实写了"本轮未重跑"） | 我补的 52/52 只是**本轮证据**，不是**回归判据**；建议把基线对拍固化成一条测试（取 `6a7f4e6` blob 或存 sha 白名单） |
| ④ | 源码（轻微） | `_DIAG_SEEN` 是**模块级 set，只增不清理**（同 `(键,原值)` 去重）；长驻进程里坏配置种类多了会缓慢累积（每条 ~几十字节） | 无正确性影响；若将来 hook 常驻，可加 LRU/上限 |
| ⑤ | 源码（轻微） | `now` 的诊断把坏 `now` 原文写进 `_audit`（截断 80 字）。目前 hook 只传 `now_utc()`，生产不可达 | 若将来把调用方任意串透传进来，`_audit` 可能收到非预期文本；可改成只记 `sha256[:12]` |
| ⑥ | **源码（口径边界）** | **`push.rerank_weights` 里的 `NaN` 不回落、不诊断**：`json.loads` 接受 `NaN`，`float(nan)` 不抛 → 六特征加权和 = `nan` → `select` 的 `score > 0` 把**整池**滤掉 → **注入包静默变空，0 条诊断**（我实测：`{"freshness": NaN}` → 分数 `[nan,nan,nan]`、选出 0、`push_config.jsonl` 为空）。`Infinity`/`1e999` 走另一条路：分数 `inf`（仍 >0）→ 照样选出、只影响排序 | 必查的 4 类坏值都不含它；这是"S3 = 一律"的唯一反例。建议 `float()` 后加 `math.isfinite()` 复核（`_cfg_any` 已有这段，权重路径没有）。**不阻塞**：该值只能由直接 SQL 写入（`set_config` 有类型校验） |
| ⑦ | **前提（登记约定）** | `_cfg_any` 对**已登记**键**不做类型复核**：登记类型必须与交付 §9 表一致。我实测把 `breaker_seconds` 登记成 `string` 再写 `"x"` → `_cfg_any` 直接返回 `"x"` → `_breaker` 的 `float("x")` **抛 ValueError**（会穿到注入包）；把 `rerank_enabled` 登记成 `string` 再写 `"false"` → `bool("false") == True`（重排被误开） | 4 个键按 §9 的类型（`bool/int/string(JSON)/float`）登记即无此问题；建议这一条写进 §9 的登记清单（登记时逐键核对类型，不做"大概"） |

---

## 4 · 最易“假绿”的两条（本轮实证）

1. **全量绿/红取决于跑的时刻与机器负载，不取决于代码。** 本次唯一红是 `test_maintenance.M3` 的固定 `NOW` 撞上真实时钟（过 `2026-10-27T18:00Z` 必红）；`test_push_rerank` 的 R6 一族同理由墙钟定：我这次 R6a2 `min 0.869 / 中位 0.951 / max 1.027s（10/10<2s）`、R6b `0.437/0.404s`、R6d 中位 `−0.006s`（样本 `[0.004, −0.018, −0.006]`），比交付记录的 `1.226–1.519s` 更快——**同一棵树、同一批判据，读数能差 0.5s**。⇒ 引用"上一轮 1532 OK"或"本轮 1537 OK"都要带上时刻与负载，否则会把时间窗当成质量。
2. **"默认关等价"若只读仓库内判据，是同一份实现自证。** `OffEquivalenceTest` 比的是本实现 on/off 两支，**没有**和 T03b 之前的基线比；交付文档也承认 R2 的 18 组对拍"本轮未重跑"。我这轮的 52/52 是拿 `6a7f4e6` 的真 blob 加载成第二个模块、同一 conn/同一 list 对拍才得到的（含"返回同一批对象"与"零新增字段"）。⇒ 下一个人若删掉 `_with_ctx_project` 的 off 早退，仅看仓库判据仍可能绿；真正的锚是**基线 blob sha `7a87fba903cb`**。

---

## 5 · 需主理人补充（我无法从仓库取证）

1. **A 何时补 `fish.py:9405` 传 `project`/`now`**：S1 现在"推不出也安全"，但 prompt 路径仍**丢掉** `project_match` 与 24h 去重两条真信号（`_with_ctx_project` 的 docstring 已如实写明"那是 A 的必做项"）。这是能力缺口，不是错误。
2. **config 4 键的实际登记计划**：登记后我验证了坏值路径是安全的（§1-S3），但"登记后 `get_config` 的 bool 只认 `"true"`"与 `_cfg_any` 未登记期的宽容读法不同，登记时仍需复跑 off 等价。
3. **`test_maintenance.M3` 由谁修**：它在 T04b 的冻结面里，不属 T03b 写作用域；建议单独开一条小修（1 行 patch `_utcnow` 或给 `maintenance_status` 加 `now=`）。

---

## 6 · 复现清单（全部只读交付树）

```powershell
cd I:\app\DSH-Home\workspace\learn\fish-push\p0
python -m unittest test_push_rerank -v                                  # 39/39 OK
python -m unittest discover -s . -p "test_*.py"                         # 1537：1534/1/2
python mutation_push_rerank.py                                          # 9/9 CAUGHT, exit 0
python -m unittest test_push_rerank.ConfigFallbackTest -v               # S3 四条
python -m unittest test_push_rerank.PromptContextTest.test_R12d_unknown_project_mixed_pool_is_neutral -v
python %TEMP%\qa_r3_s1_probe.py                                         # S1 六组读数（含 R2 旧版对照）
python %TEMP%\qa_r3_s3_probe.py                                         # S3 未登记/已登记两态 + 诊断取证
python %TEMP%\qa_r3_s3_extra.py                                         # S3 边界：NaN 权重 / 登记类型错
python %TEMP%\qa_r3_off_equiv.py                                        # 基线对拍 52/52
python %TEMP%\qa_r3_mutation_driver.py                                  # 变异 + 旁路仓库采样
python %TEMP%\qa_r3_m3diag.py                                           # M3 时钟炸弹反证
python %TEMP%\qa_r3_final_state.py                                      # 收工态（生产树/工作树/HEAD）
```

**IS_PASS（T03b-R3 S1/S2/S3）：YES**　｜　**交付树：变异阶段零写入，唯一一次写入 = `[5/5]` 检测器自测的 1 行（<1s 内逐字节还原，我的旁路采样抓到了那一下）**　｜　**唯一红为非本批既有测试的时间炸弹（已反证）**

> 交叉核对（我在自己的读数出来**之后**才读）：另一会话 02:16 落盘的 `docs/评审报告-T03b-R3.md` 结论同为 **IS_PASS YES**（0 致命 / 0 严重 / 4🟡 / 6⚪）。其中 🟡1（`NaN` 权重既不回落也不诊断）我已独立复现，记在 §3-⑥；其"脚本仍会写仓库一次（自测）"的措辞纠正我也采纳（见上）。
