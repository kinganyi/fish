# fish · hooks 运维说明（2026-10-26 · hooks 深度利用 T01–T05）

> 面向：接手这套 hooks 的人（含未来的 AI）。
> 配套：设计/任务分解见 `41-hooks深度利用-实施方案.md`；配置由 `install_hooks.py` 生成，**不要手改**。

---

## 1. 五条链路一览（谁在什么时刻做什么）

| 事件 | DSH 脚本（`~/.dsh/hooks.json`） | WorkBuddy 脚本（`~/.workbuddy/settings.json`） | 共同核心 | 输出 |
|---|---|---|---|---|
| `SessionStart` | `fish_dsh_hook.py` | `fish_session_hook.py`（`inject --auto-presence`） | `cmd_session_start` / `cmd_inject` + `board_auto_checkin` | `additionalContext`（凭证 / 注入包） |
| `UserPromptSubmit` | `fish_prompt_hook.py --host dsh` | `fish_prompt_hook.py --host workbuddy` | `fish.py prompt-push`（候选 / 陈旧性 / 记账） | `additionalContext`（≤2 条记忆） |
| `PreToolUse` | `fish_pretool_hook.py --host dsh` | `fish_pretool_hook.py --host workbuddy` | `fish_activity.find_collisions`（读快照） | `permissionDecision=ask` + 可读原因（仅在 `ask` 模式且宿主开着批准时）；`warn`（**默认**）→ **零输出**但留一次性 pending 标记，由下一个 PostToolUse 递一句 |
| `PostToolUse` | `fish_push_hook.py --host dsh --collect-activity` | `fish_posttool_hook.py --host workbuddy`（采集+绕路+推送同进程） | `fish_activity.handle_post_tool_use` + 既有推送管线 | `additionalContext`（每 N 次）或零输出 |

**每宿主只有一个采集器**：DSH 是 `fish_push_hook.py`，WorkBuddy 是 `fish_posttool_hook.py`。
两个都注册 = 每次工具调用跑两个进程、写两份行、心跳互相覆盖。`install_hooks.py --check` 把这条当硬校验。

### 1.1 matcher 只能是「纯字面备选」（F1：一个字符就能让整份配置作废）

两个宿主的 `PreToolUse` matcher 现在都是**同一串字面备选**：

```
Write|write|Edit|edit|MultiEdit|multiedit|NotebookEdit|notebookedit|ApplyPatch|apply_patch
```

`PostToolUse`（WorkBuddy）那串 `Read|read|Bash|bash|Glob|glob|Grep|grep|…` 同理。

**为什么不能用 `(?i)^(write|edit|…)$`**（2026-10-26 评审 F1，出厂 asar 原样复跑）：

- DSH 的 `parseClaudeCodeConfig` 只对「纯字面备选」（只含 `[A-Za-z0-9_]` 与 `|`）走**字面比较**，
  其余一律当**正则编译**；`(?i)` 在 JS RegExp 里直接 `SyntaxError: Invalid group`；
- `apply()` 被 catch 后 `return` → **四个事件一个都没注册**（连既有的 SessionStart 凭证注入
  与 PostToolUse 采集一起消失），而旧版 `--check` 只查"子串齐备"，照样 exit 0；
- 所以合法形态**只有一档**（`install_hooks.matcher_dialect` 是唯一实现）：
  `literal` = `^[A-Za-z0-9_]+(\|[A-Za-z0-9_]+)*$`，宿主根本不编译正则。
  **一切正则形态（锚定 / 括号 / 内联 flag）一律拒绝**——Round 5/N1 的教训是：
  "锚定正则"档只查字符集，`^(Write|write|Edit|edit$` 字符集完全合法、宿主 parser 却
  `SyntaxError`，而 `--check` 又会 exit 0「通过」。判据用**原串**（不先 strip：宿主按原串
  解释，`" Write|write"` 的前导空格会让字面比较永不命中）。
