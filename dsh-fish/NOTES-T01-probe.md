# NOTES · T01 步骤 0：三个先验假设的验证记录

> 执行时刻：2026-10-27 13:37–14:20（Asia/Shanghai）
> 运行时：系统 `node v24.19.0`（`C:\Program Files\nodejs\node.exe`）｜Python `3.11.9`
> 宿主运行时声明：`app/resources/runtime/primary-runtime/runtime.json` → `node 24.21.0` / `python 3.12.14`
> 生产库：`大肥鱼生命系统/p0/data/index.sqlite`（**全程 `mode=ro`**）
> 纪律：三个验证任一失败即停。**三项全部通过**，故继续建脚手架。

---

## 假设 1：`node:sqlite` 可用、能只读打开生产库、FTS5 `MATCH` 可用

脚本：`scripts/probe/probe-sqlite.mjs`（切词与 MATCH 表达式来自构建产物 `lib/`，不另写一份）

```
$ node scripts/probe/probe-sqlite.mjs
# probe-sqlite  db=...\p0\data\index.sqlite  node=v24.19.0
PASS  A.module  exports=DatabaseSync,Session,StatementSync,backup,constants,default node=v24.19.0
PASS  A.lib     lib=...\dsh-fish\lib\index.js
PASS  B.master  objects={"index":45,"table":28,"trigger":2,"view":2}
PASS  B.fts_def CREATE VIRTUAL TABLE memory_fts USING fts5( code UNINDEXED, title_idx, body_idx, tokenize='unicode61' )
PASS  C.readonly_enforced  写入被拒绝: attempt to write a readonly database
PASS  D.fts_match  expr="\"2026-10-23\" AND \"t01\" AND ..." fts_rows=312 hits=言纹-261023-02
# summary: 6/6 pass      EXIT=0
```

**结论：成立。** 四条要点：
1. `DatabaseSync` / `StatementSync` / `Session` / `backup` 都在，宿主 Node 24 内置模块可用（无需第三方原生模块）；
2. 生产库可只读打开；对象计数 `index=45（含自动索引）table=28 view=2 trigger=2`；
3. **只读是 SQLite 强制的**（`attempt to write a readonly database`），不是"我们小心不写"——这一条让影子期"零写路径"可被机器证明；
4. FTS5 `MATCH` 在 `memory_fts`（312 行）上命中，且表达式里的每个 token 都带双引号（`CR-12` 类查询不会退化成列名）。

## 假设 2：FTS 中文只差一个纯函数（TS 复刻 `index_tokens` 与 Python 逐条等价）

脚本：`scripts/probe/probe-cut.mjs`（TS 侧调 **`lib/cutTokens` 生产实现**）＋ `scripts/probe/cut_py.py`（Python 侧 `import p0/fish.py::index_tokens`，不复制实现）

```
$ node scripts/probe/probe-cut.mjs
# probe-cut  queries=50 python=3.11.9 node=v24.19.0
# python module: ...\p0\fish.py
PASS  diff=0  50/50 条逐 token 相等
EXIT=0
```

**结论：成立（diff=0，50/50）。**

### ⚠ 这里踩了一个坑，必须记下来（假红事故）

第一次运行是 **50/50 全红**，现象是 `py(12)=["fish","ai","����",...]` —— 数量完全一致、只是中文乱码。
根因不是切词差异，而是 **Windows 控制台默认 cp936**：Python 端 `json.dumps(ensure_ascii=False)` 写到
stdout 时按 cp936 编码，Node 端按 UTF-8 解码 → 满屏 `����`。

修法（两处，双保险）：`cut_py.py` 里 `sys.stdout.reconfigure(encoding='utf-8')`，且 `spawnSync` 时注入
`PYTHONIOENCODING=utf-8 / PYTHONUTF8=1`。修完复跑 `diff=0`。

**教训**：跨语言对拍里"看起来是内容差异"的问题，先查编码/换行/时区，再怀疑算法。
若当时直接改算法去"对齐乱码"，就会把正确的实现改坏——这是本机历史上反复出现的"把测试环境局限
当成方案错了"的那类事故。

