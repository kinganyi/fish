# fish 现场 · 首屏（一期 · 只读）

把 `index.sqlite` 里"AI 对系统做了什么"和"系统对 AI 说了什么"铺成一条**改不了**的时间线，
让主人扫一眼就知道今天这套东西有没有在动、动在哪、谁在动。

- **四栏**：52px 顶级页签窄条 ｜ 200px 切片器 ｜ 中 ≥55% 流水 ｜ 320px 判断面（两个页签）
- 数据：真实库只读（`mode=ro` + `PRAGMA query_only=1`），**没有任何示例数字**
- 技术栈：Vite + React 19 + TypeScript + Tailwind 4 + zustand 5 + `@tanstack/react-virtual`

设计与验收口径：`docs/96-工作台-产品规格-20261123.md`、`docs/97-工作台-技术设计-20261123.md`
（这两个**文件名**里的「工作台」是历史命名，docs 只读不改；界面上的页签名是「现场」）。

---

## 〇、页签名与栏位（用户 2026-11-23 裁决）

**页签名叫「现场」，数据契约名沿用 `workbench`。** 读口 schema 仍是 `fish-workbench/1`
（契约锚点：改名要连带改测试、外部脚本与历史读数，收益为零）。所以：界面/标题/aria-label
里出现的是「现场」，JSON 里出现的是 `fish-workbench/1`——两者不是笔误。

**是四栏，不是三栏。** 最左那条 48–56px 的窄条是**产品一级导航**（顶级页签容器），
一期只有「现场」一页，所以里面只有一个可点项：

| 栏 | 宽 | 职责 |
|---|---|---|
| 顶级页签窄条 | **52px** | 页面清单（`src/pages.ts`）驱动；**只画已建好的页**，未做的不画 |
| 切片器（左） | 200px | 项目 / 今日板 / 当前切片状态行 |
| 流水（中） | `minmax(0, 1fr)` | 五源合流时间线 + 折叠 |
| 判断面（右） | **320px** | 页签一「要你做的 / 整体」、页签二「这一条」 |

窄条按"将来会有多个页签"设计：**加一页 = 往 `src/pages.ts` 的 `PAGES` 加一条数据**，
`NavRail` 按数组渲染，没有写死任何单个按钮。当前页的选中态是**三件叠加**（左侧 2px 竖线 +
底色 + 文字色/字重），不是纯色块——52px 宽度里只靠底色几乎看不出来。

### 加窄条后重算的宽度

| 视口 | 固定列 52+200+320 = 572 | 中栏 | 中栏占比 | 横向溢出 |
|---|---|---|---|---|
| 1280×720 | 572 | **708** | 55.3% | 无 |
| 1440×900 | 572 | **868** | 60.3% | 无 |

右栏取 320（设计允许 320–400 的**下限**），把省下的 40px 全让给正文区——96 §5.4 的
"中栏 ≥55%"在 1280 下仍成立，且中栏仍是四列里最大的。两条读数由
`p0/tools/shoot_workbench.py` 逐张打印（`scrollWidth` vs `clientWidth`、四栏实际像素、
窄条页签数），截图只能人眼看，读数才能机检。

更窄（<1280）时不缩栏宽、不折栏，整页横向滚动（`min-w-[1280px]`）：折栏等于砍掉
"左筛中、中改右"这个三角。**本期不覆盖 <1280**，如实写在 §六。

---

## 一、启动（两种拓扑，别混）

**验收/交付拓扑 = UI 静态托管 + API 独立进程**（F-2 拍板）。读口挂了，页面还能显示
「读口没有起来 + 启动命令 + 重试」——这是判据 A-2 能在浏览器里被验收的前提。

| | 验收 / 交付（推荐） | 开发便利 |
|---|---|---|
| 读口 | `python p0/fish_workbench_api.py`（默认 `127.0.0.1:8790`，**不带** `--dist`） | `python p0/fish_workbench_api.py --dist ui/dist`（同源发静态） |
| 前端 | `cd ui; npm install; npm run build`<br>`python -m http.server 8799 --bind 127.0.0.1 --directory ui/dist` | 同上构建；或不构建跑 `npm run dev -- --port 5199`（Vite 把 `/api` 代理到 8790） |
| 打开 | `http://127.0.0.1:8799/?api=http://127.0.0.1:8790` | `http://127.0.0.1:8790/` 或 `http://127.0.0.1:5199/` |
| 读口挂掉时 | 页面仍能打开，显示整屏空态（启动命令 + 重试） | 浏览器直接 `ERR_CONNECTION_REFUSED`（同进程，页面跟着死） |