- 内联 flag 的拒绝是**策略性**的（Y16 更正）：`(?i)` 宿主确实不认；但 `(?i:…)` 在宿主
  Node 24 里**可以编译**——两个宿主的正则语义不可假设，所以本工具不赌，**不是宿主不认**。
  确实需要正则请人工确认宿主方言后再手改配置。
- 判据见 `--check` 的第 4 条硬校验与
  `test_surface_naming.InstallerTest.test_matcher_dialect_contract`。

---

## 2. 依赖（零新增第三方包）

全部只用 Python 3.11 标准库：`sqlite3` / `json` / `hashlib` / `re` / `datetime` / `pathlib` / `os` / `sys` / `subprocess`（仅 prompt hook 调 CLI）/ `argparse`（仅 CLI）。

| 组件 | 依赖 | 说明 |
|---|---|---|
| `fish_surface.py` / `fish_naming.py` | 无 | 纯函数，import ≈1ms（可进 hook 快路径） |
| `fish_activity.py` | `sqlite3` + 上面两个 | 采集/聚合/快照/撞车，import ≈5ms |
| 四个 hook 脚本 | `hook_io` + `fish_activity`（推送时才 `import fish` ≈400ms） | 失败一律零输出 |
| `install_hooks.py` | 标准库 | 只读写 JSON，无网络 |

**不新增**：MCP 工具（仍是 16 个）、第三方包、常驻进程、新表（`tool_activity` 是唯一新表）。

---

## 3. 装配 / 校验 / 回滚

```powershell
cd I:\app\DSH-Home\workspace\learn\大肥鱼生命系统\p0

# 1) 只看差异（不写盘）；校验失败或与目标不一致 → exit 2
python install_hooks.py --check

# 2) 写入两个宿主（先备份 <file>.bak-20261026-hooks，已存在则加序号，绝不覆盖旧备份）
python install_hooks.py --apply

# 3) 回滚（按备份**字节还原**；还原后会立刻校验，坏版本返回非 0）
python install_hooks.py --restore latest

# 4) 演练/测试：只动指定文件
python install_hooks.py --file <某个临时文件> --host dsh --check
python install_hooks.py --file <某个临时文件> --host dsh --apply
python install_hooks.py --file <某个临时文件> --host dsh --restore latest
```

- **配置是"启动读一次"**：改完必须重启 DSH 桌面端与 WorkBuddy 才生效。
- ⚠️ **`latest` 是最近一次备份（可能早于修复）**：回滚前先 `python install_hooks.py --check`。
  还原后工具会自己再校验一次目标文件——matcher 不合契约时**宿主会整份拒绝**（一个事件
  都不注册），此时会响亮打印「会被宿主整份拒绝，请手工确认」并**返回非 0**（Round 5/N2）。
  已知坏备份已人工改名：`hooks.json.bak-20261026-hooks.3.BAD-matcher-regex`（含 `(?i)` 正则形态，
  **不要**用它回滚）；若回滚时被选中，工具会按 N2 逻辑返回非 0 并警示。
- `--check` 的**四条硬校验**：① 各 4 个事件；② 每宿主只有 1 个采集器；③ 写类 matcher
  覆盖大小写两种写法；④ **matcher 是纯字面备选**（唯一合法形态，见 §1.1）——第 ④ 条
  不通过就 `exit 2` 并打印原因，**不许**再用"子串齐备"当合法性。
  另外，**合法但与目标不一致也会 exit 2**（文案说「与目标不一致（会被改写）」），
  退出码与 diff 文案保持一致（Round 5/N3）。
- `--apply` 缺省是**合并**：只覆盖/新增本工具负责的四个事件，`hooks` 层里别人的
  `Stop`/`Notification` 等事件、以及 `settings.json` 的其它顶层键**一个字节不动**
  （序列化保留原行尾：`hooks.json` 是 CRLF、`settings.json` 是 LF）。
  确需整层替换才用 `--apply --force`，此时 `--check`/`--apply` 都会**列出将被删除的事件**。
- 回滚顺序：**先关配置开关（秒级止血，不用重启）→ 再移除 hook（要重启）→ 最后回代码/DB**。
  不要先删表再关开关（会留下半套代码）。

