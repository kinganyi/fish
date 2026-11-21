# Test Report · B1 / B2 / B3 增量（QA 独立验证）

**读数时刻**：2026-10-26 05:11–05:45（Asia/Shanghai）
**被测树**：`I:\app\DSH-Home\workspace\learn\fish-devA`，分支 `dev/fish-devA`，HEAD **`dd33a798ef8fecb8597343a7f6d23bec28cb5c32`**
**CWD（全量测试）**：`fish-devA/p0`；**环境**：Windows / Python 3.11.9 / tiktoken 0.13.0 / 默认控制台代码页 936(cp936)
**验证方式**：不采信工程师自报。全部判据由 QA 另写脚本或另写测试在临时目录内复跑；真实 vault 只读。
**本次 QA 对仓库的唯一改动**：新增 `p0/test_qa_b1b2b3.py`（未改生产代码、未改他人测试）。工作树另有 2 个**非 QA** 未跟踪文档（`docs/安全审计-B1B2B3-v0.1.md`、`docs/评审报告-B1B2B3-v0.1.md`），属其他角色并行产出。

---

## 0. 结论与路由

| 项 | 读数 |
|---|---|
| **全量测试（基线，不含 QA 新文件）** | **Ran 672 tests / OK / 0 failure / 0 error**，228.968 s（墙钟 232.6 s），exit 0 |
| **全量测试（含 QA 新文件）** | **Ran 692 / Passed 688 / Failed 4**（failures 3 + errors 1），212.020 s，exit 1 |
| **独立检查点（QA 自写脚本）** | **85 条：80 PASS / 5 FAIL / 0 SKIP** |
| **变异自证（真跑）** | B2/B3 **12/12 按预期变红**；T05 **5/5 按预期变红**；对照组全绿。**但仅在 `PYTHONIOENCODING=utf-8` 下成立**（默认控制台不可完成，见 F-QA-6） |
| **Coverage（估算）** | **≈85%**（无 coverage 工具：`import coverage` 失败；按 205 条新测试 + 85 检查点对新增段的路径覆盖估算，**不是实测**） |
| **Routing Decision** | **Engineer（源码 Bug）** —— 3 个根因、5 条判据红；Round 1 结束，等主理人路由，QA 未改生产代码 |

**三态**：本报告 0 条 SKIP；"判不到"的三项（CLI kb 全族、并发压测、Obsidian 运行时增强）在 §8 单独声明，**不计入 PASS**。

---

## 1. 复跑读数（全部带时刻）

| # | 命令 | 读数 | 时刻 / 耗时 |
|---|---|---|---|
| 1 | `cd p0; python -m unittest discover -p "test_*.py"`（基线） | **Ran 672 / OK**，exit 0 | 05:15:39 / 228.968 s |
| 2 | 同上（加入 `test_qa_b1b2b3.py`） | **Ran 692 / failures=3 errors=1**，exit 1 | 05:40:24 / 212.020 s |
| 3 | `python mutation_b2b3_selfcheck.py`（`PYTHONIOENCODING=utf-8`） | 对照组 7 模块全绿；**12/12 变异按预期红**，exit 0 | 05:21:26 / 162.2 s |
| 4 | `python mutation_t05_selfcheck.py`（`PYTHONIOENCODING=utf-8`） | 对照组绿；**5/5 变异按预期红**，exit 0 | 05:17 / 33 s |
| 5 | `qa_b1_b2_v2.py`（B1 门面 + B2 状态表，独立脚本） | **25/25 PASS** | 05:42 / 23.2 s |
| 6 | `qa_b3_v2.py`（B3 知识库，独立脚本） | **30 PASS / 5 FAIL**（35 检查点；其中 1 条首轮为 QA 自身字段名错误，已用 `qa_kb13.py` 复判 PASS 并改判） | 05:31–05:42 |
| 7 | `qa_mcp.py`（真 stdio 子进程 + JSON-RPC） | **14/14 PASS** | 05:34 / 2.9 s |
| 8 | `qa_real_vault.py`（真实库只读） | **6/6 PASS**，总 10.8 s | 05:34 |
| 9 | `qa_budget_real.py`（预算截断 + 真实门面产物） | **3/3 PASS** | 05:43 |
| 10 | mp4/魔数拒绝抽验 | **2/2 PASS**（`.mp4`→`media`；伪装 `.md`→`media`，`magic:mp4`） | 05:44 |

**新测试文件计数（205 条，与设计"467+新增"口径一致）**：`test_b1b2b3_foundation` 23 / `test_layout` 28 / `test_board_home_root` 9 / `test_project_status` 28 / `test_open_loop_project` 7 / `test_kb_vault` 18 / `test_kb_read` 28 / `test_kb_write` 33 / `test_kb_security` 18 / `test_mcp_kb` 13。

---

## 2. 失败清单（判据 ID / 复现 / 期望 vs 实际 / 归属）

### F-QA-1 🟠 裁决 Q13「禁止 AI 写 `90-模板/`」未实现（静默偏离）

- **判据**：裁决 Q13（`docs/主理人裁决-B1B2B3.md` §二）；PRD §7.4 禁写目录（待 Q13 → 已裁决"禁止"）。
- **复现**：`cd p0; python -m unittest test_qa_b1b2b3.QAKBWriteTest.test_write_into_template_dir_is_forbidden -v`
  等价脚本：`qa_b3_v2.py` 的 `Q13-90-模板禁写` 检查点。
- **期望**：`KBError`（`ignored`/`denied` 类）。
- **实际**：`NO_ERROR` —— `90-模板/新模板.md` 被成功创建（`KB_DEFAULT_IGNORE_GLOBS` 只有 `.obsidian/**`、`.trash/**`；全仓 `p0/*.py` grep `90-模板` 0 命中）。
- **归属**：**源码**（另：`偏离记录-B1B2B3-v0.1.md` 未登记此偏离）。
- **交叉引用**：安全审计 F9（🟡，已发现，未修）；评审报告未列。