前端**不写死同源**（`src/api/client.ts`）。API 基址优先级：

1. 地址栏 `?api=http://127.0.0.1:8790` —— 静态托管时用这个；
2. 构建期 `VITE_FISH_API_BASE` —— `npm run build` 时定死（打包给宿主壳层用）；
3. 同源相对路径 —— 读口 `--dist` 同源发静态、或 Vite dev 的 `/api` proxy（天然满足）。

### 端到端验证：杀读口后必须出空态（不是浏览器报错页）

```powershell
cd I:\app\fish
# ① 构建一次（源码变了才需要）
cd ui; npm run build; cd ..

# ② 静态托管（独立进程，不依赖读口）
Start-Process python -ArgumentList '-X','utf8','-m','http.server','8799','--bind','127.0.0.1','--directory','ui\dist'

# ③ 读口单独起（不带 --dist）
Start-Process python -ArgumentList '-X','utf8','p0\fish_workbench_api.py','--port','8790'

# ④ 打开带 ?api= 的地址：应出四栏读数
#    http://127.0.0.1:8799/?api=http://127.0.0.1:8790

# ⑤ 杀掉读口进程，刷新同一个地址：必须是整屏「读口没有起来 + 启动命令 + 重试」
Get-NetTCPConnection -LocalPort 8790 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

截图（`ui/screenshots/`，**文件名带时间戳**，全部晚于当前 `dist` 构建）：

| 文件 | 内容 |
|---|---|
| `01-main-*.png` | 1440×900 默认四栏 |
| `02-1280x720-*.png` | 1280×720 下限，不缩栏不折栏、无横向溢出 |
| `03-static-apidown-*.png` | 静态托管 8799 + 读口指到空端口 → 整屏空态 |
| `04-project-czhy-*.png` | 左栏点 CZHY → 中栏每条项目都是 `CZHY` |
| `05-ledger-deeplink-*.png` | 页签二深链：这一条 / 这个人 / 他的账本 |

重拍（源码变了才需要，**别手搓命令行参数**）：

```powershell
cd I:\app\fish
cd ui; npm run build; cd ..
python -X utf8 p0\tools\shoot_workbench.py --api http://127.0.0.1:8790 --static http://127.0.0.1:8799
# 脚本会：把旧 png 挪进 screenshots/_legacy-predist/ → 重拍 5 张（带 HHMM 时间戳）
#        → 逐张打印 scrollWidth/clientWidth、四栏实际像素、窄条页签数
```

**核对"截图晚于 dist 构建"**（评审原文：三张截图早于 dist 5–14 分钟）：

```powershell
cd I:\app\fish\ui
node scripts/smoke.mjs      # S6 这一条就是它：任一张 png 比 dist 旧 → FAIL
```

不做的事：不装 Electron、不写库、不动 `fish.py`、不动 `fish_ui.py`。

---

## 二、六个端点（契约见 97 §C）

| 端点 | 作用 | 缓存 |
|---|---|---|
| `GET /api/facets?win=24h` | 左栏：项目（含「未归属」）+ 今日板，各带 `lastTouchedAt` 与计数 | 10 s |
| `GET /api/stream?win=&scope=&limit=&since=&before=&wsince=` | 五源合流（工具 / 登记 / 推送 / 记忆事件 / 系统事件），单键 `sk` 排序 + 折叠 | 无（计数 10 s） |
| `GET /api/stats?days=1&top=5` | 右栏页签一：在跑/最近来过、未闭合档期、待确认、推送、工具、公民、热搜 | 60 s |
| `GET /api/entry?id=tool:40423` | 右栏页签二：这一条 + 这个人 + 他的账本 | 30 s |
| `GET /api/meta` | **读口自述**：`schema` / `schemas[]` / `capabilities[]` / `db{index,corpus}` / `readOnly:true` / `serverPid` | 无 |
| `GET /api/health` | 轻量探活（读口是否在跑） | 无 |

`scope` 单选：`all` / `project:<名>` / `project:__none__`（未归属）/ `board:<id>`。
每个响应带 `schema:"fish-workbench/1"`、`asOf`、`availability.state`、`sources[]`（表名/ok/rows/error/ms）；
错误信封 `{ok:false, code, hint, action}`。`/api/meta` 是**唯一不套信封**的端点（它是自述，
套一层 `data` 只会让外部脚本多剥一层）。

**`/api/meta` 的 `capabilities` 是"已实现"，不是"计划"**：`entry/facets/health/meta/stats/stream`。
它非空本身就是判据（`verify_v4.py` 的 V1），所以别在这里写将来的端点。

### 翻页的窗口起点：`wsince`（R-01 残留）

`/api/stream` 默认按 `win` **现算**窗口起点（`now - 24h`，滑窗）。翻页时如果把第一页响应里的
`window.since` 原样带回（`&wsince=2026-11-22T16:00:00Z`），整段翻页就钉在**同一个** since 上。

为什么必须有它：滑窗下第 1 页与第 24 页的 `since` 差几秒，末端的行会滑出窗口——
「本刀 N 条」与可达行数于是差 1–2 行。那不是丢行，是**窗口在动**。
（`streamcount` 缓存键里同时含 `since`：键里不带它，10 秒 TTL 内会复用另一个 since 的计数，
症状一模一样。两处都做了，因为它们是**两个不同的错因**：一个错在窗口滑、一个错在缓存串。）

界面上：`store/workbench.ts` 把第一页的 `window.since` 记进 `winSince`，`loadMore` / `pollNew`
每次都带上；换切片或换窗口时清空。

**宿主一律取 `session_key` 前缀**（`codebuddy#958a` 这类）。库里 `tool_activity.host` 列与它不一致，
所以**只在前缀上判定**；口径说明在「宿主」行尾的 ⓘ 悬停提示里，不当正文给用户看。