> 为什么改成合并（2026-10-26 评审 L4）：`settings.json` 是**共用文件**，别的插件/别的工具
> 也往同一个 `hooks` 层挂事件。上一版整层替换 → 静默删掉别人的事件，而 `--check` 还写着
> "其它事件保持不变"——文案与实际不符，症状是"某个功能悄悄没了"。

### 立即降级（不重启，秒级止血）

```powershell
python fish.py --root data config set activity.enabled false
python fish.py --root data config set board.auto_presence false
python fish.py --root data config set push.prompt_enabled false
python fish.py --root data config set board.collision_ask off
```

| 开关 | 关掉之后 |
|---|---|
| `activity.enabled=false` | 不落动作行、不刷快照（采集整体停） |
| `board.auto_presence=false` | 会话开场不再自动签到（看板回到手工模式） |
| `push.prompt_enabled=false` | prompt hook 仍被触发，但零输出 |
| `board.collision_ask=off` | PreToolUse 什么都不做（连审计都不写） |
| `board.collision_ask=warn`（**默认**） | 不阻塞：写 `pretool-asks.jsonl` 审计 + 落一次性 pending 标记，由**下一次 PostToolUse** 给模型递一句（只一次、15 分钟内有效） |

---

## 4. 重启后**先看这四件事**（不是先看新功能）

部署后第一件事是验证"原本好的没坏"，然后才看新能力：

0. **先跑一次 `python install_hooks.py --check`（两宿主）**——它现在会校验 matcher 的
   **宿主方言契约**（§1.1）。2026-10-26 评审 F1 的教训：matcher 写成 `(?i)…` 会让整份
   配置在宿主侧解析失败（评审用出厂 asar 复跑得到 `apply()` catch → 一个事件都不注册），
   而旧版 `--check` 只查子串，照样绿。**任何"某个事件突然不触发了"的排查，
   第一条永远是「matcher 是否满足宿主方言」**。

1. **既有 `PostToolUse` 心跳还在更新**：
   `data/_audit/hooks/heartbeat-dsh-PostToolUse.json`（WorkBuddy 侧 host 是 `workbuddy`）
   —— mtime 应该是刚刚，内容里应带 `tool_name`（本机 2026-10-26 05:42 实测：`tool_name: "edit"`）。
   ⚠️ **SessionStart 不写心跳**（Y7 更正）：两个宿主的 SessionStart 脚本都没有心跳调用，
   所以 `heartbeat-*-SessionStart.json` **本来就不该存在**——别去找它，更别把它当故障。
   "SessionStart 到底跑没跑"看这两样：
   - WorkBuddy：`data/_audit/hook-calls.jsonl`（每会话一行，含 `injected N chars` / 身份 /
     失败原因；126 行实测）；
   - DSH：只在 `FISH_HOOK_DEBUG=1` 时写 `data/_audit/hook-dsh.jsonl`（`issued …` /
     `session_start_failed …`）；不开调试就只能看**会话里有没有收到注入块**。