### F-QA-2 🟠 `tree` / `search` 顺着 junction 越界读取

- **判据**：PRD §7.1（"vault 内 junction/符号链接…命中即拒绝"）+ §7.5（知识库内容不得被搬运/越界）；设计 §5.1 的逃逸防线。
- **复现**：`python -m unittest test_qa_b1b2b3.QAKBPathTest.test_tree_and_search_do_not_descend_junction -v`
  夹具：`cmd /c mklink /J <vault>/jump <库外目录>`。
- **期望**：`tree` 不出现库外文件名；`search` 对库外独特词 0 命中。
- **实际**：`tree` 列出 `jump/库外秘密.md`（19 B）；`search` 命中 1、`scanned.bytes=52`（含库外 26 B）。原因：`_kb_walk`/`_kb_tree_dir` 用 `entry.is_dir(follow_symlinks=False)`，junction 在 Windows 上仍判为目录 → 向下遍历；`kb_resolve_target` 的逐级 reparse 只保护**显式路径**，不保护遍历。自造 junction 指向库外、库内都被解析路径拒绝（`outside_vault`/`reparse_point`），但遍历面绕过了这条边界。
- **归属**：**源码**（安全报告 F1 只讲忽略规则绕过、F12 只讲硬链接，**未列遍历面**；此条为 QA 新发现）。
- **风险注记**：真实库实测 0 个链接（PRD §7.1），实际触发需用户手造 junction；但会造成"同一棵树两套边界"，且 junction 环可放大扫描（我首轮夹具里形成了自指，`kb search` 扫了 764 文件 / 44 s）。

### F-QA-3 🟡 `kb open` 未校验"目标是常规文件"：空路径 / 目录泄漏 `PermissionError`，设备名可读

- **判据**：PRD KB-11（"8 类坏路径全部拒绝且错误可判"，含**空路径**）；设计 §4.2。
- **复现**：`python -m unittest test_qa_b1b2b3.QAKBPathTest.test_empty_path_open_is_domain_error test_qa_b1b2b3.QAKBPathTest.test_device_names_are_rejected -v`
  脚本：`qa_b3_v2.py` 的 `KB-11-open空路径应给域错误`。
- **期望**：`KBError`（`path_invalid` / `not_found` / `bad_usage`）。
- **实际**：
  - `kb_open(…, "")` → `PermissionError [Errno 13]`（CLI `fish kb open ""` 直接打 Traceback，exit 1）；
  - `kb_open(…, "NUL")` → **成功返回**（读到 NUL 设备，sha256=空串）；`CON`/`COM1`/`CON.md` 为 `not_found`（未触发，但同一缺口）。
  - 根因：`_kb_open_impl` 在 `read_bytes()` 前只判大小/媒体，没有 `is_dir()/is_file()` 与设备名检查；`_kb_guard` 只捕 `KBError`（也不落 `reject` 审计）。
- **归属**：**源码**。
- **交叉引用**：安全审计 F7（含 `open` 目录/空串的 `PermissionError`）；**NUL 设备可读是 QA 新增证据**。

### F-QA-4 🟡 `kb search` 文本回执只给相对路径（PRD 要求全路径）

- **判据**：PRD KB-4「结果行给**全路径**」；设计 §4.3.7「每条 `全路径:行号`」。
- **复现**：`qa_b3_v2.py` 的 `KB-4-search命中1+来源声明+全路径` 检查点（断言 `str(vault_dir) in text`）。
- **期望**：文本回执每条命中含库内全路径。
- **实际**：`- \`10-PPT/笔记A.md\`（命中 1 处）` —— 只有相对路径；`hits[i]["abs"]` 在 JSON 结构里有，但文本渲染（CLI/MCP 共用的 `text`）没输出。
- **归属**：**源码**（也可按"改文档"处理，但 PRD/设计两处都写"全路径"，按判据优先记源码）。

### F-QA-5 🟡 `tree` 列出附件文件名（PRD §7.3 要求只计数不展开）

- **判据**：PRD §7.3「`attachments/**` 不排除…`tree` 只显示"附件 N 个"不展开文件名」。
- **复现**：`qa_b3_v2.py` 的 `KB-2/§7.3-附件只计数不列名` 检查点（vault 内放 `attachments/pic.png`，断言 `pic.png` 不在 `tree` 文本里）。
- **期望**：`pic.png` 不出现，只出现"附件 1 个"计数。
- **实际**：`tree` 文本同时给出计数与文件名（`_kb_tree_lines` 对每个子项都渲染 `child['name']`）。偏离记录 11 条 B3 偏离里没有这一条。
- **归属**：**源码（口径）/文档一致性**；若判定"列名单更有用"，应改 PRD 或补登偏离记录。

### F-QA-6 🟡 变异自证脚本在默认 Windows 控制台不可复跑（工具链缺陷，影响"变异可复跑"这一交付声明）

- **复现**（不设 `PYTHONIOENCODING`）：
  - `cd p0; python mutation_b2b3_selfcheck.py` → `UnicodeEncodeError: 'gbk' codec can't encode character '\u2713'`（打印 ✓/✗ 时崩，exit 1）；
  - `python mutation_t05_selfcheck.py` → 首轮把 **M-T05-1 误判成 ✗**（父进程按 `encoding="utf-8"` 解子进程输出，子进程实际按 locale=GBK 写中文，`expect in output` 必然 False）后崩在打印 ✗。
