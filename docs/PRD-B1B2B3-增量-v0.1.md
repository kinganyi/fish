# 增量 PRD · B1 / B2 / B3

> 产品经理：许清楚｜日期：2026-10-26｜版本：v0.1
> 基线：开发副本 `I:\app\DSH-Home\workspace\learn\fish-devA`，分支 `dev/fish-devA` @ `b23a63f`（`git status --porcelain` 干净）
> 范围：**只写文档，不改代码**。产出落点：`docs/PRD-B1B2B3-增量-v0.1.md`
> 本批三块：**B1 fish-home 目录门面** / **B2 项目状态表** / **B3 知识库协议 + MCP 工具**

---

## 0. 一页速览

| 项 | 值 |
|---|---|
| 需求总数 | **40 条**（P0 26 / P1 10 / P2 4） |
| 需求编号前缀 | `HOME-*`（门面）/ `PST-*`（状态表）/ `KB-*`（知识库）/ `X-*`（横向） |
| 冲突 | **5 处**（C1–C5，每处给 2–3 个落法 + 推荐，见 §8） |
| 待确认 | **18 条**（5 组，按"阻塞什么"分组，见 §10） |
| 新增表 | 2 张（`kb_vault` / `kb_access`）+ 1 视图（`kb_path_stats`）+ 1 处语义变更（`boards.path`） |
| 新增 MCP 工具 | **2 个**（`kb` 读 / `kb_write` 写）；工具面 16 → 18 |
| 新增 CLI 子命令族 | 2 族（`layout` / `kb`）+ 1 个子命令（`project status`） |
| 本批不写界面 | 门面是**文件**，本批无 UI（UI 属 09 Q20，另议） |

**三个关键取舍（结论先行，理由见正文）**

1. **知识库不建索引**：`search` 用"按需扫描 .md"实现，不建任何索引表。实测扫 297 个 md（3.05 MB）耗时 **130 ms**、全库 walk+stat 2492 个文件 **586 ms**（缓存热）→ 不值得为它引入第二套索引与一致性风险。
2. **知识库上 MCP，但只上 2 个工具**：读一个（`kb`，多 action）、写一个（`kb_write`，独立安全契约）。这与 `16 §6.3`"kb 只走 CLI"的裁决不同，属于用户新拍板带来的改判（冲突 C2）。
3. **AI 可写但删/移/改名一律不做**：AI 能"新建 / 追加 / 显式更新（带乐观锁）"，写前必备份、可回滚；删除、移动、重命名交给 Obsidian 自己（它会自动改双链，fish 重实现风险高收益低）。

---

## 1. 背景与目标

### 1.1 系统现状（取证，不是转述）

> 取证方式：读代码与文档 + 只读查库（`file:...?mode=ro`）+ 只读扫 vault。**未跑测试套件**（避免污染副本库），测试数用静态计数。

| 面 | 现状 | 证据 |
|---|---|---|
| 代码 | `p0/fish.py` **8,972 行**（238 个顶层符号）；`p0/mcp_server.py` **1,520 行** | 实测读文件；`docs/代码地图-基线.md` L6 |
| MCP 面 | **16 个工具 + 2 个资源**（`fish://me`、`fish://index`） | 实测 `TOOLS` L131–436、`RESOURCES` L438 |
| 工具入参纪律 | `token` 由 `_attach_token()` 统一补；**工具若自带 `token` 字段会直接 raise** | `mcp_server.py` L115–129 |
| 测试 | **41 个 `test_*.py`，467 个 `def test_*`** | 实测静态计数（`12-开发基线` 记 41 个；`30 §3.4` 的 463 是旧读数，最近记忆 `大肥鱼生命系统-261026-01` 记"全量 467 绿"） |
| 记忆数据 | events **614**；`code_index` **246**（active 207 / superseded 33 / withdrawn 6）；正文 **420** 篇；公民 **44** | 实测 as-of 2026-10-26T01:4x |
| 项目前缀 | `code_registry` **16** 行（active **12** / closed **1** / retired **3**），其中 **9 个是中文前缀**（active 7：`企业智能`/`常用会话`/`言纹`/`生活`/`工作`/`未分类`/`大肥鱼生命系统`；retired 2：`测试项目`/`中文测`） | 实测 `SELECT prefix,status`；`PRAGMA table_info` 确认列 |
| 看板 | `boards` **7** / `presence` **11** / `board_posts` **17**；已实现项目级、根级聚合与归档 | 实测；`board_render_root` L8312 |
| 看板落点 | 渲染写进 **`data_root`**：`Path(root)/board["path"]` | `fish.py` L8370；`p0/data/看板-261026.md` 实存 |
| 档期 | `project_stints` **17** 条全是开档期（`to_ts IS NULL`），指向 16 个公民，其中 **11 条指向"未分类"** | 实测 |
| 门面 | **零实现**：`kb` / `layout` / `项目状态表` / `fish-home` 在 `p0/*.py` 中 grep **0 匹配**；`.gitignore` 已预留 `fish-home/` | 实测 grep；`.gitignore` L22 |
| 知识库 | `E:\Obsidian\安逸知识库`：**2521 文件 / 252 目录 / 10.96 GB**；**318** 个 `.md`，其中 **285** 有 frontmatter、**302** 有 wikilink（共 **928** 条）；顶层 **11** 项 | 实测只读扫描 |
| 知识库风险面 | **315** 个文件 >10 MB、**10** 个 >100 MB（最大 641.2 MB 的 mp4）；**295** 个 md 里 **2** 组同名（`拆解报告2.md`、`拆解报告5.md` 各 2 处） | 实测 |
| 知识库扫描成本 | 排除 `.obsidian`/`.trash` 后 2492 文件；walk+stat **586 ms**；读全部 297 md（3.05 MB）并做关键词匹配 **130 ms** | 实测（缓存热，冷启未测） |
| Obsidian | 安装 **1.14.5**；`Obsidian.com` 在 PATH（`I:\app\Obsidian\Obsidian.com`）；**当前未运行**（无 `Obsidian` 进程） | 实测文件版本 + 进程查询 |

**三条与文档不一致的实测发现（先声明，避免后面对不上）**

1. `docs/代码地图-基线.md` L255 列了 **17** 个工具且含一个 `fish` 工具；**实测只有 16 个，没有 `fish`** → 文档漂移（见 Q16）。
2. `09 §4.4` 记 vault 为 **2520 文件 / 251 目录**；实测 **2521 / 252**（`.trash` 内文件变动所致）→ 数字以实测为准，文档不抄副本（X-2）。
3. `30 §6.2` 把"看板（三级 + 签到 + 容量切分）"列为 **未实现**；实测看板主体已实现（`boards`/`presence`/`board_posts` 三表 + 渲染 + 根板聚合 + 归档，**7 个测试文件共 71 条**）→ 该行**已过期**。

### 1.2 三个产品目标（正交，各自可判）

| 目标 | 一句话 | 判据形态 |
|---|---|---|
| **G1 · 看得懂** | 陌生 AI/人只打开 `fish-home`、不看任何文档，能说出三库各是什么、正文在哪 | 人工判 + 目录深度与 README 四要素机器判（§4.1） |
| **G2 · 有入口** | 项目状态一屏到底：谁在动、卡在哪、下一步去哪；每个值可追溯到表，且带 as-of | 机器判（六列 + as-of + 可重建 + 假前缀必出现） |
| **G3 · 能激活** | 知识库可浏览、可打开、可搜、可写；且**绝不污染** fish 记忆面 | 机器判（独特词不进 `recall`；写入 100% 有备份） |

**为什么这三条正交**：G1 是"空间"（人在哪儿找东西），G2 是"时间/状态"（现在发生了什么），G3 是"内容"（过去积累的东西能不能被用）。任一条达标不蕴含另一条——门面做全但知识库不可读，G3 仍然红。

### 1.3 用户已拍板（本批不可改的输入）

| # | 拍板 | 对本 PRD 的强制约束 |
|---|---|---|
| 1 | 门面根名 **`fish-home`**（不是 `home`） | 全批统一用 `fish-home`；与 DSH 家目录区分 |
| 2 | **允许中文前缀**，允许新建中文前缀 | B2 的项目名直取 `code_registry.prefix/namespace`，不做 ASCII 清洗；目录名可用中文 |
| 3 | **知识库要上 MCP 工具** | `kb` / `kb_write` 两个工具（KB-9）；覆盖 C2 |
| 4 | **允许 AI 调整知识库**，以 Obsidian 兼容形式 | 写三档 + 备份 + 回滚 + 不删不搬（KB-5/6/7） |
| 5 | 用户可指定**任意本地文件夹**为知识库（多 vault） | `kb_vault` 注册表（KB-1）；不写死 `E:\Obsidian` |
| 6 | 默认：vault 原地不动；`kb tree` 默认 **2 层**（可配）；状态表放 `10-项目库/项目状态表.md`、`00-首页` 放链接；`p0/data` 不动、门面是**投影**（可删可重建） | HOME-1/4/7、PST-1、KB-2 |

### 1.4 本批与既有文档的关系

| 上承 | 本批怎么接 |
|---|---|
| `09 §4.4` R3-1~R3-5（知识库） | R3-1（不进 FTS/前 N 层）**保留为硬边界**；R3-2/3 落为 KB-2/3；R3-4（Obsidian 最小集）扩成 KB-6；**R3-5 从 P2 `could` 升为 P0**（用户拍板 4） |
| `09 §4.5` R4-1~R4-5（目录与状态表） | R4-1/R4-3 → HOME-1/2；R4-2 → PST-1~5；R4-4/R4-5 **不在本批**（手改兜底、迁移） |
| `09 §4.7` R6-5（知识库目录服务） | → KB-2（tree）+ KB-3（open） |
| `09 §8.2` 阶段 B 判据 B1~B4 | B1/B2/B3 三条本批覆盖并加严（每条带变异自证）；B4（迁移回滚）不做 |
| `10 §7.1~7.4`（data_root / memory_root 分离、三库口径、ignore_globs） | 全盘采用：真相根 `p0/data`、门面根 `fish-home`；`ignore_globs` 默认 `[".obsidian/**",".trash/**"]`，`attachments/**` 只统计不展开 |
| `16 §4.2`（知识库：扫描 + 一张访问表） | 采用"扫描 + 一张访问表"的骨；**加 `kb_vault` 表**（多 vault 是新拍板） |
| `16 §6.3`（kb 只走 CLI） | **改判**（用户拍板 3）→ 见 C2 |
| `16 §7`（目录结构） | 采用；看板落点需迁移（C3） |
| `30 §6.2`（未实现清单） | 知识库/门面从 P2 提前到本批（用户 2026-10-26 指令）；看板行已过期 |