2. **另外三个事件都有心跳**：`heartbeat-<host>-UserPromptSubmit.json`、
   `heartbeat-<host>-PreToolUse.json`、`heartbeat-<host>-PostToolUse.json`。
   读心跳不能只看 mtime，**要看内容里的字段**（本机 2026-10-26 实测）：

   | 文件 | 内容要点 | 说明 |
   |---|---|---|
   | `heartbeat-workbuddy-PreToolUse.json` | `tool_name: "Edit"`、`session: 81347d72`（03:49） | 真实工具调用 → WorkBuddy 用户级 `PreToolUse` **是支持的**（Y8 证据） |
   | `heartbeat-workbuddy-UserPromptSubmit.json` | `prompt_len: 1814`、`session: 31e0b745`（03:47） | 真实提示词 → 用户级 `UserPromptSubmit` **是支持的** |
   | `heartbeat-dsh-PostToolUse.json` | `tool_name: "edit"`、`payload_len: 571`（05:42） | DSH 的 PostToolUse 正常在跑 |
   | `heartbeat-dsh-PreToolUse.json` / `-UserPromptSubmit.json` | **`bad_payload: true`**（02:26，此后没更新） | 那是**手工演练**喂坏 JSON 留下的，**不能当作"事件真的触发过"**；真实写操作没有对应心跳 = 这两个事件当前没在跑。**F1 已给出根因**：`PreToolUse` 的 matcher 写成 `(?i)^(…)$`，宿主按正则编译失败（评审用出厂 asar 复跑：`parseClaudeCodeConfig` 抛 `SyntaxError` → `apply()` catch 后 return → **no hooks registered**）。修法见 §1.1（已改用纯字面备选并重新 apply）；`UserPromptSubmit` 没有 matcher，它与 `PreToolUse` 一起缺失的原因只能是"整份配置被拒"或"当前进程用的是 apply 之前的配置"（配置启动读一次）——两者都指向同一个动作：**重启后按本表复核**。 |

   判据仍是心跳：**没有心跳（或只有 `bad_payload` 的演练行）= 那个事件没在真实调用里触发**。
3. **动作流有行**：`python fish.py --root data activity stats --days 1`
4. **看板有自动行**：`python fish.py --root data board show --project <项目>`
   —— 应该能看到一行 `（自动登记：会话开始…）`，且会话列是**可读名**而不是 ULID。

---

## 5. WorkBuddy 的事件支持（按实测回填，Y8 更正）

**2026-10-26 实测结论：WorkBuddy **用户级** `settings.json` 支持 `UserPromptSubmit` 与
`PreToolUse`**——证据不是"文件存在"，而是**心跳内容里有真实的工具名/提示词长度**：

| 文件 | 内容要点（本机 2026-10-26） |
|---|---|
| `_audit/hooks/heartbeat-workbuddy-UserPromptSubmit.json` | `prompt_len: 1814`、`session: 31e0b745`（03:47:34） |
| `_audit/hooks/heartbeat-workbuddy-PreToolUse.json` | `tool_name: "Edit"`、`session: 81347d72`（03:49:08） |
| `_audit/hooks/heartbeat-workbuddy-PostToolUse.json` | `payload_len: 1739`、`tool_name: "Edit"`（03:49:10） |

所以**旧版"24h 无心跳就从 WorkBuddy 配置里移除该事件"的指令作废**（那是基于
"用户级未实测"的假设写的，实测已经推翻了前提）。现在的口径：

- 认不认某个事件，**不影响**其它事件：不支持的事件键静默忽略（"配置即忽略"）；
- ⚠️ **但"配置即忽略"只对「不支持的事件」成立**（评审 F3）：**非法的 matcher 会让整份
  配置被拒**——一个事件都不注册（不是"那个事件不生效"）。这两种失败完全不同，
  混在一起会把"配置写错了"误判成"宿主不支持"。判据：`--check` 的第 ④ 条硬校验；
- **判据是心跳**：真跑过就会写 `heartbeat-<host>-<事件>.json`；
- 心跳缺失时按顺序查三件事，而不是直接删配置：
  1. **matcher 是否满足宿主方言**（`install_hooks.py --check` 已做成契约判据：
     只允许「纯字面备选」；锚定/括号/内联 flag/`.*`/`\b` 一律非法
     → 见 §1.1）。旧版这里写的是"覆盖了真实工具名"，那只查得到"写错大小写"，
     查不到"宿主编译失败"——F1 就是从这个缝里漏过去的；N1 又把"锚定正则"档收掉
     （它只查字符集、不查可编译性）；
  2. 宿主是否重启过（配置启动读一次）；
  3. 该事件在这个宿主上是否根本没有触发点（例如没有一次写操作）。
- 只有在**上面三条都排除之后**才考虑降级，并把结论按 §9 的格式记一行（日期+事件+现象+结论）。
- 降级做法（不要手改 JSON）：改 `install_hooks.py` 的 `desired_hooks("workbuddy")` 后
  `python install_hooks.py --apply --host workbuddy`。