- **期望**：默认环境下也应完成 12/12、5/5 并按预期红。
- **实际**：默认环境：B2B3 在第一条变异后即崩；T05 出现**假红**。设置 `PYTHONIOENCODING=utf-8` 后两者全部符合预期（我已据此完成判定）。
- **归属**：**源码（验证工具）**。修法：脚本入口 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`，或把 ✓/✗ 换成 `OK/RED`；子进程捕获改 `encoding=locale.getpreferredencoding(False)` 或统一给子进程注入 `PYTHONIOENCODING=utf-8`。
- **注意**：这条不否定判据本身——我逐条手工复现了 M-T05-1（3 次全红，`_attach_token` import 期抛错文案命中），说明是脚本环境耦合，不是变异无效。

### 非失败、但需记录的两项偏差

| 项 | 实测 | 判断 |
|---|---|---|
| 设计 §6.1「37 个文件/426 条测试**一字不改**」 | 实际另改了 3 个：`test_identity_p1b.py`、`test_identity_p1c.py`、`test_push_p0.py`（只加 `kb`/`kb_write` 两个工具名与字段集，`16→18`，集合/字段集仍"严格相等"） | 改动**必要且强度未降**；`偏离记录` T05-1 已登记 → 记 **文档漂移（P3）**，不记源码缺陷 |
| `p0/fish.py:8967` DeprecationWarning | `invalid escape sequence '\O'`（docstring 里的 Windows 路径没写 raw 字符串），全量测试反复打印 | **P3 清洁度**，不影响行为 |

---

## 3. 已独立验通过的关键判据（节选，均带读数）

### B1 门面（25/25 PASS）

- HOME-1：一级目录集合严格五元组；文件最大深度 2（夹具）≤3。
- HOME-2：五份 README 的四要素（`## 这是什么`/`## 能做什么`/`## 谁在写`/`不能`+`生成时刻：`+`手改会被覆盖`）逐条命中。
- HOME-3：把真实正文拷进 `20-记忆库/`（改名）→ `verify` exit 1 且 `memory_body_copy`（sha256 交集命中）；正文文件名形态亦红。
- HOME-4：同一 `--as-of` 连跑两次 → 12 个产物 sha256 **diff=0**；`rmtree(fish-home)` 后重建 → 文件清单与内容**全等**。
- HOME-5/1/2：`99-垃圾` → exit 1 + `top_dir_extra`；删 `20-记忆库/README.md` → exit 1 + `readme_missing`；干净态 → exit 0（0 error / 0 warning）。
- HOME-6：临时 `data_root` 渲染前后 `(path,size,mtime_ns)` 快照 **diff 为空**；真实库副本渲染：`index.sqlite` **字节不变**、无门面产物写入，仅 `-wal/-shm` 侧车 mtime 变动（SQLite 连接机制，**不是渲染器写盘**；判据措辞需注明"侧车除外"）。
- HOME-7/PST-1：首页链接 `../10-项目库/项目状态表.md` 可解析。
- R1：索引 **≤128 KB** + 页头常驻"上限 128 KB"声明（临时 712 B；真实库 61,106 B 亦含声明与"正文相对路径"列）。

### B2 状态表（8/8 PASS）

- PST-2 六列表头严格等于契约（顺序一致）。
- PST-3 临时库插 `FISHTEST` → 表内出现；删掉 → 消失。
- PST-4 同一 `--as-of` 两次渲染除 as-of 行外 **diff=0**；默认（不带 as-of）渲染与 now 差 **0.4 s**（<60 s）。
- PST-5 无记忆/无看板/无档期三格分别是 `—（无记忆）`/`—（今日无看板）`/`—（无开档期）`，全表无 `None`、无空单元格。
- PST-6 造"未闭合"记忆 → 未了事项 `1`；改到 31 天前 → `—`。
- PST-7 retired 不进主表；折叠区 `已退役（n）` 与 registry retired 行数相等（临时 1=1；**真实库 3=3**）。
- PST-9 `--json` 可 `json.loads`，每行含 `sources`，`columns` 前六项严格。
- 真实数据：`code_registry` active 12 / closed 1 / retired 3；`boards` 7 行全部新口径（0 行旧口径残留）；`p0/data` 下 **0** 个看板文件。

### B3 知识库（30/35 PASS，5 条见 §2）