**记忆事件的项目归属**取 `code_index`（`events.subject.id` = code）。`/api/facets` 的计数与
`/api/stream` 的作用域过滤用**同一个** `scope_where()`——所以"本刀 N 条"与翻到底的行数一致。

**作用域谓词只有一处定义（R-16）**：`scope_where()` 返回四源各自的 `(WHERE 片段, 参数)`，
`_branch_selects`（取行）与 `_count_sql`（计数）都只从这里取。判据是**改一处即全变**：
`verify_v4.py` 的 V3 把 `scope_where` 的项目谓词整段摘掉，断言「计数与行数**一起**变、
且变异后仍彼此相等」——如果哪一处又自己写了一份谓词，行会变多而计数不变，这条就红。

---

## 三、空态四句 + 「缺」与「错」（判据 A-10 / R-08）

| 场景 | 文案落点 |
|---|---|
| 读口没起来 | 整屏：`codes.unreachable`（无任何读数字，**不显示骨架数字**） |
| 库为空 | 每块：`codes.emptyDb`（不写 0） |
| 筛选后无结果 | 中栏：`codes.filteredEmpty`，**回显筛选条件** + 「清除筛选」 |
| 窗口内无行为 | 中栏：`codes.windowEmpty` + 「看更早（七天）」 |

四句全部在 `codes.json`，是唯一真相；`0` 是读数、「空」是状态、「缺」是能力、「判不到」是证据不足，四者文案不同。

**「缺」与「错」是两件事，四条链各自分开**（`facets` / `stream` / `stats` / `entry` 各存一份
`availability`）：

- `missing`（缺表 / 缺库）→ 说「缺 `<cap>`（不是读口没起）」，**不给重试按钮**——重试一万次也不会好；
- `error`（这次读失败）→ 才给「确认读口在跑，然后重试」。

左栏的「空库」判据与中栏**同一份**（`isEmptyDb(facets, stats)`：窗口里 0 行 **且** 累计也是 0）。
原先是两份（左栏只看窗口），于是"停了一整天再打开"时左栏说"这个库还是空的"、中栏说
"这个时间窗里没有行为"，同一屏两句话互相打脸（QA K1）。

---

## 四、自己验（判据必须能红）