---

## 6. 心跳 / 审计文件（都在 `data/_audit/`）

| 文件 | 内容 | 隐私 |
|---|---|---|
| `hooks/heartbeat-<host>-<event>.json` | `ts` / `event` / `host` / 工具名 / session 哈希 / payload 长度 | **只留长度与哈希**：任何 >32 字符的字符串都变成 `{len, sha8}`；`session` 更严——**恒为 8 位十六进制哈希**（`fish_activity.session_digest`），会话键原文不落盘 |
| `_audit/detours.jsonl` | 每会话首次绕路一行：`ts/tool/path/session/host`（字段封闭） | `path` 只接受**路径类**字段；Bash 折成 `bash:<命令名\|len=N>#<sha8>`——命令名必须是**固定命令表**（`_SAFE_COMMANDS`：python/git/grep/curl/…）里的常量，表外一律 `len=N`（`command` 只参与匹配，**永不落盘**——设计 §10.3；QA Round 2 · P2：字符集白名单挡不住 `fsh_EDGECASE…` 这类凭证形态，只有查表挡得住） |
| `hooks/active_surfaces.json` | 快照：谁声明/实际占了哪块（相对 + 绝对两列路径） | 不含内容、不含 command、不含 prompt |
| `hooks/pretool-asks.jsonl` | 每次"决定要问/要提醒"一行：`ts/session_key/target/mode/decision/hits` | 只含路径与分钟数 |
| `hooks/warn-pending-<会话哈希>.json` | `warn` 模式**欠模型的那一句提醒**：`ts/session/reason/target/hits/delivered_at` | 只含路径与分钟数；`delivered_at` 非空 = 已递过一次（**只递一次**，过 15 分钟不递）。同会话**覆盖写**：后到者覆盖（保留最新一条），`ts` 更旧的乱序写入被丢弃（F10）；`delivered_at` **写不进盘时这一句不递**，标记留着下次再试（F9：宁可这次不提醒，也不许每次 PostToolUse 重递一遍） |
| `hooks/state.json` | 快照节流时间戳 | — |
| `counters/<event>-<session>.n` | 频率闸计数（BOM 容错） | — |
| `hook-state/prompt-<session>.json` | prompt hook 的**prompt 哈希**与时间戳 | **原始 prompt 绝不落盘** |
| `hooks/hook-dsh.jsonl` | `FISH_HOOK_DEBUG=1` 时 SessionStart 的调试行 | 不含 token |

误报审计：`pretool-asks.jsonl` 里 `decision=warn` 的行就是"本可以弹窗但没弹"的记录；
`fish activity stats --days 1` 里的 `撞车询问 N 条` 是同一口径的汇总。
**首日误报 >1 次**就把 `board.collision_min_segments` 调大或直接 `off`。

### 6.1 事实 vs 缓存（Y9：别把缓存当真相）

| 想回答的问题 | 看哪里 | 不要看哪里 |
|---|---|---|
| 某人**真的**在写哪块 | `tool_activity`（读时聚合 = `fish_activity.auto_surfaces`） | `presence.auto_surface` |
| "刚才最近一次"快捷读数 | `presence.auto_surface / auto_seen_at / auto_source` | — |
| 撞车判定 | 快照 `active_surfaces.json`（`declared_abs` / `auto.dir_abs`）+ `path_hit` | `presence` 的展示列 |

- `presence.auto_*` 三列是**缓存**：由 `refresh_presence_cache` 节流刷新（默认 20s 一次），
  所以它最多比事实表旧 20 秒；**撞车判定、推送候选、看板「实际在动」一律读事实表**。
- 快照里的 `auto.dir_abs` **只来自事实列**（`dir_of(path_abs)`，2026-10-26 评审 L1 修复）：
  A 板的动作在 B 项目、或未映射项目（root 板）时会话，都不会再被"看板项目根"重算成不存在的路径。