- KB-1：同路径二次 `vault add` 行数不变；非法 `mode`→`bad_usage`；`vault remove` 不动磁盘。
- KB-2：`tree` 期间 **vault 内 `builtins.open` 次数 = 0**；双上限（宽库实测 `chars=1561 / tokens=1166 / trunc=True / reasons=['chars','tokens']`；`max_chars=200` 时 192）；回执带生效忽略规则。
- KB-3：>2 MiB→`too_large`；`.png`→`media`；PNG 改名 `.md`→`media`（魔数）；`.mp4`→`media`（扩展名）；`ftyp` 伪装 `.md`→`media:magic`；正常 md 给正文 + 三句声明（来源/未建索引/截断比例）。
- KB-11：`..`、绝对、盘符、UNC、`\\?\`、`/etc`、空（写）= 全 `path_invalid`；库外 junction→`outside_vault`、库内 junction→`reparse_point`；`.obsidian` 文件与目录都 `ignored`；`..%2f` 不解码（`not_found`）；超长路径给域错误（`not_found`）。
- KB-5/6/7/15：`create` 已存在→`exists`；`update` 缺/错 `base_sha256`→`stale_base`；`append` 后原字节是新字节前缀；每次写都有 `backup_path/meta.json`；`rollback` 后 sha256 与写前**全等**；删备份后 `rollback`→`backup_missing`；`create` 回滚=删除自建文件；写后重读 sha256 与回执一致；无 BOM、未知 frontmatter 键与注释保留、`[[双链]]` 不转义、无 `.tmp` 残留。
- KB-8：`tree/open/search/create/update/append/rollback` 各有审计行；`meta` 不含 query 原文与正文；`open` 前后 `events` 行数不变（1→1）。
- KB-10：临时库独特词 `FISH-KB-PROBE-QA261026` → `recall` **0 命中**、`kb_search` **1 命中**、`memory_fts` 行数不变、`inject` 包不含该词。
- KB-12/13/14：GBK→`encoding=gb18030` 并转码；坏编码明确拒绝；`stats` 未读 2→1、opened=1，`reads/writes` 与 `kb_access` 聚合一致；`doctor` 在 Obsidian 未运行（`available=False, reason=app_not_running`）时照常可用。
- 90-模板：**FAIL**（F-QA-1）。

### MCP 面（14/14 PASS）

- `tools/list` = **18** 且与 18 名单严格相等；每工具 `inputSchema.type=object`；**`token` 不在任何 `required`**、18 个工具均有可选 token（`_attach_token` 统一补）。
- `kb(vaults/tree/open/search)` 三行头固定（`source=知识库（未建索引）` / `as_of=` / `scan=` 或 `truncated=…shown=…`）；`depth=100` 服务端夹到 4、`limit=999` 夹到 30。
- 无 token → `kb_access.citizen_id='unattributed'`、`via='mcp:kb'`；带 token → 归属本人。
- `kb_write(create)` 成功：回执含 `write_id`、备份目录存在、审计 `via='mcp:kb_write'`；`mode=ro` 库写 → `isError=True` 且文案含 **`mode=ro`**。
- `search` 的 `meta` 只有 `query_hash/qlen/limit/scan_*`，无 query 原文。

### 真实库只读（6/6 PASS，10.8 s）

- `kb doctor`：1,872 ms，`obsidian_cli={'available': False, 'reason': 'app_not_running'}`。
- `kb tree --depth 2`：1,812 ms，`chars=1678 / tokens=1163 / truncated=True`，`files=2492 / bytes=11,497,542,860`。
- `kb search 拆解报告`：799 ms，命中 **34** 文件（显示 10），扫描 **297** 个 md / 3,193,462 B，`chars=1660 / tokens=1327`。
- `kb open 10-漫剧/拆解库/_拆解库索引.md`：7 ms，2,605 B，`truncated=false`，1,179/1,179 字。
- **没有对 `E:\Obsidian` 写入**：2,521 个文件 `(路径,字节,mtime_ns)` 快照前后 **diff=[]**；未产生任何 fish 产物。

---

## 4. 变异自证判定

| 组 | 对照组 | 变异结果 | 判定 |
|---|---|---|---|
| B2/B3（12 条） | 7 个测试模块全绿 | 12/12 按预期红（含 M-B1-1/2、M-B2-1..4、M-B3-1..6） | **成立**（仅 UTF-8 环境） |
| T05（5 条） | `test_mcp_kb` 绿 | 5/5 按预期红（含 import 期 token 手抄、分发表漏项、来源行、归属丢失、回显正文） | **成立**（仅 UTF-8 环境） |
| 环境耦合 | — | 默认 cp936 控制台：B2B3 崩、T05 假红 | **F-QA-6 记缺陷** |

我另外独立复现了 M-T05-1 三次（临时副本 + 改 schema + 跑 `test_03_constants_match`）：三次均 `rc=1` 且命中 `RuntimeError: 工具 kb 已经有 token 入参了：token 只允许由 _attach_token 统一补` —— 证明变异本身有效，问题只在脚本 IO 编码。

---

## 5. 回归与兼容审计

| 检查 | 结果 |
|---|---|
| 被改的 4 个既有测试（`test_mcp` / `test_board` / `test_board_archive` / `test_root_board`） | 改动点与设计 §6.1 的 9 处一一对应（1+6+1+1）；仍是"集合/落点严格断言"，无削弱；`test_board` 额外用 `board_out_path` + `assert startswith("10-项目库/NEWP/")` **钉住新口径**（比原来更强） |
| 设计称"一字不改"的 37 个文件 | 实际另改 3 个（工具面冻结断言），已在 `偏离记录` T05-1 登记；diff 只加 `kb/kb_write` 名字与字段集，`len(names)==18` 与逐工具字段集仍严格相等 → **无判据削弱** |
| `board_render` 调用点 | 11 处全部走 `board_render`（内部 `board_out_path`→`home_root`）或 `board_out_path`；`mcp_server` 两处仍传 `data_root`（符合 §6.2"签名不改、内部解析 home"）；无一处再拼 `root/path` 作落点 |
| `migrate()` 链尾 | `_ensure_kb_tables` → `_ensure_boards_home_paths` → `commit`，顺序符合"结构先、数据迁移后"；`boards` 7 行实测全为新口径 |
| `p0/data` 看板残留 | 0 个（`rglob("看板-*.md")` 空）；`.gitignore` 已含 `fish-home/` 与 `p0/data/projects/**/看板-*.md` |
| 文档漂移（Q16/D1/D3/D4/D5） | `docs/代码地图-基线.md` 已是 18 工具 + "以 `len(TOOLS)` 为准" + 资源 2/设计 4 的差异说明；`30 §6.2` 已把"看板未实现"行作废并留档；`docs/install-cli-说明.md` 存在（只写步骤） |

---

## 6. 新增的对抗性补测（`p0/test_qa_b1b2b3.py`，20 条）

| 类 | 用例（计数） | 结果 |
|---|---|---|
| 路径（8） | 大小写变体、尾随点/空格、`vault-evil` 假前缀（junction）、`..%2f` 不解码、超长路径、设备名 CON/NUL/COM1、空路径 open、junction 越界遍历 | **6 PASS / 2 RED**（空路径 → `PermissionError`；junction 越界） |
| 写（6） | 并发两次 update（后写被乐观锁拒）、空 content、正文自带 frontmatter、既有文件 frontmatter 无收尾、>512 KiB、`90-模板/` 禁写 | **5 PASS / 1 RED**（90-模板） |
| tree（3） | 空 vault、单文件 vault、目录符号链接（不下降 + 显式路径拒绝） | 3 PASS |
| 状态表（3） | 空 registry（占位行、无折叠区）、全 retired（主表空 + 折叠=2）、无档期无记忆（三种 `—` + 5 个 sources） | 3 PASS |

`--verbose` 复跑：`Ran 20 tests … FAILED (failures=3, errors=1)`（4 条红 = F-QA-1、F-QA-2、F-QA-3 的两条表现）。

---

## 7. 最易"假绿"的三条判据（下一轮重点盯）

1. **HOME-6（`p0/data` 零写入）**：快照若包含 SQLite 侧车，任何 CLI 命令都会让 `-wal/-shm` mtime 变；若排除侧车又要防止"把产物写进 `data_root` 的 `.md`"。我在报告里把两者分开判（内容/字节不变，侧车除外）。另：本判据只在"已迁移库"上成立——旧库首跑会被 `migrate()` 改写 `boards.path`（正常迁移，但快照会红）。
2. **KB-2（tree 不 open）**：`builtins.open` 计数只能证明"没调内置 open"，不能证明"没读盘"（`scandir/stat` 仍在）；且 `_kb_tree_dir` 的 `max_depth` 不封顶（安全审计 F6：真实库 `depth=2` 仍 248 次 `scandir`），"只看了一点点"是假象。
3. **PST-4（as-of 幂等）**：把 as-of 写死，幂等必然全绿；必须与"新鲜度 <60 s"成对断言（我两条都跑：diff=0 + gap=0.4 s）。

---

## 8. 判不到 / 未覆盖（不计 PASS）

| 项 | 原因 | 建议 |
|---|---|---|
| CLI `kb tree/open/search/write/rollback/stats/doctor` 全族 | QA 只独立跑了 `kb open ""`（缺陷复现）与 `layout/project status` 全族；其余 CLI 面仅由工程师测试覆盖 | 下一轮补 6 条 CLI smoke（exit code + 关键字段） |
| 并发写压测 | 只做顺序乐观锁（后写被拒）；未做两进程真并发 | 若 MCP 多客户端是真实场景，补 |
| Obsidian 运行中的 `available=True` 分支 | 本机 app 未运行（README 也写"当前实测就是"）；未启动 app（避免打扰） | 交给人手动两态复核 |
| 非 Windows 分支 | 本机只有 Windows | 声明为未验证 |
| Coverage 百分比 | `coverage` 模块未安装 | 若要硬指标，先装 coverage 再跑 |
| 安全面（凭证可读、忽略规则大小写绕过、frontmatter 注入…） | 已由安全审计独立覆盖（F1–F15），QA 只抽验了与本批判据直接相关的 3 条 | 见 §9 交叉引用 |

---

## 9. 与并行角色的交叉引用（不是"我发现的"）

- `docs/安全审计-B1B2B3-v0.1.md`（未提交，作者=安全官）：**F9 = 我的 F-QA-1**（90-模板）、**F7 含 F-QA-3 的 `open` 目录/空串**、F1（忽略规则大小写/尾随空格绕过 `.obsidian`）、F2（search 失败把 query 写进 `kb_access.path`）、F3（MCP 写无身份门槛）、F6（tree `max_depth` 死代码）等 15 项。
- `docs/评审报告-B1B2B3-v0.1.md`（未提交，作者=评审官，`IS_PASS: NO`）：🔴-1 与我交叉的 query 泄漏、🟠-2 tree depth 不生效、🟠-3 rollback `write_id` 后缀不校验等。
- 我的增量：**F-QA-2（遍历面 junction 越界）**、**F-QA-4（search 全路径）**、**F-QA-5（附件列名）**、**F-QA-6（变异脚本控制台编码）**，以及设备名 `NUL` 可读这一条**新证据**。

---

## 10. 需要主理人补充 / 裁决

1. **Q9 的 `rw` 与安全 F3 冲突**：裁决 Q9 要求"本机 `安逸知识库` 注册时显式 `--mode rw`"，但开发库 `kb_vault` 实测是 **`mode='ro'`**（`is_default=1`）。最终交付口径是 ro 还是 rw？若 rw，安全报告 F3（MCP 无 token 可写）需要同时拍板是否加门槛——两条是同一个决定的两面。
2. **两份未提交报告**（安全审计、评审报告）是否随本批提交？其中 F1/F3/F7/F9 等是否**本批修**——这决定我的 F-QA-1/3 是"本轮必修"还是"下批"。
3. **F-QA-6 修法**：变异脚本改 `sys.stdout.reconfigure(encoding="utf-8")` + 子进程统一编码（否则"变异自证可复跑"这条交付声明只在 UTF-8 环境下成立）。
4. **F-QA-4 / F-QA-5 走"修实现"还是"改文档"**：PRD 字面要求"全路径""附件不展开名"，实现给了相对路径/文件名；若认为实现更合理，应改 PRD 或补登 `偏离记录`。

---

## 11. 局限与诚实声明

- 本报告全部读数来自 `dd33a79` 的**工作树**（含 2 份未跟踪文档，但不影响被测代码）：我未提交、未改生产代码。
- 真实库只做只读访问；"零写入"的证据是 2,521 文件 `(路径,字节,mtime_ns)` 快照 diff 为空，**不是**文件系统审计级证明。
- 变异自证只在 `PYTHONIOENCODING=utf-8` 下跑完；默认控制台不可完成已作为缺陷记录（F-QA-6），**不影响 12/12、5/5 的判定**（我另做了 M-T05-1 三次手工复现）。
- 我的 `qa_b3_v2.py` 首轮有 1 条**QA 自身错误**（`kb_stats` 未读字段名写成 `unread`，实际是 `vaults[i].unread_notes`），已用 `qa_kb13.py` 复判 PASS 并改判；同类 QA 自身错误另有 2 条已修（`search_memories` 返回 `(rows, sources)` 二元组、备份行计数时机）。
- 覆盖率 ≈85% 是**估算**，不构成"已覆盖"的证明。

---

## 附录 · 复跑命令（QA 脚本在 `%TEMP%\qa-b1b2b3\`，不随仓库提交）

```powershell
cd I:\app\DSH-Home\workspace\learn\fish-devA\p0
python -m unittest discover -p "test_*.py"                 # 692 tests（含 QA 文件）
python -m unittest test_qa_b1b2b3 -v                       # 20 条对抗性补测
$env:PYTHONIOENCODING='utf-8'                              # 变异脚本必需（见 F-QA-6）
python mutation_b2b3_selfcheck.py                          # 12/12 红
python mutation_t05_selfcheck.py                           # 5/5 红