```powershell
cd I:\app\fish
# ① 修复轮回归（QA 的 F-1~F-6）：14 条；带 --api 再多 4 条 HTTP 面 = 18 条
python -X utf8 p0\tools\verify_workbench_fixes.py --api http://127.0.0.1:8790

# ② 产品判据（docs/96 §十 的 A-1~A-13）：13 条 + 13 条变异自证
python -X utf8 p0\tools\accept_workbench.py --api http://127.0.0.1:8790 --static http://127.0.0.1:8799

# ③ v4 收口新增判据（R-08/R-09/L1/R-16/四栏）：5 条 + 5 条变异自证
python -X utf8 p0\tools\verify_v4.py --api http://127.0.0.1:8790 --static http://127.0.0.1:8799

# ④ F-7 的「每个 state 至少被构造一次」+ F-8 的「partial 已整条删除」证据
python -X utf8 p0\tools\accept_workbench.py --api http://127.0.0.1:8790 --static http://127.0.0.1:8799 --f78

# ⑤ 产物冒烟（构建新鲜度 / 空态四句在包里 / 无内部口径与旧页签名 / 状态构造点 / 截图晚于 dist）
cd ui; npm run smoke; cd ..

# ⑥ 单条判据故意破坏 → 必须红（评审要求："故意破坏一条 → 必须红"）
python -X utf8 p0\tools\accept_workbench.py --api http://127.0.0.1:8790 --static http://127.0.0.1:8799 --mutate a08
```

`accept_workbench.py` 的 A-1~A-13 **每条都有变异自证**：先把断言跑绿，再把同一断言喂给一个
已知为坏的输入（猴子补丁 / 假读口 / 构造字符串），必须红；红不了记 `MUTATION-FAIL`（不算 PASS）。
它的 DOM 类判据（A-2/A-7/A-11 的 DOM 半边/A-12）走无头 Edge + CDP 取 `document.body.innerText`——
理由：本机 Edge 158 的 `--dump-dom` 输出 0 字节（实测两次），而截图只能人眼看、不能机检。

`verify_v4.py` 的五条（同样每条都配变异）：

| # | 判据 | 变异（必须红） |
|---|---|---|
| V1 | `/api/meta` 200 + 六字段齐 + `capabilities` 非空 + `readOnly:true` | 打不存在的端点；`capabilities:[]`；`readOnly:false` |
| V2 | 缺表副本下页面出现「缺 /api/stream」「缺 /api/stats」且**整屏无「重试」** | 换成错误态页面（有「重试」） |
| V3 | 作用域谓词单源（`scope_where` 里各 2 处、函数外 0 处、两处调用） | 摘掉 `scope_where` 的项目谓词 → 计数与行数必须一起变 |
| V4 | 源码 `active:` 命中 ≥6 且 5 个可点文件都有，构建产物里有 `:active` 变体 | 同一段计数喂给 `codes.json`（0 命中） |
| V5 | 窄条页签 == 1、名字「现场」、有选中态 | 把"全页页签数"（3）当窄条数 |

`verify_workbench_fixes.py` 的每条判据同样先跑基线（绿）再跑变异（必须红）；变异只翻运行期键，
不改源码。四条能红的变异：

```powershell
# F-1 深翻页：把游标放回并集外（旧实现）→ 翻到空页只覆盖约 17.8%，断言必须红
$env:FISH_WB_CURSOR_PUSHDOWN="0"; python p0\tools\verify_workbench_fixes.py --child paging --db I:\app\fish-data\data\index.sqlite
# F-4 关掉推送折叠 → 项目视图首屏 push 占比回升、并出现连续同 code 未折叠
$env:FISH_WB_FOLD_PUSH="0"; python p0\tools\verify_workbench_fixes.py --child pushfold --db I:\app\fish-data\data\index.sqlite
# F-5 改回 payload.project → 项目视图 memory 类归零
$env:FISH_WB_EVENT_PROJECT="payload"; python p0\tools\verify_workbench_fixes.py --child memory --db I:\app\fish-data\data\index.sqlite
# D2 关掉折叠（工具+推送一起关）→ fold.groups 回到 0
$env:FISH_WB_FOLD_MIN="9999"; python p0\tools\verify_workbench_fixes.py --child foldmin --db I:\app\fish-data\data\index.sqlite
```

**性能**（本机 2026-11-23，5 次取中位）：`/api/stream?win=24h&limit=500` **约 76–82 ms**
（目标 <150；`limit=40` 约 23–42 ms）、`/api/facets` ~4 ms、`/api/stats` 缓存命中 ~4 ms。
三条连接级只读 PRAGMA（`cache_size=64MB` / `mmap_size=256MB` / `temp_store=MEMORY`）把同一查询的
中位从 45.2 ms 压到 20.1 ms（实测交替跑 7 次），**不改库**。

---

## 五、键盘、深链与时间列

`/` 聚焦切片器 · `↑ ↓ / j k` 行走 · `Enter` 选中并切页签二 · `Esc` 一步返回（清选中 + 回页签一）
· `1 / 2` 切页签（不改选中）· `g / G` 栈顶/栈底 · 左栏点一次筛、再点一次取消。