- **未映射项目 / 根板会话**：`presence` 行 `scope='root'`，缓存照刷（缓存只看公民，
  与项目无关）；但它们**没有项目根**去解析人写的相对面 → 快照 `declared_abs` 会保留相对串
  （`p0/fish.py`），撞车判定走 `path_hit` 的保守路径（≥2 段才算命中，`p0` 这种 1 段面直接放过）。
- `path_dir`（事实表的展示列）是相对**写入者那一刻的基准**算出来的，**不能拿它反推绝对路径**；
  机器判定只认 `path_abs`。

---

## 7. 真人验证（部署后，需要两个会话）

> ⚠️ **先看这一条**（2026-10-26 评审 L3）：**宿主开着 `approvals disabled` 时，`ask` = 拒写**——
> 需要批准的动作会被自动拒绝。所以在那种宿主上验证撞车能力，**必须用 `warn`**：
>
> - `warn`：不阻塞，但**模型能看见**——PreToolUse 留一次性标记，下一次 PostToolUse 递一句
>   `additionalContext`（判据：PreToolUse 零输出 + `_audit/hooks/warn-pending-*.json` 出现 +
>   紧接着的 PostToolUse 输出含「撞车提醒」+ 同一个标记只递一次）；
> - `ask`：只在**确认宿主开着批准**时用来验证弹窗；否则你验证的是"写被拒绝"，不是"撞车提示"。

1. 新开两个会话 A / B（都带 fish 凭证）。
2. A 写 `p0/_collision.txt`（或用 Bash `echo x > p0/_collision.txt`）。
3. 60 秒内 B 写**同一个文件**：
   - `warn`（默认）→ B **不被打断**，但会收到一句「撞车提醒」（同一次工具调用的
     PostToolUse 追加），且 `pretool-asks.jsonl` 多一行 `decision=warn`；
   - `board.collision_ask=ask` **且宿主开着批准** → B 收到一次批准请求，原因可读
     （名字/项目/路径/分钟数）。
4. B 立刻重试同一文件：60 秒冷却内不再问、也不再递（审计也不加行）。
5. `python fish.py --root data config set board.collision_ask off` → 再写同一个文件，
   什么都不发生（不输出、不审计、连 pending 标记都不写）。

---

## 8. 配置键（本批新增 19 个，全部在 `fish.py CONFIG_DEFAULTS`）

```
activity.enabled=True              activity.retention_days=14
activity.aggregate_window_min=30   activity.snapshot_min_interval_sec=20
activity.snapshot_max_age_sec=3600 activity.presence_cache_min_interval_sec=20
board.auto_presence=True           board.active_window_sec=300
board.collision_window_min=30      board.collision_ask=warn   # off|warn|ask
board.collision_ask_cooldown_sec=60 board.collision_min_segments=2
push.prompt_enabled=True           push.prompt_budget_tokens=120
push.prompt_max_items=2            push.prompt_min_interval_sec=90
push.prompt_min_relevance=0.6      push.prompt_stale_floor=0.35
push.prompt_exclude_hours=24
```

`board.collision_ask` **为什么默认 `warn` 而不是 `ask`**（用户 2026-10-26 拍板 + 主理人决策）：
当前宿主 approvals disabled 时 `ask` 会被**自动拒绝** → "撞车提示"变成"撞车拒写"，正好违背它的目的。
`warn` 不阻塞，靠 `activity_overlap` 推送 + 看板「观察到的重叠（推断）」提醒；
只有显式改成 `ask`（且宿主确实开着批准）才真的弹窗。

---

## 9. 实测记录（部署后由运维/用户补写，勿删旧行）