# QA 独立脚本（路径按本机临时目录）
python $env:TEMP\qa-b1b2b3\qa_b1_b2_v2.py <p0>             # 25/25
python $env:TEMP\qa-b1b2b3\qa_b3_v2.py <p0>                # 30 PASS / 5 FAIL
python $env:TEMP\qa-b1b2b3\qa_mcp.py <p0>                  # 14/14
python $env:TEMP\qa-b1b2b3\qa_real_vault.py <p0>           # 6/6
```

---

# 第二轮 · 收口复验（最终门）

**读数时刻**：2026-10-26 07:33–07:56（Asia/Shanghai）
**被测树**：`I:\app\DSH-Home\workspace\learn\fish-devA`，分支 `dev/fish-devA`，HEAD **`b2e41b9701b73c04848320d65ed3371c998e35ba`**（`git status --porcelain` 空）
**环境**：Windows / Python 3.11.9 / 默认控制台 cp936 / `coverage` 模块仍未安装（百分比仍是估算）
**方法**：逐条复验、不接受转述。86 条独立检查点全部由 QA 另写脚本在 `tempfile.mkdtemp()` 内复跑；真实 vault `E:\Obsidian\安逸知识库` **只读**；只在副本上跑门面渲染。

## 0. 结论与路由

| 项 | 读数 |
|---|---|
| **Total / Passed / Failed** | **88 / 87 / 1**（86 条独立检查点 + D1 全量 1 条 + D2 变异脚本 1 条） |
| **全量测试（D1）** | **Ran 736 tests in 236.408 s / OK / failures=0 / errors=0** |
| **变异自证（D2）** | T05 **6/6 按预期红**；B2B3 **16/17**（M-R3 未按脚本期望文案红，见 K1）；对照组 8 模块全绿 |
| **Coverage（估算）** | **≈87%**（无 coverage 模块，按新增/修改段路径估算，**不是实测**） |
| **Routing Decision** | **Known Issues（产品可交付；1 条 P2 工具链遗留，非产品缺陷）** |

## 1. 逐条复验（A / B / C / D / E）

图例：**绿** = 与期望一致；**红** = 不一致；**跳过** = 环境不支持（本轮 0 条跳过）。

| # | 判定 | 证据（命令 → 实际 vs 期望） |
|---|---|---|
| **A1** | **绿** | `qa2_verify.py <p0> a`：`90-模板/新模板.md` create → `ignored`、**未落盘**、reject 行 `reason_code=ignored`；既有 `90-模板/模板一.md` update → `ignored` 且 **sha256 不变**；`90-模板/子目录/深一层.md` create → `ignored` 且目录未被创建。期望=拒写且可解释 ✔ |
| **A2** | **绿** | 自造 `mklink /J <vault>\jump <库外目录>`（库外放 `库外秘密.md` + 独特词 `QA-R2-OUTSIDE-7788`）：`tree(depth=3)` 文本 **0 次**出现 `jump`/`库外秘密`；`search` 该词 **total=0**、`scan.files=2`（=在库 md 4 − `90-模板` 内置忽略 1 − 敏感 1；若越界应为 3）；显式 `jump/库外秘密.md` → `outside_vault`（域错误码，旧版读到 19 B） |
| **A3** | **绿** | 6 条坏输入：空路径→`bad_usage`、目录→`bad_usage`、`NUL`/`CON`→`path_invalid`、`笔记.md:ads`→`path_invalid`、含 `\x00`→`path_invalid`；**每条都新增 1 行 `action=reject`**（旧版 0 行）。CLI 三条（空路径/`NUL`/目录）rc=2/1/2，stderr 只有 `错误[<code>]：…`，**0 次 Traceback** |
| **A4** | **绿** | `search` 文本：``- `C:\...\<vault>\10-PPT\笔记A.md`（库内 `10-PPT/笔记A.md`，命中 1 处）`` —— 库内全路径 ✔；`tree(depth=2)` 中 `pic-图片.png` **0 次**出现，只有 `- attachments/  0 篇笔记 / 1 附件 / 40 B` |
| **B1** | **绿** | 离线 + `limit="abc"` 两次失败：`kb_access.path` 均为 `''`、`meta` 仅 `query_hash`（两探针词 **0 明文子串**）、`reason_code=offline/bad_usage`；`kb stats` 整份 JSON 里探针词 0 次 |
| **B2** | **绿** | GB18030 文件 append / update → **`bad_encoding` ×2**，**sha256 前后相等**（字节未动）；两行 reject 的 `reason_code` 均是 `bad_encoding` |
| **B3** | **绿** | 仅 `A/a.md`、`A/B/b.md`、`A/B/C/c.md` 的库：depth1 无 `A/B`、有 `A/a.md`、`files=1`；depth2 有 `A/B`+`A/B/b.md`、无 `A/B/C`、`files=2`；depth3 有 `A/B/C`+`A/B/C/c.md`、`files=3`；`chars` 519→601→690 **单调**、三次 `truncated=false` |
| **B4** | **绿** | `1-beefcafe`（非真后缀）→ `not_found` 且 `新建.md` **仍在**；append 后错后缀同样 `not_found`、字节不变；**真 write_id 回滚 → `removed=true`**（对照组，证明不是"永远拒绝"） |
| **C1** | **绿** | `.obsidian/`、`.OBSIDIAN/`、`.Obsidian/`、`.obsidian /`（尾空格）、`.TRASH/` **五写法全 `ignored`**；写 `.OBSIDIAN/坏插件.json` → `ignored` 且磁盘无文件；两目录里文件的独特词 search 均 **0 命中**；MCP 通道读=`isError`+“按约定忽略”、写=`isError` 且未落盘 |
| **C2** | **绿** | 库层同 B1；MCP 通道：库离线后 `kb search` → `isError`，reject 行 `path=''`、`meta` 只有 `query_hash`（探针词 0 泄漏） |
| **C3** | **绿** | MCP `kb_write` 无 token → `isError=true`（"写知识库需要会话凭证"）、文件未创建；带 token → 成功，`kb_access=(citizen_id=01QAR2…, account_id=**self:qa2-mcp**, via=mcp:kb_write)` |
| **C4** | **绿** | `凭证管理.md` → `sensitive`（文案含文件名+大小，**0 正文泄漏**）；`allow_sensitive=true` / `--allow-sensitive` 才出正文且带"显式授权"声明；search `QA-R2-SECRET-9911` **0 命中**、`skipped_sensitive=1`；`server.pem`、`.env.local` 也 `sensitive`；真实库 `80-资料库/凭证管理_v1.0.md`(6970 B) → CLI rc=1 `错误[sensitive]` |
| **C5** | **绿** | 2,200,213 B 既有 md `append` → **`too_large`** 且字节不变；另用 4,400,107 B（安全 P7 原尺寸）：读 `too_large` + 写 `too_large`、字节不变 |
| **C6** | **绿** | 6 层库 `scandir` 计数：depth1/2/3/4 = **2/3/4/5**（旧版 depth=1 是 **14**）；每次 `hidden_dirs=1`、`partial` 如实 |
| **C7** | **绿** | frontmatter 值含 `\n` / `\r` / `\n---\n` 三种 → 均 **`bad_frontmatter`** 且文件未创建 |
| **C8** | **绿** | `--home` 指 `C:\`、`%WINDIR%`、`%ProgramFiles%`、`%USERPROFILE%` 全 **`RuntimeError`**；外部目录不带开关 → `RuntimeError`（文案点 `allow-external-home`），带开关通过；CLI `layout render --all --home <外部>` rc=1 且 **0 文件落盘** |
| **D1** | **绿** | `cd p0; python -m unittest discover -p "test_*.py"` → **Ran 736 / OK**，236.408 s（07:35→07:39）。注：pwsh 的 `[exit code: 1]` 是 PowerShell 把 unittest 进度点当 `NativeCommandError` 的假红 |
| **D2** | **红（部分）** | `mutation_t05_selfcheck.py`（**不设 `PYTHONIOENCODING`**）→ 对照绿、**6/6 按预期红**（含 M-R11）。`mutation_b2b3_selfcheck.py`（同样默认控制台）→ 对照 8 模块全绿、**16/17**；**M-R3 ✗** 详见 K1 —— **"17/17" 的交付声明在 HEAD 不成立** |
| **D3** | **绿** | 12 条报告复现命令重跑：V1（depth 三次互异 + 错后缀被拒）、V2、V3、V4（产物 sha 两次全等 + 热态 stdout 全等 + verify 0/0 + `status --json` 可解析）、P1（五写法）、P6（scandir=2）、P7（4.4 MB 读写皆 too_large）、P8（`9-0000` 被拒）、P14、P16（三种越界 `RuntimeError`）+ R16（缺 `data_root` → `TypeError`）→ **12/12** 与报告描述一致 |
| **E1** | **绿** | 真实库只读：`doctor` rc=0；`tree --depth 2` rc=0；`search 拆解报告` rc=0（`scan=296 files / 3,186,492 B / 763 ms｜命中 34`）；`open 10-漫剧/拆解库/_拆解库索引.md` rc=0（2,605 B / 1,179 字）；`open 80-资料库/凭证管理_v1.0.md` rc=1 **`sensitive`**。**2521 个文件 `(路径,字节,mtime_ns)` 前后 diff=[]**；全部读发生在临时 data root 的登记里 |
| **E2** | **绿** | `recall` 唯一读入口 `search_memories`：临时库独特探针 **0 命中**、真实库词「拆解库索引」**0 命中**；两次 `kb search` 前后 `memory_fts` 行数 **0→0** |
| **E3** | **绿** | `p0/data` 经 sqlite backup 复制成"已初始化库"→ 副本上 `board render --all` + `layout render --all`：**data_root 快照 1 文件 diff=[]**（N4 口径）；`layout verify` **0 条 error / 0 条 warning**；真实 `p0/data/index.sqlite` sha256 前后同为 `37c0c40a32aa…`（本轮零改动自证） |
| **E4** | **绿** | 无 token MCP：`kb tree` / `recall` 均 `isError=false`（读面不要求凭证） |

## 2. 上一轮 4 条源码红：闭合情况

| 上一轮 | 判据 | 本轮读数 | 闭合 |
|---|---|---|---|
| F-QA-1 `90-模板/` 可被写 | A1（Q13） | create/update/深层 create 三条全 `ignored`、零落盘、字节不变 | **✔ 闭合** |
| F-QA-2 tree/search 下钻 junction | A2（PRD §7.1） | tree 不列、search 0 命中、扫描面不越界、显式路径仍拒 | **✔ 闭合** |
| F-QA-3 空路径/目录/设备名裸异常 | A3（KB-11） | 6 条域错误码 + 每条 1 行 reject；CLI 0 次 Traceback | **✔ 闭合** |
| F-QA-4 search 只给相对路径 | A4（KB-4） | 文本每条含库内全路径 | **✔ 闭合** |
| F-QA-5 tree 列附件名 | A4（§7.3） | 只计数不列名 | **✔ 闭合** |
| F-QA-6 变异脚本 cp936 崩/假红（上轮"其它"） | D2（R9） | 默认控制台无 `PYTHONIOENCODING` 下两个脚本都跑完、无 `UnicodeEncodeError`；子进程显式注入 UTF-8 | **✔ 闭合** |

另：我上一轮写的 `p0/test_qa_b1b2b3.py`（20 条，当时 4 红）现在 **20/20 OK**（`python -m unittest test_qa_b1b2b3 -v`，3.09 s），独立佐证同一条结论。

## 3. Known Issues（不计 PASS，不阻断交付）

| # | 级别 | 问题 | 证据 | 建议 |
|---|---|---|---|---|
| K1 | **P2（工具链/声称面）** | `mutation_b2b3_selfcheck.py` 的 **M-R3 期望文案错位**：它期望 `depth=1 不该出现二级目录`（显示层断言），但该变异改的是**统计层** `if level >= max_depth`，实际红在 `depth=1 的 scandir 次数必须受控`（8 > 6）→ 脚本判"不符合预期"、exit 1，"17/17" 不成立（实际 16/17）。`docs/变异自证` §6 把 M-R3 标成 ✓ 亦不准确 | 脚本输出 `✗ M-R3 …`；手工复核：① 原样变异 → rc=1（红在 scandir 断言）；② **只摘显示层 → rc=0**（与 §6 自己的说明一致：必须同时摘两层）；③ §6 的 M-R4（两层同摘）→ `KBDepthSemanticsTest` 2 条红；④ M-R4b → 7 条红。**判据本身有效，产品码无缺陷** | 2 行改：`expect` 改成 `scandir 次数必须受控`，或把 M-R3 改成"统计层+显示层同摘"（即 §6 的 M-R4 入脚本） |
| K2 | P3（观察） | 显示层 depth 闸（`_kb_tree_lines.emit` 的 `if level >= max_level`）目前是**冗余防御**：只摘它判据全绿（统计层已把深层裁掉）。这是"统计/显示分离"设计的代价，不是缺陷；但"显示层独立守 depth"没有单点判据 | 手工复核变体② rc=0 | 若将来统计扩到全树，需补一条"统计全量 + 显示裁剪"的判据 |
| K3 | P3（已登记 N3） | 错误文案 / `meta.reason` 仍带 vault 绝对路径（如 `知识库不在线：C:\...`） | B1/V3 的 reject 行可见 | 裁决 N3 已登记"本轮不修"，仅提醒 |

## 4. 仍未覆盖（不计 PASS）

| 项 | 原因 |
|---|---|
| 两进程**真并发**写 | 只做了顺序乐观锁（`stale_base`）与单进程写；未起两个进程抢同一文件 |
| Obsidian **运行中** `available=True` 分支 | 本机 app 未运行；未为验收启动 GUI（避免打扰） |
| 非 Windows 分支 | 本机只有 Windows（`mklink`、`normcase`、reparse 位均 Windows 语义） |
| **Coverage 硬指标** | `coverage` 模块未安装；≈87% 是估算 |
| 硬链接穿越（安全 F12 / 裁决 N1） | 登记不修；本轮**未重跑** P15（上轮安全报告有证据） |
| MCP 传输层超大单行 JSON | 既有问题、非本批引入；本轮未压测 |
| HOME-6 在**真实** `p0/data` 原地跑 CLI | 按 N4 改在 sqlite backup 副本上跑（已初始化库为起点）；真实 `p0/data` 只做 sha 零改动自证 |

## 5. 本轮新增 / 保留的测试

- **保留**：`p0/test_qa_b1b2b3.py`（20 条，上一轮 QA 对抗性补测；**它是 4 条源码红的第一见证**，现在全绿，已随 054f1f1 入仓）。本轮它在 736 全量里。
- **新增（不随仓库提交，沿用上一轮 `%TEMP%` 约定）**：
  | 脚本 | 检查点 | 覆盖 |
  |---|---|---|
  | `qa2_verify.py <p0> a` | 17 | A1–A4 |
  | `qa2_verify.py <p0> b` | 12 | B1–B4 |
  | `qa2_verify.py <p0> c` | 23 | C1/C4/C5/C6/C7/C8 |
  | `qa2_mcp.py <p0>` | 11 | C1(MCP)/C2/C3/C4(MCP)/E4 + tools/list=18 |
  | `qa2_d3.py <p0>` | 12 | 报告复现（V1–V4、P1/P6/P7/P8/P14/P16、R16） |
  | `qa2_real.py <p0> [vault]` | 11 | E1–E3 + `p0/data` 零改动自证 |
  | `qa2_mut_depth.py` / `qa2_mut_r4.py` | 手工复核 | K1/K2：M-R3 与 §6 的 M-R4/M-R4b |
- **合计**：86 条独立检查点（87 pass 含 D1）+ 1 条变异脚本判定（红）。

## 6. 附录 · 第二轮复跑命令

```powershell
cd I:\app\DSH-Home\workspace\learn\fish-devA\p0
$env:PYTHONIOENCODING='utf-8'      # 我的脚本要打印中文；变异脚本**不需要**（R9 已修）