深链：`#/w?scope=project:CZHY&win=24h&sel=tool:40423&tab=ledger`。
刷新后「切片 / 窗口 / 选中 / 页签」四件事同时复现；`sel` 指向不存在的 key → 页签二显示"这条已不在当前窗口里"，不是白屏。
轮询取到新条目时**绝不自动滚顶**，只在顶部显示「N 条新行为（回到最新）」。

**时间列默认 `HH:mm`，悬停才展开 `HH:mm:ss`**（`components/Time.tsx`，纯 CSS、不进 state）：
等宽字体下常显秒会把同行的类型/项目挤成省略号，而扫读要的是"几点几分"。
页签二的「时间」行同理（`MM-DD HH:mm` → 悬停 `MM-DD HH:mm:ss`）——原先是 `stampOf` 与 `clockOf`
并排渲染，屏幕上出现 `10-25 19:19 19:19:59` 这种同一分钟的重复（QA K3），现在只有一条。

**按下态**：可点行 / 按钮 / 页签 / 折叠条都带 `active:` 变体（`active:bg-n3` 一类），
点在按下那一刻就有反馈，不等到松手才变。判据 V4 数源码命中数并检查构建产物里真的有
`:active` 规则——只写在源码里、Tailwind 没生成，等于没有。

---

## 六、已知边界（如实写，不装样子）

1. **二期才有**：Electron 壳、写入动作（签到/下板/闭合/推送反馈）、对话原文、全局搜索、主题开关。
2. **折叠条不跨页被切开**（按实测改写，QA K4）：服务端按"每个分支自己的完整性边界"取页，
   边界之上的折叠组才进本页，不够就补页——所以**一组不会只显示一半**、下一页也不会把它当新组重折。
   实测（QA 第二轮独立复算）：`scope=all / limit=40` 70 页、项目视图 9 页，页边界上"同一逻辑段
   续接"**0 处**；281 个折叠组 `count == len(items)` **全等**、`truncated=0`；原始行 key 去重后
   `3,645 == rawSum`。旧文案说"跨页会被切开"，把自己说保守了，已按实测改正。
   ⚠️ 仍有一条**潜在**边界：单组明细超过 `FOLD_ITEMS_MAX=100` 且正好压在页边界时，翻页覆盖数会
   重复计（`counts.total + N`）。24h 实测最大组 15 条，当前数据不触发；触发条件与改法在
   `docs/102` §5.2（K2）。
3. **推送折叠只折「严格连续」的同 code 推送**（中间夹任何非推送行就断开，间隔 ≤300 s，条数门槛与工具折叠共用
   `FISH_WB_FOLD_MIN`）：不这样做，折叠条的时间跨度会压过后面更旧的行，时间线会倒序。一组里推给多个对象时，
   文案是「（给 N 个对象）」而不是断言成一个名字。
4. **「本页折叠」只统计当前返回的这一页**（文案是「本页折叠 N 组」），翻页时 `loadMore` 不重算它。
5. **深翻页的安全边界**：四个源密度不同（24h 工具 ~2.5k、推送 ~0.7k、事件 ~0.3k），各自 `LIMIT` 的深度也不同；
   服务端按每个分支自己的前沿算出「完整性边界」，只把边界之上的条目算进本页，不够就补页——这是
   「翻到底不丢行」的实现方式（旧实现把游标放在并集外过滤，24h 只能覆盖 17.8%）。
6. **`/api/stats` 首调含 `import fish`（取 `push_stats`）**：这个固定成本由启动预热吸收，预热失败只记日志、不影响服务。
7. **四种空态里②「库为空」与④「窗口内无行为」未在活库上触发**（活库一直有数据），
   其代码路径与文案差异见 `codes.json`；QA 第二轮已用副本库把四种都构造过（含 K1 的修正）。
8. **`payload` / `args_digest` / `token_hash` 只回键名清单、不回值**；事件 subject 是公民时**不回 26 位 ULID**。
   界面上不写"只给键名，不给值"这种解释句：`payload` 标签 + 键名清单本身就是那个意思（评审 L1）。
9. 板视角下系统事件会有缺席（只有部分事件能按会话归属），已在设计里写明，接口不伪造补齐。
10. **`partial` 本期不支持，已整条删除**（QA F-8）。任何一个源失败都走 `_missing_envelope` 的 `missing`；
    UI 的 `AvailState` 不再声明 `partial`。机检：`accept_workbench.py --f78` 的 `F-8 partial 已删` 一行。