| 日期 | 宿主 | 事件 | 观察 | 结论 |
|---|---|---|---|---|
| 2026-10-26 | DSH | 4 个事件 | 配置已 `--apply`（待重启） | 待重启后按 §4 复核 |
| 2026-10-26 | WorkBuddy | `UserPromptSubmit`/`PreToolUse` | 用户级支持性未实测 | ~~24h 无心跳则移除（§5）~~ **已作废，见下一行** |
| 2026-10-26（评审 Y8 更正上行） | WorkBuddy | `UserPromptSubmit`/`PreToolUse` | 心跳内容是真的：`PreToolUse` → `tool_name: "Edit"` / `session 81347d72`（03:49:08）；`UserPromptSubmit` → `prompt_len: 1814` / `session 31e0b745`（03:47:34）；`PostToolUse` → `payload_len: 1739`（03:49:10） | **用户级支持**；"24h 无心跳即移除"作废（§5 已改） |
| 2026-10-26（评审 Y8 附注） | DSH | `PreToolUse`/`UserPromptSubmit` | 两个心跳文件只有 `{"bad_payload": true}`（02:26:32 / 02:26:33，此后没更新）；而 `PostToolUse` 05:42 仍在更新（`tool_name: "edit"`） | 那两个 02:26 的行是**手工演练**，不算真实触发；真实写操作期间 PreToolUse 没心跳 = **当前进程未装配** → 需一次重启复核（重启后应看到 `tool_name` 字段而不是 `bad_payload`） |
| 2026-10-26（评审 Y7） | 两宿主 | `SessionStart` | 没有 `heartbeat-*-SessionStart.json`，脚本里也没有心跳调用（`fish_dsh_hook.py` 仅 `FISH_HOOK_DEBUG=1` 写 `hook-dsh.jsonl`；`fish_session_hook.py` 写 `hook-calls.jsonl`，126 行） | **设计如此**：SessionStart 的判据是 `hook-calls.jsonl` / 注入块，不是心跳（§4 已改） |
| 2026-10-26（评审 L3） | 两宿主 | `warn` | 冒烟：PreToolUse 零输出 + `warn-pending-7e7a80f9.json` 出现；随后 PostToolUse 递一句「撞车提醒」；再跑一次为空；`off` 无标记 | warn 的可见性已打通，**不需要**改 `ask` 才验证 |
| 2026-10-26（评审 L4） | DSH | 配置写入 | 在 `hooks.json` 副本里插 `Stop`/`Notification` → `--apply` 后两者仍在、顶层 `_comment/_created/_purpose/_rollback` 也在；`--restore latest` 字节哈希一致 | 缺省合并；`--force` 才整层替换并列出将删除的事件 |
| 2026-10-26（评审 F1 修复） | DSH | `PreToolUse` matcher | 旧串 `(?i)^(write\|edit\|…)$` → 评审用出厂 asar 的 `parseClaudeCodeConfig` 复跑：JS RegExp `Invalid group` → 抛错 → `apply()` return（**no hooks registered**）。改为纯字面备选后：`--check` 两宿主 `EXIT 0`；Node 用「字面分支 + `new RegExp()`」两条路各跑一遍，字面分支覆盖 10 个工具名、正则分支不再被走到 | matcher 只许 `literal`（Round 5/N1 起为**唯一**合法形态）；`--check` 第 ④ 条契约判据挡住回归 |
| 2026-10-26（Round 5 · N1–N3） | 两宿主 | `--check` / matcher / 回滚 / 退出码 | **N1**：临时副本喂 `^(Write\|write\|Edit\|edit$`、`^(Write\|write\|Edit\|edit))$`、`(?i)^(…)$`、前导空格四种坏 matcher → 全部 `exit 2` 且打印「宿主方言契约」；合法字面 → `exit 0`（`_audit/r5-n1-counterexamples.log` 6/6）。**N3**：合法但 matcher 多一个 `read` → `exit 2` + 「与目标不一致（会被改写）」。**N2**：坏备份 `--restore latest` → 非 0 + 「会被宿主整份拒绝，请手工确认」；合法备份 → 0 + 「校验通过」。真配置 `--check` 两宿主 `EXIT 0`（`_audit/r5-check-both-hosts.log`）；全量 **655 条 OK**（`_audit/r5-unittest-final.log`）；源码级变异 `_audit/变异自证-N1.log`（加回锚定档 → 判据必红 → 字节还原 → 复跑绿） | 锚定档已取消（只认纯字面）；`--check` 退出码与 diff 文案一致；回滚后自动校验（§1.1 / §3） |