# A/B/C/D3/E（独立判据，全部 tempfile 隔离）
python $env:TEMP\qa2-b1b2b3\qa2_verify.py <p0> a   # 17/17
python $env:TEMP\qa2-b1b2b3\qa2_verify.py <p0> b   # 12/12
python $env:TEMP\qa2-b1b2b3\qa2_verify.py <p0> c   # 23/23
python $env:TEMP\qa2-b1b2b3\qa2_mcp.py    <p0>     # 11/11
python $env:TEMP\qa2-b1b2b3\qa2_d3.py     <p0>     # 12/12
python $env:TEMP\qa2-b1b2b3\qa2_real.py   <p0>     # 11/11（真实库只读）

# D1 / D2
python -m unittest discover -p "test_*.py"         # Ran 736 / OK（236 s）
python mutation_b2b3_selfcheck.py                  # 16/17（K1），exit 1
python mutation_t05_selfcheck.py                   # 6/6，exit 0
```

## 7. 诚实声明

- 本轮我**只改了一份文件**：本报告（追加第二章节）。未改生产代码、未改任何测试、未提交 git。
- 真实 vault 只读：证据是 2521 个文件的 `(路径, 字节, mtime_ns)` 快照 diff=[]，**不是**文件系统审计级证明。
- 我在 `%TEMP%\qa2-b1b2b3\` 下另写 6 个脚本（见 §5），未入仓——若主理人要把它们变成常设判据，我建议入仓为 `p0/test_qa_round2_verify.py`（需工程师同提交跑一次全量，届时测试数会 >736）。