### 查询词的来源（诚实说明）

50 条查询词**不是**历史真实用户提问：生产库 `memory_access_log.payload` 实测全为 `NULL`（只记 code + via），
真实查询串没有被持久化。因此按"真实语料"抽样：25 条 `code_index.title` + 15 条 `kb_access.path`
+ 10 条 `board_posts.text`（全部来自生产库，含中英混排、编码、路径、标点）。

## 假设 3：两路进程并发写同一 SQLite（`BEGIN IMMEDIATE` + `busy_timeout=5000`）不出未处理 BUSY

脚本：`scripts/probe/probe-concurrent.mjs`（父进程）＋ `scripts/probe/concurrent-write-child.mjs`（工人）

```
$ node scripts/probe/probe-concurrent.mjs
# probe-concurrent  src=...\p0\data\index.sqlite  rows/worker=100
PASS  A.snapshot 副本=C:\Users\15596\AppData\Local\Temp\fish-concurrent-xxx\probe-index.sqlite
PASS  A.no_unhandled_busy  busyErrors=0 otherErrors=0 written=200/200 exits=[0,0]
PASS  B.count_conserved    total=200 perTag=alpha:100,beta:100 seqComplete=true
PASS  C.busy_visible_when_no_timeout  busy_timeout=0 时 busyErrors=104（>0 才证明并发是真的在争锁）
# summary: 4/4 pass      EXIT=0
```

**结论：成立**，且带**反向对照**：
- `busy_timeout=5000` → 两进程各 100 行**零未处理 BUSY**，计数守恒（200 行、每 tag 100、seq 1..100 完整）；
- `busy_timeout=0` → 104 次 BUSY。这条对照证明 A 测的是**真并发**（不是两个进程恰好串行跑完）。

**只读纪律**：源库只用 `readOnly` 连接 + `sqlite.backup()` 复制到 `%TEMP%`，所有写都发生在副本上，
`finally` 里整目录删除。生产库全程未被写入。

---

## 步骤 0 顺带发现的其他事实（影响后续设计，先记不修）

| # | 发现 | 影响 |
|---|---|---|
| F1 | 事件类型：源码 `append_event` 调用点共 **27 种**；生产库实测 **21 种** | 调研文档的"21"是**观测值**；spec 收录 27 并标 `observedInDb`，生成脚本对"库内出现未登记类型"和"观测数≠21"双向硬失败 |
| F2 | 注入包：`INJECTION_SECTIONS` 是 **8 项**（status/board/rules/attention/lessons/push/index/recent），外加 **L0 永不关** | 任务书写"8 段"、架构方案写"9 段"，实测口径 = **L0 + 8 可选段**；spec 按实测记录（`sections` 9 段，L0 单独 `kind: l0`） |
| F3 | `p0/data/index.sqlite` 的业务表是 **21 张**（28 张表 − `memory_fts` 1 − `memory_fts_*` 5 − `sqlite_sequence` 1） | 我的第一版"业务表=22"是口径错（把 `memory_fts` 本体算进去了），已在 `businessTableNames()` / `businessTableCount` 两处统一修正 |
| F4 | `pnpm` 11 起不再读 package.json 的 `pnpm.onlyBuiltDependencies`，改用 `pnpm-workspace.yaml` 的 `allowBuilds` | 不写 `pnpm-workspace.yaml` 时 `tsdown` 会因 esbuild 未构建而失败；已在仓库内澄清（并显式把 5 个用不到的原生依赖设为 `false`） |
| F5 | 本机 `pnpm` 不在 PATH（只有 `app/resources/runtime/pnpm/bin/pnpm.cjs`） | 验收命令要求 `pnpm` 在 PATH；本机用临时 shim 跑通（shim 放在 `%TEMP%`，未污染仓库） |
| F6 | `node --test test/unit` 与 `node --test test/unit/` 都被 Node 24 当成"模块路径"而失败 | `pnpm test` 改为显式文件路径（与社区模板一致的做法） |