11. **`Block` 的五个状态都有构造点**（QA F-7）：`ok`/`empty`（空库副本）/`missing`（缺表副本）/
    `error`（假读口回错误信封）/`loading`（假读口不返回）——全在 `--f78` 里真渲染一遍；
    `npm run smoke` 的 S5 检查每个状态值在 `src/**` 里有构造点。
12. **A-11 的"滚到末页 ≥50fps"没有机检**（本机没有可编程的帧率采样器）。机检的是它的两个可测半边：
    首屏服务端中位 <1s + 虚拟滚动下 DOM 行节点 <200 而流里 3,600 条。帧率请人眼在 DevTools 看，不宣称已测。
13. **看板正文里的 `**` 不属于 UI 文案**：`board_posts.text` 是用户自己写的话，原样透传不改写；
    `npm run smoke` 的 S4 扫的是**构建产物**（剥掉第三方 license 注释后），不会误伤数据正文。
14. **<1280 视口整页横向滚动**（`min-w-[1280px]`）：本期不做折栏，也不宣称适配窄屏。
15. **页签二「这一条 / 这个人 / 他的账本」八行属性结构经用户裁决保留，评审 R-06 的条件被覆盖**
    （评审要求"压到 3 项以内"，用户 2026-11-23 裁定保持现状，理由是账本要能回答"他干过什么"，
    而压到 3 项就只剩下"有哪些字段"）。
16. **决策点口径**（R-05）：`docs/97 §A2` 的 16/12/9 是**旧口径**；按现行实现重数 = **15**
    （3 时间窗 + 1 状态行 + 5 项目控件 + 4 板控件 + 2 页签），含 8 个 ⓘ 入口是 **23**。
    **L2 未宣称**（不要按旧表的 16/12/9 复算，也不要把 15 当成"决策点少了 1 个"）。
17. **页签一 8 块 ⓘ 的窗口文案**：三块（推送 / 热推 / 热搜）真正跟随流水窗口选择器
    （`days` 由 `win` 推出：24h→1、7d→7、全部→30），另五块的窗口由查询本身固定
    （全期 / 全表 / 近 30 分钟 / 近 24 小时），文案从 `windows.ts` 的同一张表与后端返回的
    `windowMinutes` 渲染，不写裸字符串。窗口切到「全部」时这三块按 **30 天**（`days` 的上限）算，
    ⓘ 里如实写「近 30 天」——不假装是"从有数据起"。

---

## 七、目录

```
ui/
├── index.html  package.json  vite.config.ts  tsconfig.json  tailwind.css  codes.json
├── scripts/
│   └── smoke.mjs          # npm run smoke：构建新鲜度/资源完整/空态四句/无内部口径与旧页签名/状态构造点/截图晚于 dist
├── screenshots/           # 五个状态的验收截图（01-main / 02-1280x720 / 03-static-apidown / 04-project-czhy / 05-ledger-deeplink），文件名带时间戳
│   └── _legacy-predist/   # 早于当前构建的旧截图（留档，不算证据）
└── src/
    ├── main.tsx  App.tsx  router.ts  codes.ts  format.ts
    ├── pages.ts  windows.ts                 # 页面清单（窄条的数据源）/ 时间窗的唯一一张表
    ├── api/{client,types,endpoints}.ts      # client.ts 里是 API 基址解析（?api= → 构建期 → 同源）
    ├── store/{workbench.ts,scope.ts}        # 四条链各自一份 availability；isEmptyDb 是"空库"的唯一判据
    ├── components/{NavRail,AsyncBoundary,OriginTip,StatusLine,Tabs,Time,VirtualList,StreamRow,RowGroup,NewItemsPill,ValueCell}.tsx
    ├── views/workbench/{Workbench,Rail,Stream,Panel,TabNeedYou,TabLedger}.tsx
    └── styles/tokens.css
```

读口侧的脚本在仓根 `p0/tools/`：`verify_workbench_fixes.py`（F-1~F-6 与变异）、
`accept_workbench.py`（A-1~A-13 + F-7/F-8 证据）、`verify_v4.py`（v4 五条新判据与变异）、
`shoot_workbench.py`（重拍 5 张截图 + 版面读数）。全部只读；变异用 `%TEMP%` 副本库与假读口。