> **口径提醒**：`30 §0.3` 的用户拍板是"先造催化剂（看板+推送）"，本批把 B1–B3 提前。这不是推翻旧记录，是**新指令**；两条记录并存，实现顺序以最新指令为准。

---

## 2. 用户故事（5 条）

| # | 角色 | 我想要 | 以便 | 对应需求 |
|---|---|---|---|---|
| US-1 | 新来的 AI | 一打开 `fish-home` 就知道记忆正文在哪、知识库在哪、项目在哪、谁在写，且每层只有两三层 | 我不用问人就能开始干活，也不会把"索引"当成"正文" | HOME-1/2/3 |
| US-2 | 人（安逸） | 一眼看到所有项目：状态、最近什么时候动过、看板在哪、谁在管、还差什么没闭合 | 我每天不用翻十个文件夹判断该做什么 | PST-1~5 |
| US-3 | AI | 在知识库里按目录浏览（只给两层、带每目录的笔记数与最近修改）、按需打开某篇、按关键词搜 | 人类积累了十年但从来不看的 318 篇笔记能真的被用上，而不是躺在 10.96 GB 里 | KB-2/3/4 |
| US-4 | AI | 在知识库里新建或追加一篇笔记，知道随时能撤回，也不会因为我的操作弄断别人笔记里的双链 | 我敢写，但不至于毁掉人的东西 | KB-5/6/7 |
| US-5 | 人（安逸） | 看到"哪些笔记从来没被读过"、"谁读过" | 我能判断知识库还有没有活性，而不是继续往里堆 | KB-8/13 |

---

## 3. 需求池

> 级别用 must / should / could；P0 = 本批不做完不算交付；P1 = 本批做，可小幅顺延；P2 = 只登记，不在本批。

### 3.1 B1 · fish-home 目录门面（7 P0 / 2 P1 / 1 P2）

| ID | 需求 | 级别 | 来源/理由 | 依赖 |
|---|---|---|---|---|
| **HOME-1** | **门面根固定为项目根下 `fish-home/`**（与 `p0/` 同级），含五个一级目录：`00-首页` / `10-项目库` / `20-记忆库` / `30-知识库` / `90-系统`；**任何文件深度 ≤3**；不允许多余一级目录 | P0 must | 用户拍板 1/6；`09 R4-1`；位置取 `.gitignore` L22 的根相对口径 | — |
| **HOME-2** | **每个一级目录必须有 `README.md`**，含四要素：① 一句人话（这库是什么）② 能做什么（能对它做什么、不能做什么）③ **谁在写**（生成器 / 人 / fish 代码）④ 生成时刻 + "手改会被覆盖"声明 | P0 must | `09 R4-3`；G1 判据"只看目录能说出三库是什么" | HOME-1 |
| **HOME-3** | **`20-记忆库` 只有索引与最近列表，禁止正文副本**：`索引-<YYYY-MM>.md`（每条一行：编码 · 标题 · 归属 · 状态 · 正文相对路径）、`最近.md`（最近 30 条） | P0 must | `10 §7.2`（正文唯一副本在 `data_root`）；用户拍板 6 | HOME-1 |
| **HOME-4** | **`fish layout render --all` 一键重建**：删掉整个 `fish-home/` 后一条命令重建；同一 as-of 下连跑两次，**除 as-of 行外字节全等** | P0 must | 用户拍板 6（可删可重建）；`10 §7.3` | HOME-1/2/3 |
| **HOME-5** | **`fish layout verify`**：检查缺失 / 多余 / 超深（>3）/ 越界（不属于五目录）/ 首页链接失效，**有问题 exit≠0 并列清单** | P0 must | `10 §7.3`（`verify --module layout` 的设计，落到独立子命令） | HOME-1~4 |
| **HOME-6** | **渲染只写 `fish-home/**`**：渲染前后对 `p0/data` 做文件清单 + mtime 快照比对，必须零变更 | P0 must | `10 §7.1`（门面 ≠ 真相）；防"顺手把投影写回真相" | HOME-4 |
| **HOME-7** | `00-首页/README.md` 必须含指向 `10-项目库/项目状态表.md` 的**相对链接**，且目标文件存在 | P0 must | 用户拍板 6 | PST-1 |
| **HOME-8** | `90-系统/` 下 `IDENTITY.md` / `CAPABILITIES.md` / `渲染清单.md` 可被 AI 读，但**不得含任何凭证形态**（`fsh_` 前缀、`token=`、私钥头） | P1 should | `10 §7.2` 的 A9 判据；凭证纪律（铁律 15） | HOME-4 |
| **HOME-9** | 数据源不可用时**明说**：某张表不存在/查询异常 → 渲染出"数据源不可用：<原因>"段落，**不得渲染空白文件或静默 0** | P1 should | `30 §6.3`（注入包无 as-of 等"静默失真"教训）；防假绿 | HOME-4 |
| **HOME-10** | `--only <dir>` 增量渲染（当前全量一次约 1s 级，非必需） | P2 could | 推导 | HOME-4 |

### 3.2 B2 · 项目状态表（5 P0 / 3 P1 / 1 P2）

| ID | 需求 | 级别 | 来源/理由 | 依赖 |
|---|---|---|---|---|
| **PST-1** | 文件固定 `10-项目库/项目状态表.md`；`00-首页` 有链接（同 HOME-7） | P0 must | 用户拍板 6 | HOME-1 |
| **PST-2** | **六列固定且顺序固定**：`项目 / 状态 / 最近活跃 / 看板入口 / 主责 AI / 未了事项` | P0 must | `09 R4-2` 原文六列 | PST-1 |
| **PST-3** | **数据只读来源，禁止手写项目清单**：`code_registry`（项目集合与状态）+ `code_index`（最近活跃/未了事项）+ `boards`/`presence`（看板入口/在场）+ `project_stints`（主责 AI）+ `_open_loop_rows()`（未了事项） | P0 must | `09 R4-2` + 用户 2026-10-26 指令；判据见 §4.2（插假前缀必出现） | 以上表存在 |
| **PST-4** | 表头必须有 **as-of 时刻**（ISO8601，含时区）与生成器标识；同一 as-of 下重渲染**除 as-of 行外全等**（可重建） | P0 must | `30 §6.3`（无 as-of 是已知缺陷，本表自建 as-of） | PST-2 |
| **PST-5** | **缺数据要显式，不许编、不许空白**：无记忆 → `—（无记忆）`；无开档期 → `—（无开档期）`；今日无看板 → `—（今日无看板）`；`未了事项` 为 0 → `—`。表尾脚注写清每个 `—` 的含义与判定口径 | P0 must | `09` 纪律"不猜不填空"；`EVAL` 系列教训（把未知说成事实） | PST-2/3 |
| **PST-6** | **未了事项口径**：沿用 A 类未闭合定义（标题命中 8 个开放词 + **近 30 天**窗口），**按项目过滤**；现有 `_open_loop_rows()` 无 `project` 参数，需加可选参数（默认 `None` 保持旧行为） | P1 should | `fish.py` L5414（签名）、L5301（30 天）、L5306（8 词） | PST-3 |
| **PST-7** | 状态映射：`active→进行中` / `closed→已闭合` / `retired→已退役`；**`retired` 默认不进主表**，收进文末"已退役（N）"折叠区 | P1 should | 实测 12 active / 1 closed / 3 retired；中文前缀混排 | PST-3 |
| **PST-8** | `主责 AI` 取值口径（当前开档期公民 / 最近记忆 owner / 人工维护）待 Q4 定，未定前按"开档期优先、缺则 `—（无开档期）`"实现 | P1 should | 实测 `code_registry` **无 owner 列**（PRAGMA 实读）；17 条开档期指向 16 个公民 | Q4 |
| **PST-9** | `fish project status --json`：同一数据的机器可读形态（含每行取值来源），供后续门面/推送复用 | P2 could | 推导（避免别的模块再写一份 SQL） | PST-3 |

### 3.3 B3 · 知识库协议 + MCP 工具（11 P0 / 4 P1 / 2 P2）

