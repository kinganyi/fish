"""会话树投影的**纯 DDL**（113 §2.3 · 2026-11-25 · W1b 从 `fish_schema.py` 抽出）。

## 为什么单独一个模块

W1 把这两张表直接写进 `fish_schema.py`，而那个文件的 J4 窗口当时已经**顶格**
（700 = 上限）——于是 W1 把窗口抬到 780。**方向反了**：仓库的规矩是
`test_split_contract.py:114-115` 那句「拆一次就降一次……方向只许降」，
以及 2026-11-11 的先例「**先抽层再抬窗口**」。W1b 把常量搬到本模块，窗口还回 700。

先例是 `fish_git_schema.py`：`fish_schema` 要 import 它拼 `SCHEMA`，所以它必须
**比 fish_schema 更底层**。本模块同理——**不 import 任何东西**（连 stdlib 都不用），
只含字符串常量。这段就是「它为什么在底层」的答案。

## 本模块的边界

- 只有两个 `_*_DDL`、`_LINEAGE_INDEXES` 与拼装出的 `_LINEAGE_SCHEMA`：
  **没有函数、没有 IO、没有 import**
- 建表与建索引同处一个常量：全新库走 `fish.SCHEMA`，旧库走
  `fish_lineage.ensure_tables()`——两条路 `executescript` 的是**同一个对象**，
  不是两份会各自漂移的拷贝
"""

# ============================================================
# 会话树投影（113 §2.3 · 2026-11-25 · W1）：两张**派生表**，可整体重建。
#
# 真相在宿主日志（110 §3.2），本表只是可查询的投影——所以**没有外键、没有触发器**，
# 回退只需要 `DROP TABLE`（113 §2.7）。三条反直觉的取舍必须留在这里（113 §2.3 末）：
#   ① 不建外键：`citizens_all` 是视图、公民又分冷热两表，FK 只能指向热表，
#      归档公民立刻违约。改用判据兜：`citizen_id` 必须能在 `citizens_all` 查到。
#   ② `origin='main' + 有父` 做成 CHECK（110 §6 J-D4 的库级兜底）：库里就**不可能**
#      存在这种行。代价是"计数是否为 0"恒 0、没有判别力，所以 W1 判据改成**试插入**。
#   ③ `session_identity.session_key` 允许带 `#role` 尾（113 §1-5：`issue_agent_token`
#      的子键是 `<父会话>#<role>`，W4 不执行去尾）：join 一律走
#      `fish_lineage.identity_key_of()` 这一个剥离点，不许各处自己 split。
#
# `lineage_path` 随深度线性变长（深 100 层就上千字符）：**只许展示、不许判据**（113 §2.8）。
# ============================================================
_SESSION_LINEAGE_DDL = """CREATE TABLE IF NOT EXISTS session_lineage (
  host           TEXT NOT NULL,              -- dsh | workbuddy | codebuddy
  session_id     TEXT NOT NULL,              -- 宿主原始形态：不带 'host:' 前缀，**不做前缀归一**（110 §3.2 末）
  parent_session TEXT,                       -- 裸父 id；NULL = 根 或 判不到（由 basis 区分）
  lineage_path   TEXT NOT NULL DEFAULT '',   -- '/a/b/c'；根=''；**只许用于展示，不许用于判据**
  depth          INTEGER NOT NULL DEFAULT 0, -- 由链重算（§3.6.1）；宿主值落 depth_host
  depth_host     INTEGER,                    -- 宿主 delegationDepth，仅对拍；与 depth 不等 → basis='conflict'
  origin         TEXT NOT NULL,              -- main | subagent | fork | unknown
  relation       TEXT,                       -- spawn | fork | team_member | continue | cycle | NULL
  agent_id       TEXT,                       -- DSH 恒 NULL；WB = transcript 文件名 'agent-<..>'
  agent_role     TEXT,                       -- DSH = agentPreset；WB = runtime members[].agentId 的 role 段
  agent_label    TEXT,                       -- DSH = catalog.label；WB = members[].taskDescription
  agent_path     TEXT,                       -- 预留（同会话多代理宿主）；本期**恒 NULL**，不许为对称先填
  cwd            TEXT,
  agent_preset   TEXT,
  basis          TEXT NOT NULL,              -- header|catalog|db|path|runtime|depth|inferred|missing|conflict
  source         TEXT NOT NULL,              -- 'dsh:header' / 'wb:runtime' / 'wb:path' / ...
  extra          TEXT,                       -- JSON：infer_rule / path_id_mismatch / notes
  observed_at    TEXT NOT NULL,
  retracted_at   TEXT,
  PRIMARY KEY (host, session_id),
  CHECK (origin IN ('main','subagent','fork','unknown')),
  CHECK (relation IS NULL OR relation IN ('spawn','fork','team_member','continue','cycle')),
  CHECK (basis IN ('header','catalog','db','path','runtime','depth','inferred','missing','conflict')),
  CHECK (depth >= 0),
  -- 把 110 §6 的 J-D4 做成**约束**：主+有父 在库里就不可能存在
  CHECK (origin <> 'main' OR parent_session IS NULL)
);"""

_SESSION_IDENTITY_DDL = """CREATE TABLE IF NOT EXISTS session_identity (
  session_key    TEXT PRIMARY KEY,           -- 宿主原始形态；可能带 '#role' 尾（§1-5）
  citizen_id     TEXT NOT NULL,              -- 解析结果
  inherited_from TEXT,                       -- 最近一个**有身份**祖先的 session_key；NULL = 本会话自己的公民
  origin         TEXT NOT NULL,              -- direct | inherited | provisional
  evidence       TEXT NOT NULL,              -- machine | inherited | self-reported | provisional
  created_at     TEXT NOT NULL,
  observed_at    TEXT NOT NULL,
  CHECK (origin IN ('direct','inherited','provisional')),
  CHECK (origin <> 'inherited'  OR inherited_from IS NOT NULL),
  CHECK (origin <> 'direct'     OR inherited_from IS NULL),
  CHECK (origin <> 'provisional' OR citizen_id <> '')
);"""

_LINEAGE_INDEXES = (
    # 三张索引对应三种取数口：按父找子（树）、按深度分层（渲染/对账）、按证据强度筛（判据）
    "CREATE INDEX IF NOT EXISTS idx_lineage_parent ON session_lineage(host, parent_session);",
    "CREATE INDEX IF NOT EXISTS idx_lineage_depth  ON session_lineage(host, depth);",
    "CREATE INDEX IF NOT EXISTS idx_lineage_basis  ON session_lineage(basis);",
    # 身份继承的两条读：按公民反查（"这个公民有哪些会话"）/ 按祖先找后代（继承链）
    "CREATE INDEX IF NOT EXISTS ix_identity_citizen ON session_identity(citizen_id);",
    # 部分索引：`inherited_from IS NULL` 的行（本会话自己的公民）**根本不进**这个索引
    "CREATE INDEX IF NOT EXISTS ix_identity_inherited "
    "ON session_identity(inherited_from) WHERE inherited_from IS NOT NULL;",
)

# 拼装纪律同 `_PROFILE_SCHEMA`：索引串之间必须补分号，否则 `CREATE TABLE` 之后紧跟
# `CREATE INDEX` 会拼成 `...)CREATE INDEX ...` —— sqlite 报 near "CREATE": syntax error。
_LINEAGE_SCHEMA = ("\n" + _SESSION_LINEAGE_DDL + "\n\n" + _SESSION_IDENTITY_DDL
                   + "\n" + "\n".join(_LINEAGE_INDEXES) + "\n")