| ID | 需求 | 级别 | 来源/理由 | 依赖 |
|---|---|---|---|---|
| **KB-1** | **vault 注册（多 vault）**：`fish kb vault add --name <n> --path <绝对路径> [--mode ro\|rw] [--ignore <glob>]... [--default]`；登记**不搬运**；同路径重复 add 幂等（不新增行）；记录名字/路径/读写模式/忽略规则/最近扫描/备注 | P0 must | 用户拍板 5/6；`10 §7.2`（`30-知识库/vaults.md` 是它的投影） | — |
| **KB-2** | **`kb tree`**：默认 **2 层**（`--depth` 可配 1–4）；**只统计不读正文**：每目录给 `笔记 n / 附件 m / 字节 / 最近修改`，不列文件名（附件只给计数）；受**预算约束**（默认 ≤8000 字符 / ≤1200 tokens）；超预算按"每目录最多 20 项 + `…还有 N 项`"截断并明示 | P0 must | `09 R3-2`/`R6-5`；用户拍板 6；`10 §7.4`（attachments 只计不展） | KB-1 |
| **KB-3** | **`kb open <path>`**：必须给**显式相对路径**；返回正文 + 三句声明（①来源是知识库不是 fish 记忆 ②未建索引 ③是否截断/截断多少字节）；超过 `max_open_bytes`（默认 2 MiB）**拒绝**并给字节数与建议；二进制/解码失败**明说** | P0 must | `09 R3-3`；实测 315 个 >10 MB、10 个 >100 MB | KB-1 |
| **KB-4** | **`kb search <query>`**：只在已注册 vault 范围内、只扫 `.md`；结果**必须标注**"来源=知识库（未建索引）"+"本次扫描 N 个文件/M 字节/耗时 x ms"；默认 10 条（上限 30），每条给**全路径**+ 行号 + 上下文片段；**绝不与 `recall` 结果混在同一集合** | P0 must | 用户拍板 3；`09 §8.2 B3`；实测扫描 297 md = 130 ms | KB-1 |
| **KB-5** | **写三档**：`create`（目标不存在才成功）/ `append`（追加到末尾）/ `update`（**必须带 `base_sha256`**，不匹配即拒绝）；**覆盖必须走 update 且先备份**；`create` 目标已存在 → 拒绝并提示改用 update | P0 must | 用户拍板 4；`09 R3-5` 升级 | KB-6/7 |
| **KB-6** | **Obsidian 兼容**：UTF-8 无 BOM；frontmatter 只增改给定键、**不删未知键**；正文 `[[双链]]` **不转义不重写**；新建文件时**同名检测**（实测 vault 已有 2 组同名笔记）→ 同名不同路径时警告"双链会歧义"，并要求用相对路径表达 | P0 must | 用户拍板 4（"以兼容 Obsidian 机制的形式"）；实测 295 md / 2 组同名 | KB-5 |
| **KB-7** | **备份与回滚**：每次写前把**被写的那一个文件**（不含整库）备份到 `p0/data/_backups/kb/<vault_id>/<ts>-<slug>/`，回执给 `write_id` + 备份路径；`fish kb rollback <write_id>` 恢复字节并校验 sha256；目标已被他人改动时**拒绝回滚**（除非 `--force`） | P0 must | 用户拍板 4（可回滚）；整库 10.96 GB → **绝不整库备份** | KB-5 |
| **KB-8** | **访问与写入记录 `kb_access`**：tree/open/search 命中/写入/回滚/拒绝各落一行（谁、何时、哪个 path、动作、来源通道、结果、字节数、sha256、备份路径）；**不记正文**；无 token 调用记 `unattributed`（与 `mcp_server.py` L53 同一取值） | P0 must | `16 §4.2`；用户原话"激活从不被看的知识"必须先知道谁看过 | KB-2/3/4/5 |
| **KB-9** | **MCP 工具 2 个**：`kb`（读：vaults/tree/open/search）、`kb_write`（写：create/append/update）；`token` 由 `_attach_token` 自动补，**工具 schema 不得自带 token**；同步更新 `test_mcp.py::test_02_tools_list` 的集合断言（16→18） | P0 must | 用户拍板 3；`mcp_server.py` L115–129、`test_mcp.py` L103–118 | KB-2/3/4/5 |
| **KB-10** | **硬边界**：知识库内容**不进 `memory_fts`**、**不进 `recall` 默认范围**、**不进 `inject` 注入包**；知识库检索是独立面 | P0 must | `09 R3-1`（hard）；用户拍板 3 不解除这条 | KB-4 |
| **KB-11** | **安全边界**：路径穿越（`..`）、绝对路径、盘符、UNC、`\\?\`、vault 内 junction/符号链接逃逸、越出 vault 根、`.obsidian/**` 与 `.trash/**` 忽略、权限失败、vault 离线——**全部明确报错，不许静默降级** | P0 must | `10 §7.4`；实测 vault 0 符号链接（需自造坏输入才测得到，见 §4.3） | KB-1 |
| **KB-12** | 非 UTF-8（如 GBK）与超限文件的处理：声明编码、可替换读并标注"可能乱码"，或拒绝；**媒体文件永不 open**（按扩展名 + 魔数双判） | P1 should | 实测 .md 之外 2203 个附件（png/jpg/mp4…） | KB-3 |
| **KB-13** | **`kb stats`**：未读清单（`kb_access` 里从无 `open` 的笔记数/总数）、最近变更、每 vault 的读写计数 | P1 should | 用户拍板 3 的目标（"激活从不被看的"）；`16 §4.2` | KB-8 |
| **KB-14** | **`kb doctor` + Obsidian CLI 增强层**：app 运行时可用 `Obsidian.com`（CLI 需 1.12+ installer、app 必须在跑、Windows 用 `Obsidian.com` 重定向）做增强（如按 wikilink 解析、backlinks）；**不可用时明写 `obsidian_cli=unavailable` 并照常用文件系统底座** | P1 should | 用户取证（官方 CLI 事实）；实测当前 app **未运行** | KB-1 |
| **KB-15** | 写后校验：重读文件算 sha256，与回执一致；frontmatter 能被 `---` 解析；不一致即报错（不假装成功） | P1 should | 铁律 14（异步 200 ≠ 成功） | KB-5 |
| **KB-16** | `kb_fts` 独立索引（只有当真痛点出现：search 变慢/需要排序/需要增量） | P2 could | 冲突 C1 的落法③，本批不做 | KB-4 |
| **KB-17** | "从未被读过的知识"主动提示（接推送系统） | P2 could | 用户拍板 3 的后半句；依赖推送候选层 | KB-13 |

### 3.4 横向（3 P0 / 1 P1）

| ID | 需求 | 级别 | 来源/理由 |
|---|---|---|---|
| **X-1** | **每条判据必须能变红**：新增判据必须附"怎么让它变红"（变异驱动或坏输入），否则不算判据 | P0 must | `09 §8.2` 前置纪律；`RX-2` |
| **X-2** | **数字不抄副本**：文档只留权威来源与复跑命令，不写字面量当真相；本文所有数字标"实测 as-of" | P0 must | `RX-3`（历史上 30/90 vs 21/60 的真实事故） |
| **X-3** | **文档与测试同步**：新增工具/表的同一提交必须更新① `test_mcp.py` 工具集合 ② `docs/代码地图-基线.md` ③ `30 §6.2` 的过期行 | P0 must | 实测三处已漂移（§1.1 末） |
| **X-4** | 向后兼容：现有 CLI/MCP 行为不变；`--root` 语义不变；不重命名既有工具/子命令 | P1 should | 41 个测试文件依赖现有面 |

### 3.5 计数

| 级别 | B1 | B2 | B3 | 横向 | 合计 |
|---|---|---|---|---|---|
| P0 | 7 | 5 | 11 | 3 | **26** |
| P1 | 2 | 3 | 4 | 1 | **10** |
| P2 | 1 | 1 | 2 | 0 | **4** |
| 合计 | 10 | 9 | 17 | 4 | **40** |

> 逐条可点：B1 = HOME-1~7（P0）/ HOME-8~9（P1）/ HOME-10（P2）；B2 = PST-1~5（P0）/ PST-6~8（P1）/ PST-9（P2）；B3 = KB-1~11（P0）/ KB-12~15（P1）/ KB-16~17（P2）；横向 = X-1~3（P0）/ X-4（P1）。

---

## 4. 验收判据（每条必须能失败）

> 写法：`判据` = 怎么判；`变红` = 怎么让它失败。`证据面` = 跑什么/看哪个文件。
> 纪律：**判据与被测实现分离**——不用被测函数自证（例：`layout verify` 的判据不许只调 `layout render` 的返回码）。

### 4.1 B1 门面

| ID | 判据 | 怎么让它变红（变异） | 证据面 |
|---|---|---|---|
| HOME-1 | `os.walk(fish-home)`：五个一级目录齐；`max(深度) ≤ 3`；一级目录集合**严格等于**五元组 | `mkdir fish-home/99-垃圾` → verify 报"多余一级目录"并 exit 1；`rmdir 20-记忆库` → 报缺失 | `fish layout verify` |
| HOME-2 | 每个一级目录 `README.md` 存在，且四要素各命中一行（正则 4 条全中） | 删掉"谁在写"那行 → 该目录判红（不是整体 exit 0） | `fish layout verify` |
| HOME-3 | ① `20-记忆库/**` 下文件名不匹配 `^[A-Z\u4e00-\u9fff]{2,8}-\d{6}-\d{2,3}\.md$`；② 更硬：该目录下所有 md 的 sha256 与 `p0/data/projects/**/*.md` 的 sha256 集合**无交集** | 把 `anyi-studio-261024-01.md` 拷进 `20-记忆库/` → ②必红 | `fish layout verify`（含 hash 集比对） |
| HOME-4 | 删整个 `fish-home/` → `render --all` → 文件清单与删除前**全等**；连跑两次，`diff` 只允许 as-of/时间行不同（≤3 行） | 把 as-of 换成固定字符串：**幂等判据变绿，但 PST-4 的 as-of 真实性判据要红**（两条判据必须同时存在，防"为了幂等而造假"） | `render --all` ×2 + `diff` |
| HOME-5 | 全绿 exit 0；任一问题 exit≠0 并逐条列出（文件/问题类型/修法提示） | 见 HOME-1 的两个变异 | `fish layout verify`（PowerShell 看 `$LASTEXITCODE`） |
| HOME-6 | `render --all` 前后对 `p0/data` 做 `(path,size,mtime)` 快照，diff 必须为空 | 让渲染器顺手写一个 `p0/data/_reports/layout.json` → 必红 | 快照脚本（复跑命令见 §11） |
| HOME-7 | 解析 `00-首页/README.md` 的 markdown 链接，`10-项目库/项目状态表.md` 目标存在（相对路径可解析） | 把链接改成 `项目状态表.md`（少一级）→ 目标不存在 → 红 | 链接解析脚本 |
| HOME-8 | 用正则扫 `90-系统/**`：`fsh_[A-Za-z0-9_-]{20,}`、`token\s*=`、`BEGIN .*PRIVATE KEY` 命中数全 0 | 往 `IDENTITY.md` 塞一个假 `fsh_` 串 → 必红 | `fish layout verify` 子检查 |
| HOME-9 | 把 `boards` 表临时改名 → 渲染 `10-项目库/*/看板-*.md` 或状态表时出现"数据源不可用：no such table: boards"，且**不生成空文件** | 让异常被 `except: pass` 吞掉 → 红 | 临时库（`mktemp` 副本）上跑 |
| HOME-10 | P2，不设判据 | — | — |

### 4.2 B2 状态表

| ID | 判据 | 怎么让它变红（变异） | 证据面 |
|---|---|---|---|
| PST-1 | `10-项目库/项目状态表.md` 存在；首页链接可解析（同 HOME-7） | 改文件名不改链接 → 红 | 链接解析 |
| PST-2 | 表头 `<thead>` 第一行严格等于六列且顺序一致（字符串比较，允许前后空格） | 少一列/换序 → 红 | 解析表格 |
| PST-3 | **插假前缀必出现**：往 `code_registry` 插 `FISHTEST`（临时库）→ 重渲染 → 表里出现该行；删掉 → 消失 | 把项目来源改成硬编码列表 → 插假前缀后不出现 → 红 | 临时库 + `render` |
| PST-4 | 表头含 `as-of <ISO8601>`（带时区）；两次渲染除 as-of 行外全等；as-of 与渲染完成时刻差 < 60 s | 写死 as-of（固定日期）→ 与当前时刻差 >60 s → 红 | `diff` + 时间比对 |
| PST-5 | 构造三种空数据（无记忆 / 无档期 / 今日无看板）→ 对应列必须是 `—（…）`，**不得为空、`None`、`-`**；表尾有脚注解释 | 把 `None` 直接格式化进表格 → 红 | 临时库造数 |
| PST-6 | 造一条标题含"未闭合"的 active 记忆（该项目）→ 该行未了事项 +1；把记录时间改到 31 天前 → 计数回 0 | 窗口从 30 天改成 0 天 → 造数后不出现 → 红 | 临时库 + `render` |
| PST-7 | `retired` 项目不在主表；折叠区计数与 `code_registry` 的 retired 行数相等 | 把 retired 混进主表 → 红 | 解析表格 |
| PST-9 | `--json` 输出可被 `json.loads`；每行含 `sources` 字段 | 去掉 `sources` → 红 | `fish project status --json` |

**这张表一个必须提前说清的"预期红"**：实测全库只有 **5 条**命中开放词（`大肥鱼生命系统` 4 / `anyi-studio` 1），所以 12 个 active 项目里 **10 行**的"未了事项"会是 `—`。这是**正确结果**，不是缺陷——`09 §8.2` 的"未了事项列非空"写成"每行非空"会让本表永远红。判据按 PST-5/6 的写法（造数必现、清零必消）执行。

### 4.3 B3 知识库

| ID | 判据 | 怎么让它变红（变异） | 证据面 |
|---|---|---|---|
| KB-1 | 新增 → 行数 +1；同路径再 add → 行数不变（幂等），`updated_at` 变；`--mode` 非法值被拒 | 同路径两次 add 后行数 +2 → 红 | CLI + 查 `kb_vault` |
| KB-2 | 默认输出 ≤8000 字符（`len(text)` 断言）且 `count_tokens(text) ≤ 1200`；含 `笔记/附件/字节/最近修改` 四类数；**渲染期间未 `open()` 任何文件**（monkeypatch `builtins.open` 计数=0 或审计 `open` 调用） | ① 把 depth 改成 4 → 字符超限 → 红；② 在 tree 里读 frontmatter → `open()` 计数 >0 → 红 | `fish kb tree` + 计数断言 |
| KB-3 | 正常 md → 返回正文 + 三句声明齐；>2 MiB 文件 → 拒绝并给字节数；`.mp4`/`.png` → 拒绝（"二进制/媒体不支持"） | 把上限提到 1 GB 然后 open 641.2 MB 的 mp4 → 若返回乱码/空 → 红 | `fish kb open` 三个样本 |
| KB-4 | 在 vault 写一个独特词 `FISH-KB-PROBE-261026`（临时 vault）→ `kb search` 命中 1；回执含"来源=知识库（未建索引）"与"扫描 N 文件"；结果行给**全路径** | 把 `kb search` 实现成调 `recall` → 独特词 0 命中 → 红（同时也是 KB-10） | 临时 vault + 两个命令对照 |
| KB-5 | `create` 新路径成功；对已存在路径 `create` → 拒绝；`update` 不带 `base_sha256` → 拒绝；带错 sha → 拒绝；`append` 后原字节是新字节的**前缀** | 让 `create` 直接覆盖已存在文件 → 原内容只能从备份找回 → 判据红（若备份也没了，红得更彻底） | CLI 5 个用例 |
| KB-6 | 写后文件：无 BOM、`---` frontmatter 可解析、**未知键仍在**、正文里 `[[目标]]` 原文保留（非 `\[\[`）；同名不同路径时回执给警告 | 写入时把 `[[` 转义 → 红；写入时重建 frontmatter（丢未知键）→ 红 | 写后读取 + 静态断言 |
| KB-7 | 每次写都有备份文件存在（`write_id` 可取）；`rollback` 后字节与原文件全等（sha256 相等）；备份被删后 `rollback` → 明确报"备份缺失" | 删备份再 rollback 若返回成功 → 红 | CLI + 文件比对 |
| KB-8 | tree/open/search/写 各产生一行 `kb_access`；不带 token 调 MCP → `citizen_id` 必须是 `unattributed`；**`meta`/行内不出现正文片段** | ① 不写 access 行 → 红；② 把 query 原文塞进 `meta` → 隐私判据红（见 §7.5） | 查表 + 正则 |
| KB-9 | `tools/list` 名字集合 = 原 16 + `{kb, kb_write}`；每工具 `inputSchema.type=="object"`；**schema 里没有 `token` 属性**（由 `_attach_token` 补） | 不更新 `test_mcp.py` 断言 → 全量红（这正是判据）；工具 schema 自带 token → `_attach_token` raise → 红 | `test_mcp.py` + 全量跑 |
| KB-10 | ① 独特词：`recall` **0 命中**、`kb search` **1 命中**；② `memory_fts` 行数在 KB 操作前后 diff=0；③ `inject` 包文本不含独特词 | **变异自证**：把该 vault 文件 `cp` 进 `p0/data/projects/<前缀>/` 并 `fish reindex` → `recall` 命中 1（证明判据在测边界，不是恒绿）；删掉再测回 0 | 临时库 + 3 条命令 |
| KB-11 | 表驱动 8 个坏路径全部拒绝且错误可判：`../../windows/system32`、`C:\Windows\win.ini`、`\\server\share\x`、`\\?\C:\x`、`/etc/passwd`、`a/../../b`、空路径、指向 `C:\Windows` 的 junction 子路径；`.obsidian/**`、`.trash/**` 一律不可 open/tree | ① 删掉 realpath 前缀校验 → 前 6 例放行 → 红；② 把 junction 检查去掉 → junction 例放行 → 红 | 临时 vault（必须**自造** junction，实测真库 0 个链接） |
| KB-12 | 造 GBK 文件 → open 明说编码（或转码并标注）；按扩展名 + 魔数双判，`.png` 改名成 `.md` 仍被识别为二进制 | 只按扩展名判 → 改名文件被当文本读成乱码 → 红 | 临时 vault |
| KB-13 | 注册后未访问 → 未读率 100%；`open` 一篇 → 未读 -1；`stats` 计数与 `kb_access` 聚合一致 | 不清空 `kb_access` 就断言 0% → 红 | 临时 vault |
| KB-14 | app 未运行（当前实测）→ `doctor` 输出 `obsidian_cli=unavailable`，`tree/open/search` 全部照常可用；启动 app 后 → `available`，且**结果与无 CLI 时一致**（底座不受增强层影响） | 把 tree 实现成依赖 CLI → app 未运行时全红 | `fish kb doctor` 两态对照 |
| KB-15 | 写后 re-read 的 sha256 == 回执 sha256；人为截断 content → 校验失败并报错 | 去掉校验 → 截断仍报成功 → 红 | CLI |

**变异自证的最低配额**（X-1）：B1/B2/B3 各至少 **1 条**带"证明判据会红"的记录（日志或测试内注释），格式沿用现有 `p0/变异自证-*.log` 习惯。

---

## 5. 数据模型草案

### 5.1 总览

| 变更 | 对象 | 说明 |
|---|---|---|
| 新增表 | `kb_vault` | 多 vault 注册（用户拍板 5） |
| 新增表 | `kb_access` | 读写事件流水（取代 `16 §4.2` 的单薄访问表，**加宽不加表**） |
| 新增视图 | `kb_path_stats` | 由 `kb_access` 聚合出 `16 §4.2` 要求的 `(path, citizen_id, last_opened_at, open_count)` 形态 |
| 语义变更 | `boards.path` | 从"相对 `data_root`"改为"相对 `fish-home`"（冲突 C3 落法①）；需迁移 7 行 |
| **不动** | `events` / `code_index` / `code_registry` / `project_stints` / `boards` 其余列 / `presence` / `board_posts` | B1/B2 只**读** |

> 原则：**B1/B2 不新增任何业务表**（都是投影）；B3 只加 2 表 1 视图。任何"顺手再加一张"都算违反 RX-4（防重复造）。

### 5.2 `kb_vault`

```sql
CREATE TABLE IF NOT EXISTS kb_vault (
  vault_id      TEXT PRIMARY KEY,          -- ulid；**不用路径做主键**（改名/搬家不该换 id）
  name          TEXT NOT NULL UNIQUE,      -- 人给的名字（默认取根目录名，可自定义）
  path          TEXT NOT NULL UNIQUE,      -- 绝对路径（存原样；比较前做 normcase+realpath）
  mode          TEXT NOT NULL DEFAULT 'ro' CHECK (mode IN ('ro','rw')),
  ignore_globs  TEXT NOT NULL DEFAULT '[".obsidian/**",".trash/**"]',  -- JSON 数组
  max_open_bytes INTEGER NOT NULL DEFAULT 2097152,                     -- 2 MiB
  is_default    INTEGER NOT NULL DEFAULT 0,                            -- 0/1，最多一个 1
  last_scan_at  TEXT,                      -- 只在显式 `kb vault scan` 时更新（读路径不写库）
  last_scan_files   INTEGER,
  last_scan_notes   INTEGER,
  last_scan_bytes   INTEGER,
  note          TEXT,
  status        TEXT NOT NULL DEFAULT 'active',
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_kb_vault_default ON kb_vault(is_default) WHERE is_default=1;
```

**为什么存 `vault_id` 而不是用 path 当主键**：`path` 会变（盘符改、目录改名），而 `kb_access` 与备份目录都要长期指向同一个"库"。路径当主键会把历史访问记录全部变成孤儿。

### 5.3 `kb_access`（+ 视图）

```sql
CREATE TABLE IF NOT EXISTS kb_access (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  vault_id   TEXT NOT NULL,
  path       TEXT NOT NULL,              -- vault 内相对路径，分隔符统一 '/'
  citizen_id TEXT NOT NULL,              -- 无凭证 = 'unattributed'（同 mcp_server.py L53）
  account_id TEXT,
  action     TEXT NOT NULL,              -- tree|open|search_hit|create|append|update|rollback|reject
  result     TEXT NOT NULL DEFAULT 'ok', -- ok|denied|not_found|too_large|bad_encoding|offline
  via        TEXT NOT NULL,              -- mcp:kb|mcp:kb_write|cli:kb
  ts         TEXT NOT NULL,
  bytes      INTEGER,
  sha256     TEXT,                       -- 写：写后 hash；读：读到的 hash（便于"是不是同一版"）
  backup_path TEXT,                      -- 写：备份位置（回滚入口）
  meta       TEXT                        -- JSON：depth/limit/scan_ms/query_hash/reason…**不存正文**
);
CREATE INDEX IF NOT EXISTS ix_kb_access_path    ON kb_access(vault_id, path, ts DESC);
CREATE INDEX IF NOT EXISTS ix_kb_access_citizen ON kb_access(citizen_id, ts DESC);
CREATE INDEX IF NOT EXISTS ix_kb_access_action  ON kb_access(action, ts DESC);

CREATE VIEW IF NOT EXISTS kb_path_stats AS
SELECT vault_id, path,
       COUNT(*)                                                   AS hits,
       MAX(ts)                                                    AS last_at,
       SUM(CASE WHEN action='open' THEN 1 ELSE 0 END)              AS open_count,
       SUM(CASE WHEN action IN ('create','append','update','rollback')
                THEN 1 ELSE 0 END)                                 AS write_count,
       COUNT(DISTINCT citizen_id)                                  AS readers
FROM kb_access
GROUP BY vault_id, path;
```

**与 `16 §4.2` 的差异（需架构确认，不算擅自改判）**：16 只要求 `kb_access(path, citizen_id, last_opened_at, open_count)`。本设计是**事件流水 + 聚合视图**，理由三条：① 回滚需要"哪次写、备份在哪"→ 必须有 `backup_path`；② 用户要"哪些积累从没被读过"→ 需要"是否发生过 open"而不只是次数；③ 事件式可审计（谁在何时改过哪篇），聚合形态由视图保留。

### 5.4 `boards.path` 语义变更（冲突 C3）

现状：`boards.path` 由 `board_relpath()` 生成，形如 `projects/anyi-studio/看板-261025.md` 或 `看板-261026.md`，写入时拼 `Path(root)/path`（`fish.py` L8370）→ 落在 `p0/data/`。

改后：`boards.path` 改为 `10-项目库/anyi-studio/看板-261025.md` / `00-首页/看板-261026.md`（根板），渲染写 `fish-home/<path>`。

迁移（一次性，7 行）：
```sql
UPDATE boards SET path = CASE scope
  WHEN 'root'    THEN '00-首页/' || path
  WHEN 'project' THEN REPLACE(path, 'projects/', '10-项目库/')
  WHEN 'branch'  THEN '10-项目库/_分支/' || scope_key || '/' || substr(path, length('boards/branch/'||scope_key||'/')+1)
END;
```
> 迁移脚本不在本批范围（本批只写 PRD）；但**判据要求**：迁移后 `fish board render` 仍能重建全部看板文件，且 `p0/data/` 下不再新增看板文件。

### 5.5 只读依赖的既有形态（实测，防实现时猜列名）

| 表 | 关键列（实测） | B2 用途 |
|---|---|---|
| `code_registry` | `prefix`（PK）, `namespace`, `last_date`, `last_seq`, `status` | 项目集合 + 状态；**无 owner 列** |
| `code_index` | `code`(PK), `title`, `project`, `owner`, `mem_type`, `status`, `tags`, `file_path`, `occurred_at`, `recorded_at`, `last_accessed_at`, `access_count`, `dormant`, `superseded_by` | 最近活跃、未了事项、正文路径 |
| `boards` | `id`, `scope`(root/project/branch), `scope_key`, `period_key`(YYMMDD), `path`, `status`(open/closed), `created_at`, `closed_at` | 看板入口 |
| `presence` | `board_id`, `citizen_id`, `agent_id`, `status`, `doing`, `surface`, `first_seen_at`, `last_seen_at` | 在场 → 最近活跃兜底 |
| `project_stints` | `id`, `citizen_id`, `account_id`, `project`, `from_ts`, `to_ts`, `set_by`, `closed_reason` | 主责 AI（开档期 = `to_ts IS NULL`） |
| `citizens` / `citizens_all` | `citizen_id`, `display_name`, `kind`, `status`… | 主责 AI 的显示名 |

---

## 6. 接口草案

### 6.1 CLI（沿用 `fish --root` 全局参数，不新增"根"概念）

```bash
# B1 门面
fish layout render --all [--only <00-首页|10-项目库|20-记忆库|30-知识库|90-系统>] [--as-of <ISO8601>]
fish layout verify [--json]

# B2 状态表
fish project status [--include-retired] [--json]

# B3 知识库
fish kb vault add --name <n> --path <绝对路径> [--mode ro|rw] [--ignore <glob>]... [--default]
fish kb vault list|remove <n>|set-default <n>|scan [<n>]
fish kb tree [path] [--vault <n>] [--depth 2] [--max-chars 8000]
fish kb open <path> [--vault <n>] [--max-chars 64000]
fish kb search <query> [--vault <n>] [--limit 10] [--json]
fish kb write <path> (--create | --append | --update --base-sha256 <h>)
                 --content-file <f> [--frontmatter k=v]... [--reason <s>] [--vault <n>]
fish kb rollback <write_id> [--force]
fish kb stats [--vault <n>]
fish kb doctor
```

- 全部命令默认**只打印计划**的写操作照既有习惯（`project retire/close` 是 `--apply` 才落库）；但 `kb write` 是**显式写命令**，不需要 `--apply`（命令本身就是意愿表达）。
- `layout render` 与 `kb` 写操作的 stdout 必须包含"改了什么/备份在哪/怎么撤"三件事（人可读）。

### 6.2 MCP 工具（2 个）

> 形式遵循现有 `TOOLS` 风格（`name/title/description/inputSchema`，`token` 由 `_attach_token()` 补，**不得自带**）。

**① `kb`（读）**

| 项 | 内容 |
|---|---|
| 何时用 | 想确认"知识库里有什么/有没有相关积累"；要看某篇原文；要在知识库范围内找关键词 |
| 何时不用 | 找 fish 记忆用 `recall`；找编码用 `resolve_code`；不要在开场无条件调 |
| 输入 | `action`（必填：`vaults`/`tree`/`open`/`search`）；`vault`；`path`；`query`；`depth`（默认 2）；`limit`（默认 10，≤30）；`max_chars`（默认 8000） |
| 返回 | markdown 文本，头部固定三行：`source=知识库（未建索引）`、`as_of=<ISO8601>`、`scan=<N files / M bytes / x ms>`（`open` 时为 `truncated=true|false, shown=A/B bytes`） |
| 预算 | `tree` ≤8000 字符/≤1200 tokens；`open` ≤64 KB；`search` ≤8000 字符/≤1800 tokens；超限即截断并明示 |
| 失败 | 路径非法/越界/离线/超限/编码 → 明确错误文案 + 可执行下一步（"用 fish kb vault list 看有哪些库"） |

```json
{
  "name": "kb",
  "title": "知识库",
  "description": "只读访问已登记的知识库（Obsidian vault）：看目录、读单文件、在知识库范围内检索。返回内容来自「知识库」，不是 fish 记忆，也不会进 recall / 全文索引。知识库默认整库跳过检索，只有你显式调用才会读到。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "action": {"type": "string", "enum": ["vaults", "tree", "open", "search"]},
      "vault":  {"type": "string", "description": "vault 名字或 id；缺省=默认 vault"},
      "path":   {"type": "string", "description": "vault 内相对路径，用 / 分隔；open 必填；tree 可选起点"},
      "query":  {"type": "string", "description": "search 必填；字面匹配（非语义），大小写不敏感"},
      "depth":  {"type": "integer", "minimum": 1, "maximum": 4, "default": 2},
      "limit":  {"type": "integer", "minimum": 1, "maximum": 30, "default": 10},
      "max_chars": {"type": "integer", "default": 8000}
    },
    "required": ["action"]
  }
}
```

**② `kb_write`（写）**

| 项 | 内容 |
|---|---|
| 何时用 | 人明确要沉淀一篇笔记；或 AI 需要把结论留在知识库（而不是 fish 记忆）时 |
| 前置 | vault 必须是 `mode='rw'`；`update` 必须先 `kb open` 拿到 `sha256` |
| 输入 | `action`（`create`/`append`/`update`）；`vault`；`path`；`content`；`frontmatter`（对象，只增改）；`base_sha256`（update 必填）；`reason` |
| 返回 | `write_id`、新旧 `sha256`、`backup_path`、`bytes`、一句"撤销命令" |
| 禁止 | 删除、移动、重命名、改 `.obsidian/**`、改 `.trash/**`、无备份覆盖、`content` >512 KB（提示分段 append） |
| 预算 | 单次 `content` ≤512 KB；回执 ≤1 KB |

```json
{
  "name": "kb_write",
  "title": "写知识库",
  "description": "在已登记的知识库里新建/追加/显式更新一篇 Markdown（Obsidian 兼容：UTF-8、frontmatter 只增改、[[双链]] 不转义）。每次写入前自动备份，可用 fish kb rollback <write_id> 撤回。不允许删除/移动/重命名；update 必须带 base_sha256；同名笔记先报冲突，不自动改写别人的文件。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "action": {"type": "string", "enum": ["create", "append", "update"]},
      "vault":  {"type": "string"},
      "path":   {"type": "string", "description": "vault 内相对路径，用 / 分隔（必填）"},
      "content": {"type": "string", "description": "Markdown 正文（UTF-8）；create/update 必填"},
      "frontmatter": {"type": "object", "description": "只增/改这些键，已有未知键保留"},
      "base_sha256": {"type": "string", "description": "update 必填：你读到的文件 sha256（乐观锁）"},
      "reason": {"type": "string", "description": "为什么写（进 kb_access.meta.reason）"}
    },
    "required": ["action", "path"]
  }
}
```

### 6.3 预算总表（可判）

| 出口 | 上限 | 超限行为 |
|---|---|---|
| `kb tree` | 8000 字符 / 1200 tokens；每目录最多 20 子项 | 截断 + `…还有 N 项（加 --depth/--path 展开）` |
| `kb open` | 64 KB 返回；文件 >2 MiB 直接拒绝 | `truncated=true` + `shown=A/B` |
| `kb search` | 10 条（≤30）；每条 ≤1 行上下文（前后各 120 字符） | `hits=N, shown=M`，并给"缩小 query"的建议 |
| `kb_write` | 单次 512 KB | 拒绝 + 建议 append 分段 |
| `layout render` | 单文件 ≤32 KB（状态表/索引）；超限按分页拆文件 | 拆成 `索引-<YYYY-MM>.md` 按月 |
| `kb_access.meta` | ≤1 KB JSON | 截断并标 `meta_truncated=true` |

### 6.4 接入点（实现级定位，供架构师/工程师）

| 要做的事 | 现有位置 |
|---|---|
| 新增 CLI 子命令族 | `fish.py::main()` L8647（`sub.add_parser(...)`），各 `cmd_*` 函数 |
| 新增 MCP 工具 | `mcp_server.py::TOOLS` L131（列表末尾插入）→ `_attach_token` L436 → `FishServer.call_tool` L1342（加分支）→ 新增 `def tool_kb/tool_kb_write(self, a, ident)` |
| 看板落点改动 | `board_relpath()` L7725、`board_ensure()` L7737、`board_render_root()` L8370（`Path(root)/path`） |
| 未闭合按项目过滤 | `_open_loop_rows()` L5414（加可选 `project=None`） |
| 访问记录风格参考 | `record_access()` L1572 / `append_access_log()` L1613 / `_log_access` (mcp L1034) |
| 身份取值 | `_resolve_identity` L502、`UNATTRIBUTED` L53 |
| 自检风格参考 | `cmd_healthcheck()` L7200（断链 + FTS 落后 + owner 校验三条子检查） |

---

## 7. 安全与隐私边界

### 7.1 路径与逃逸（KB-11 展开）

| 攻击/误用 | 规则 | 报错文案要求 |
|---|---|---|
| `..` 穿越 | 解析后 `realpath(target)` 必须 `startswith(realpath(vault)+sep)` | 说清"越出知识库根"并给相对路径原样回显 |
| 绝对路径 / 盘符 / UNC / `\\?\` | 一律拒绝（只接受相对路径） | "只接受 vault 内相对路径" |
| vault 内 junction/符号链接 | 从 vault 根到目标**逐级**检查 `st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT`，命中即拒绝 | "路径含链接/联接点，已拒绝"（并给断点位置） |
| 越界（合法前缀但同级"假朋友"） | 用 `os.path.commonpath` 而非字符串 `startswith` | 防止 `vault-evil` 绕过 `vault` 前缀 |
| `.obsidian/**`、`.trash/**` | 不可 tree 展开、不可 open、不可写 | "该目录按约定忽略" |
| 权限失败 / 文件被占用 | 透传 `errno` + 路径 | 不许 `except: pass` |
| vault 离线（盘拔掉） | 报 `offline`，`tree` 用 `last_scan_*` 显示旧数据并标 `stale` | "知识库不在线（最后扫描 …）" |

### 7.2 文件类型与大小

- **正常读**：`.md`（UTF-8）；`.canvas`/`.json` 等 Obsidian 文件 **P0 不解析**（只统计）。
- **拒绝 open**：媒体（`.mp4/.png/.jpg/.webp/.gif/.mp3`）、`.bin`、>2 MiB 任意文件、解码失败。
- **双判**：扩展名 + 首 8 KB 魔数（防"png 改名成 .md"）。
- 实测背景：vault 内 **315** 个 >10 MB、**10** 个 >100 MB（最大 641.2 MB）——**任何"顺手读一下"的实现都会撞墙**，这是硬事实，不是假设。

### 7.3 忽略规则

- 默认 `ignore_globs = [".obsidian/**", ".trash/**"]`（`10 §7.4` 修正后的口径）。
- `attachments/**` **不排除**（它的内容本是"只统计"），`tree` 只显示"附件 N 个"不展开文件名。
- 用户可加规则（如 `**/_media/**`、`*.mp4`），规则写进 `kb_vault.ignore_globs`，**tree 回执必须显示生效规则**（否则"为什么这里少了一堆东西"无法解释）。

### 7.4 写入安全（KB-5/6/7 展开）

| 约束 | 做法 |
|---|---|
| 只备份被写文件 | 备份到 `p0/data/_backups/kb/<vault_id>/<ts>-<slug>/`，**绝不整库复制**（整库 10.96 GB） |
| 乐观锁 | `update` 必带 `base_sha256`；不匹配 → 拒绝并提示"先重新读" |
| 目标存在 | `create` 拒绝；提示改名或用 `update` |
| 同名歧义 | 新建前扫同名（vault 内 295 个 md 里已有 2 组同名）→ 警告并要求用相对路径表达双链 |
| frontmatter | 解析失败 → **不写**（不让 AI 把坏 YAML 写进库） |
| 原子写 | 先写临时文件再 `os.replace`（同盘原子） |
| 回滚 | `rollback <write_id>`；目标已被改 → 拒绝（`--force` 才覆盖，且再备份一次） |
| 禁写目录 | `.obsidian/**`、`.trash/**`；`90-模板/**` 待 Q13 |

### 7.5 隐私

- `kb_access` **不存正文**；`meta` 只存 `query_hash`（sha256 前 12 位）+ 长度 + `depth/limit/scan_ms`。
  理由：人的搜索词可能涉私密（凭证、姓名、金额）；把 query 原文落库等于给知识库加一份"搜索历史"。
- 日志（stderr / `_audit`）不打印正文片段；只打 path + bytes + sha256。
- `kb open` **不写 `events`**（只写 `kb_access`）→ 判据：open 前后 `events` 行数不变。
- 知识库内容**不得被自动搬运成 fish 记忆**（禁止"读到就 remember"的自动链路）——这是"整库跳过检索"边界的实际守门人。

### 7.6 降级矩阵（不许静默）

| 条件 | 读路径 | 写路径 | 必须说出口的声明 |
|---|---|---|---|
| Obsidian 未运行（**当前实测就是**） | 全部可用（文件系统底座） | 全部可用（不依赖 app） | `obsidian_cli=unavailable` |
| Obsidian 运行中 | 可用 + 可选增强 | 可用 | 增强项逐条标注 `via=obsidian_cli` |
| vault 不在线 | `tree` 用旧扫描 + `stale` | 拒绝 | `offline` |
| `mode=ro` | 可用 | 拒绝 | `mode=ro，拒绝写入（fish kb vault set-mode <n> rw）` |
| 文件 >2 MiB | 拒绝 | 拒绝 | `too_large` + 字节数 |
| 非 UTF-8 | 声明编码 + 可替换并标注 | 拒绝（不写不确定编码） | `bad_encoding` |

---

## 8. 冲突清单（不替用户拍板）

### C1 · `kb search` vs "整库跳过检索"（永久边界）

- **冲突**：`09 §5.1` 把"Obsidian 全量索引 / 全文检索"列为**永久不做**（用户原话"包括搜索在内的场景时都会被整个跳过的库"）；本批 B3 却要求 `search`，用户新拍板 3 又说"希望 AI 也常用知识库"。
- **落法**
  | 方案 | 做法 | 代价 |
  |---|---|---|
  | ① 不做 search | 只 `tree`+`open`，靠目录与路径找到 | 违背新拍板（AI 很难找东西）；"激活积累"基本落空 |
  | ② 按需扫描（推荐） | 每次 search 现扫 `.md`（实测 130 ms / 297 篇），不建任何索引；回执声明"扫了 N 文件、未建索引" | 结果可能有"漏"（超大库会慢）；无排序/无相关性 |
  | ③ 独立索引 `kb_fts` | 建与 `memory_fts` 物理隔离的索引 | 与"永久边界"直接冲突；多一套一致性与清理成本 |
- **推荐**：②。理由：②不建索引 = 不违反"不索引"的字面与精神；成本可测（130 ms）；错了可退回①。③留到"②真的不够用"再说（KB-16）。

### C2 · 知识库上 MCP vs `16 §6.3` "kb 只走 CLI"

- **冲突**：`16 §6.3` 明确"`fish kb tree` / `fish kb open` 只走 CLI，不占工具位"（理由是"人用或低频"）；用户新拍板 3 要求"知识库要上 MCP 工具"，理由是"AI 才会常用"。
- **落法**
  | 方案 | 做法 | 代价 |
  |---|---|---|
  | ① 只 CLI（守住 16） | AI 得离开 MCP 去跑命令 | 违背拍板 3；且 CLI 与 MCP 的会话键不同，归属会对不上（`mcp_server.py` L86–87 已记录这个坑） |
  | ② 2 工具（推荐） | `kb`（读）+ `kb_write`（写） | 工具面 16→18；需更新 `test_mcp` 断言 |
  | ③ 5 工具细分 | tree/open/search/vaults/write 各一个 | 工具位被稀释（`16 §6.3` 的原始理由），AI 注意力被分散 |
- **推荐**：②。读用一个工具内 `action` 分流（与 `board` 工具同构，`mcp_server.py` L1212），写单独一个（安全契约不同、schema 更重）。

### C3 · 看板落点：`boards.path` 相对 `data_root` vs 门面要求

- **冲突**：`16 §7` 要求看板在 `10-项目库/<项目>/`；实测代码把看板写进 `p0/data/`（L8370）。门面的"看板入口"列如果指向 `p0/data/...`，人在 `fish-home` 里点不开（两棵树）。
- **落法**
  | 方案 | 做法 | 代价 |
  |---|---|---|
  | ① 改渲染根（推荐） | `boards.path` 语义改为相对 `fish-home`，渲染写 `fish-home/<path>`；迁移 7 行 | 要改 3 处代码 + 1 条迁移；`board_render` 的 `root` 参数语义变 |
  | ② 双写 | data 里照旧 + 门面渲染器复制一份 | 同一块板两个文件；"哪份是准的"永远说不清，违反 RX-4 |
  | ③ 门面只放链接 | 链接指向 `../p0/data/看板-*.md` | 链接能用但很丑；门面"自洽"破功，且 `p0/data` 不进 git |
- **推荐**：①。看板的真相在 `boards/presence/board_posts` 三张表，**文件本来就是投影**，换投影位置不丢数据。

### C4 · "AI 可写知识库" vs "知识库只读"

- **冲突**：`09 R3-5` 是 `could`（P2），`09 §9-Q13` 给了"只读 / 允许 append / 双向"三选项；用户已拍板"允许 AI 调整"，但没定"调整"到什么程度。风险是 AI 把人十年的笔记改坏。
- **落法**
  | 方案 | 做法 | 代价 |
  |---|---|---|
  | ① 只读 | 只读不写 | 违背拍板 4 |
  | ② 三档写 + 备份 + 禁删（推荐） | create/append/update（update 带乐观锁）；写前备份；删除与移动不做 | AI 仍可能写出低质量笔记（但可回滚、可审计、不毁原文） |
  | ③ 每次写人工确认 | 进审阅队列，人点通过才落盘 | 最安全，但"常用"会被掐死（AI 会绕开） |
- **推荐**：②，并**默认 `mode=ro`**（注册时人要显式 `rw`）。这样"能写"是授权动作，不是默认状态。

### C5 · "README 可手改" vs "门面可删可重建"

- **冲突**：`HOME-2` 要 README 让人看懂（人会想改），`HOME-4` 要删了能一键重建（生成器会覆盖）。两条同时成立会打架。
- **落法**
  | 方案 | 做法 | 代价 |
  |---|---|---|
  | ① 全部生成 | README 也每次覆盖 | 人改一次就丢一次，最后没人改 |
  | ② 缺失时生成、已存在不覆盖（推荐） | 首次生成兜底文案；之后尊重人的修改；`--force-readme` 才覆盖 | "重建"重出的 README 是兜底版（可接受：判据只要求四要素在） |
  | ③ README 人写、生成器不碰 | 完全不管 | 删了重建 → README 永久丢失，`HOME-4` 判据得放宽 |
- **推荐**：②。README 的判据是"人看懂"，不是"永不变化"；但**重建必须能产出合格兜底**，所以兜底文案要写全四要素。

---

## 9. 明确不做（本批）

| 不做 | 理由 | 什么时候再看 |
|---|---|---|
| 搬 vault、建收录副本、软链进 `fish-home` | 用户拍板 6（原地不动）；软链会让 Obsidian 重复索引且删起来危险 | 换设备/迁移时另立项目 |
| 知识库全文索引 / 向量检索 | `09 §5.1` 永久边界 + ADR-006；C1 落法②已够 | ②真的不够用时（KB-16） |
| AI 删除 / 移动 / 重命名知识库文件 | 移动会断双链；Obsidian 自带重命名会改链，人做更快更安全 | 若确需：先做"影响面报告 + 自动改链 + 备份"，单独立项 |
| 写 `.obsidian/**`（插件、主题、配置） | 动它等于动用户的 Obsidian 本体 | 不做 |
| 把知识库内容自动写进 fish 记忆 | 会把"整库跳过"变成"整库灌进检索" | 不做 |
| `20-记忆库` 放正文副本 | 正文唯一副本在 `p0/data`（`10 §7.2`） | 不做 |
| 迁移 `p0/data` 布局 / `R4-5` 迁移回滚 | 用户拍板 6（data 不动）；`阶段 B4` 不在本批 | 阶段 B 后续 |
| 人类手改正文的事件兜底（`R4-4`） | 本批只做投影，不碰写入链路 | 与 `Q15` 一起议 |
| 界面（门面是文件） | 本批无 UI；UI 范围见 `09 Q20` | 另议 |
| 给知识库做多用户/权限/租户 | `09 §5.1`（本地全信任） | 服务器阶段 |
| 知识库多设备同步 | 同上 | 服务器阶段 |

---

## 10. 待确认问题（按"阻塞什么"分组）

> 纪律：不假设、不填空。每条标"阻塞什么"。

### A 组 · 阻塞 B1（门面）

| # | 问题 | 为什么不猜 | 选项 |
|---|---|---|---|
| **Q1** | README 归谁写？（C5） | 全部生成 vs 缺失时生成 vs 人写，三种落法的"重建"语义不同 | ① 全生成 ② **缺失时生成、已存在不覆盖（推荐）** ③ 人写、生成器不碰 |
| **Q2** | `00-首页` 本期是否放"主人画像.md + 画像建议/"？ | `16 §4.3` 要求画像放首页（人直接编辑 + AI append-only 建议），本批 B1 只列了 README；放进去会改变首页布局与 B1 判据 | ① 本批就放（顺带把画像文件位定死）② 本批不放，只 README（B1 更小） |
| **Q3** | `fish-home` 位置确认在**项目根**（与 `p0/` 同级）？ | `10 §7.1` 原文写 `<fish>/home`，用户改名为 `fish-home` 但没重申位置；`.gitignore` 是根相对口径（能反推），但这是要写死的路径 | ① 项目根/`fish-home/`（推荐）② `p0/fish-home/` ③ 可配（`--home` 参数） |

### B 组 · 阻塞 B2（状态表）

| # | 问题 | 为什么不猜 | 选项 |
|---|---|---|---|
| **Q4** | `主责 AI` 取值口径？ | `code_registry` **无 owner 列**（实测 PRAGMA）；`project_stints` 有 17 条开档期指向 16 个公民，且 11 条是"未分类" → 三种口径给出完全不同的表 | ① 当前开档期公民（多名列前 2 + "等 N 人"；推荐）② 该项目最近一条记忆的 owner ③ 人工在项目 README 维护一列 |
| **Q5** | `未了事项` 的时间窗口：沿用 30 天？还是全量？ | 30 天是 `ATTENTION_OPEN_WINDOW_DAYS=30`（注入包口径）；全量会越攒越多（当前全库仅 5 条命中，全量也不多，但会逐年长） | ① 30 天（与注入包一致，推荐）② 全量（附"最老一条"日期）③ 可配 |
| **Q6** | "项目"以什么为准：`code_registry` 还是正文目录？ | 实测 **16 个前缀 vs 14 个正文目录**，且存在 `_retired-ANYI-20261025`、`_retired-ANYI-late-20261025` 两个非前缀目录（共 154 篇正文） | ① 只以 `code_registry` 为准（推荐；目录多出来的不进表）② 并集（非前缀目录单列"未登记目录"） ③ 以目录为准 |
| **Q7** | `retired` 项目是否进主表？ | 3 个 retired 前缀（`cdql`/`中文测`/`测试项目`）带着历史记忆，全进会让表变噪音，全不进会显得"记忆消失了" | ① 折叠区（推荐，PST-7）② 完全不显示 ③ 混在表里灰显 |

### C 组 · 阻塞 B3（知识库）

| # | 问题 | 为什么不猜 | 选项 |
|---|---|---|---|
| **Q8** | `search` 落法（C1）——**最阻塞**：决定要不要新表 | 三方案的数据模型与判据完全不同 | ① 不做 ② **按需扫描（推荐）** ③ 独立 `kb_fts` |
| **Q9** | 默认写模式：全 `ro` 还是把用户的 vault 直接设 `rw`？ | `ro` 安全但要人多打一条命令；`rw` 好用但等于开门 | ① 默认 `ro`，显式授权（推荐）② 第一个 vault 默认 `rw` ③ 注册即问 |
| **Q10** | 移动/重命名本期是否要？ | 你只说了"允许调整"，而移动会断双链——Obsidian 自己做这个动作会顺手改链 | ① 不做，交 Obsidian（推荐）② 做"移动 + 自动改链 + 备份"（工作量大） |
| **Q11** | 单文件 open 上限（默认 2 MiB 够吗）？ | 实测 315 个 >10 MB、10 个 >100 MB；上限太低会挡住长文档（PDF 转的 md 可能几 MB） | ① 2 MiB（推荐）② 8 MiB ③ 按扩展名分档 |
| **Q12** | 多 vault 时"默认 vault"怎么定？ | 不指定 vault 时要有确定行为，否则 AI 每次都要猜 | ① 第一个登记的（脆：注册顺序不该决定默认） ② 显式 `is_default` + `set-default`（推荐） ③ 必须每次显式传 |
| **Q13** | 允许 AI 写 `90-模板/` 吗？ | 模板决定人以后生成的所有笔记；AI 改模板 = 改人的产出标准 | ① 禁止（推荐）② 允许 ③ 允许但每次要 `reason` + 进审阅 |

### D 组 · 阻塞测试与交付

| # | 问题 | 为什么不猜 | 选项/需要什么 |
|---|---|---|---|
| **Q14** | 允许改 `test_mcp.py::test_02_tools_list` 的集合断言吗？（**必然要改**，否则加工具必红） | 这是"改测试"的高风险动作，必须显式授权 | 需要：① 允许同步更新集合 ② 只加工具名、不改断言结构（推荐） |
| **Q15** | 允许给 `_open_loop_rows()` 加可选 `project=None` 吗？（PST-6 需要） | 改的是现有函数签名，虽然向后兼容 | 需要：① 允许（推荐） ② 在 B2 渲染器里另写 SQL（会重复一份词表逻辑） |

### E 组 · 需主理人补充（非用户偏好，但影响排期/口径）

| # | 问题 | 谁答 | 阻塞什么 |
|---|---|---|---|
| **Q16** | 本批交付是否**顺带修三处文档漂移**（`代码地图` 17 vs 16 工具 / `09 §4.4` 2520 vs 2521 / `30 §6.2` 看板已过期）？ | 主理人 | X-3 的验收范围 |
| **Q17** | `fish` CLI 是否进 PATH？（`30 §6.3` 记 P3：新 AI 要翻配置才找到入口） | 主理人 | `kb` 命令的可发现性（MCP 工具不受影响） |
| **Q18** | `_retired-ANYI-*` 两个正文目录（154 篇）是否在本批清理？ | 主理人 + 用户 | B2 的统计口径（Q6）与门面 `20-记忆库` 索引是否包含它们 |

**如果只能回答 3 个**：先答 **Q8（search 落法）/ Q4（主责 AI 口径）/ Q1（README 归属）**——它们决定三块的数据形态，其余可以边做边定。

---

## 11. 附录 A · 证据与复跑

### A.1 本次读数（as-of 2026-10-26 01:3x–01:5x，Asia/Shanghai）

| 读数 | 值 | 取法 |
|---|---|---|
| `events` / `code_index` / 正文 | 614 / 246 / 420 | `sqlite3` 只读 + `os.walk(p0/data/projects)` |
| `code_index` 状态 | active 207 / superseded 33 / withdrawn 6 | 同上 |
| `code_registry` | 16（active 12 / closed 1 / retired 3） | 同上 |
| 各项目记忆数 Top5 | anyi-studio 109 / 大肥鱼生命系统 64 / 企业智能 27 / EVAL 24 / CDQL 11 | `GROUP BY project` |
| boards / presence / posts / seen / stints | 7 / 11 / 17 / 3 / 17 | `COUNT(*)` |
| links / memory_access_log / push_log | 187 / 399 / 112 | `COUNT(*)` |
| 开档期指向"未分类" | 11 / 17 | `project_stints WHERE to_ts IS NULL` |
| 开放词命中（全量） | 5（大肥鱼 4 / anyi-studio 1） | 8 词 `INSTR(LOWER(title))` |
| vault | 2521 文件 / 252 目录 / 10.96 GB | `os.walk` + `getsize` |
| vault 笔记 | 318 个 `.md`（含 `.obsidian`/`.trash`）；排除后 297 篇 / 3.05 MB | 同上 |
| frontmatter / wikilink | 285 / 318 有 frontmatter；302 / 318 有 wikilink，共 928 条 | 正则统计 |
| 大文件 | >10 MB 315 个；>100 MB 10 个；最大 641.2 MB | `getsize` 排序 |
| 同名笔记 | 295 个 basename 中 2 组同名（`拆解报告2.md`、`拆解报告5.md`） | 字典计数 |
| 扫描耗时 | walk+stat 2492 文件 586 ms；读 297 md 并匹配 130 ms | `perf_counter`（缓存热） |
| Obsidian | 1.14.5；`Obsidian.com` 在 PATH；**未运行** | 文件版本 + `Get-Process` |
| 测试 | 41 文件 / 467 个 `def test_*` | 静态正则计数（**未跑**） |

### A.2 复跑命令（只读，不碰主树）

```powershell
cd "I:\app\DSH-Home\workspace\learn\fish-devA\p0"
$env:PYTHONIOENCODING='utf-8'
# 1) 库读数
python -c "import sqlite3;c=sqlite3.connect('file:data/index.sqlite?mode=ro',uri=True);print(c.execute('select count(*) from events').fetchone(),c.execute(\"select status,count(*) from code_index group by status\").fetchall())"
# 2) vault 结构（只读）
python -c "import os;r=r'E:\Obsidian\安逸知识库';f=d=0
for dp,dn,fn in os.walk(r): d+=len(dn); f+=len(fn)
print(f,d)"
# 3) 测试计数（不跑）
python -c "import glob,re;fs=glob.glob('test_*.py');print(len(fs),sum(len(re.findall(r'^\\s+def test_',open(f,encoding='utf-8').read(),re.M)) for f in fs))"
# 4) 全量测试（**本批未跑**；跑之前先确认不会写生产库）
python -m unittest discover -p "test_*.py"
```

### A.3 本文的局限（诚实声明）

1. **没跑测试套件**：只做了静态计数（467）。全量是否绿、跑多久，本文不作断言。
2. **没实测 Obsidian CLI**：app 当前未运行，CLI 行为按用户提供的官方事实引用，**未亲自验证**；因此 P0 设计**不依赖 CLI**。
3. **没碰主工作树**：`大肥鱼生命系统` 的状态与进度一律未查（避免与 `fish-主AI-3` 冲突）。
4. **扫描耗时是缓存热读数**：冷启（尤其 E 盘为机械盘）未测，`kb tree` 首跑可能明显慢于 586 ms。
5. **没验证"Obsidian 能打开 AI 新建的笔记"**：这需要人打开 Obsidian 看一眼（KB-6 判据里已标为人工判）。

---

### 第二轮口径变更（2026-10-26 · 权威文件：`docs/主理人裁决-第二轮修复.md`）

> 本节只列**与 v0.1 原文不同**的口径；未提及的条款一个字没变。实现与判据见
> `p0/test_b3_round2.py`、`docs/偏离记录-B1B2B3-v0.1.md` 第二轮段、
> `docs/变异自证-B1B2B3-v0.1.md` §5。

| 原条款 | 第二轮口径 |
|---|---|
| §7.5 / 裁决 §四.6「`kb_access.meta` 只存 `query_hash`」 | 加强：**失败路径**同样不存 query 原文（`path=""`、`meta` 只有 `query_hash`）；reject 行额外存 `meta.reason_code`（错误码，可统计） |
| KB-2「`--depth` 可配 1–4，默认 2 层」 | 落实：`depth` 真裁剪；超深处只汇总不下钻，未展开数量在回执头与 `scan.partial` / `scan.hidden_dirs` 明示（合计只含已展开部分，不冒充全量） |
| KB-4「结果行给全路径」 | 文本回执给**库内全路径 + 相对路径**两种（全路径可落地，相对路径给 `kb open` 用） |
| §7.3「`attachments/**` 只显示附件 N 个、不展开文件名」 | 落实：`tree` 只列 `.md` 文件名，附件只进目录汇总计数 |
| §7.4 禁写目录（裁决 Q13） | 定为**禁写 + 禁读**：`90-模板/**` 与 `.obsidian/**`、`.trash/**` 同为**不可删**默认忽略项 |
| §5.2 忽略规则 | 判定前逐段 `normcase` + `rstrip(". ")`（只用于判定）；默认黑名单大小写不敏感——Windows 上 `.OBSIDIAN/`、`.obsidian /` 是同一个目录 |
| §5.8「非 UTF-8 → 写路径拒绝」 | 落实并加码：`append`/`update` 目标不是 UTF-8 族 → `bad_encoding`，**文件字节不变**（原实现 append 会写坏、update 抛裸异常） |
| §3.5「`write_id` 后缀防碰撞」 | 落实：`rollback` 读备份 `meta.json` 做**全串相等**校验；错后缀 → `not_found` 且文件不动 |
| §4.5 回滚 | 同上；备份 `meta.json` 里没有 `write_id` 的历史备份同样拒绝回滚（不猜） |
| §3.3 MCP 工具面 | `kb_write` **必须有有效 token**（文案同 board checkin）；`token` 仍不在 `required`——「不 required」≠「不带能写」。`kb` 增可选 `allow_sensitive` |
| 新增：敏感名单 | 内置不可删：`*凭证*` / `*credential*` / `*secret*` / `*密码*` / `*.pem` / `*.key` / `id_rsa*` / `.env*` → `sensitive`；要正文必须 `--allow-sensitive` / `allow_sensitive=true`；`search` 不进片段 |
| §6.3 单次写入上限 512 KiB | 加码：既有目标文件 > `max_open_bytes`（默认 2 MiB）→ `too_large`（与读路径同口径），备份 payload 同限 |
| §6.2 frontmatter 只增改给定键 | 加码：字符串值禁换行/控制字符（`bad_frontmatter`），渲染后再做一次单行断言——防「值里带 `\n` 注入顶层属性 / 提前闭合文件头」 |
| 新增：`--home` 约束 | 拒绝盘根 / `%WINDIR%` / `%ProgramFiles%` / 用户家目录根；非标准位置要 `--allow-external-home` |
| 新增：路径错误面（KB-11 补强） | 空路径 `open` / 目录 → `bad_usage`；NUL 字符 / 设备名（NUL、CON、COM1）/ ADS（`x.md:hidden`）→ `path_invalid`；非 `KBError` 的 `OSError` 也转域错误并落 reject 审计 |
| 新增：遍历面链接 | `tree` / `search` 遇 junction / 符号链接**不列、不下降、不命中**（不只是「显式路径拒绝」） |
| §3.2 备份 | **永不自动清理**（裁决 N2）：`kb doctor` 增备份体积 / 最旧 / 最新；清理只走显式 `fish kb backup prune --keep-days N`（含 `--dry-run`） |
| 交付门槛 | 每条「必须修」都有判据；至少 R1/R3/R6/R10/R15 各 1 条变异自证（实跑 17/17 红，见变异自证 §5） |

---

## 12. 变更记录

| 版本 | 日期 | 改了什么 |
|---|---|---|
| v0.2 | 2026-10-26 | 第二轮：按 `docs/主理人裁决-第二轮修复.md` 落 21 条口径变更（隐私审计 / 深度裁剪 / 敏感名单 / 写身份门槛 / 备份策略等），逐条见上方「第二轮口径变更」节 |
| v0.1 | 2026-10-26 | 首版：B1/B2/B3 需求池 40 条（P0 26 / P1 10 / P2 4）、5 处冲突、18 条待确认；数据模型 2 表 1 视图 + `boards.path` 语义变更；MCP 2 工具 + CLI 2 族 |
