#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据库 DDL 常量（2026-11-11 从 `fish.py` 抽出，错题本 E-11 拆包第二刀）。

## 为什么先搬这个

`fish.py` 是枢纽文件：真正的领域逻辑（注入包组装、会话身份）传递闭包都跨几十个函数，
硬拆会制造循环依赖。而**建表语句是纯数据**——没有逻辑、没有分支、没有 IO，
搬走它们的风险是拆包里最低的，收益却是实打实的四百来行。

## 为什么不搬 `SCHEMA` 本身

`SCHEMA` 是**组合者**：它要拼上 `fish_kb._KB_SCHEMA`。把它也搬进来，本模块就得
import `fish_kb`，于是分层变成 `fish_schema → fish_kb`——而 schema 概念上应当在
kb 之下。所以 `SCHEMA` 留在 `fish.py`，它从这里和 `fish_kb` 两边取零件拼装。
这样本模块**只依赖 stdlib**，是最干净的一层。

## 与 J2 的关系（`fish → fish_kb → fish_common`）

本模块不 import 任何 fish_* 模块，所以它在 J2 的允许侧，谁都可以安全依赖它。
`fish.py` 通过 `from fish_schema import (...)` 保持同名，既有引用一个字都不改。
"""


_ACCESS_LOG_DDL = """CREATE TABLE IF NOT EXISTS memory_access_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  code        TEXT NOT NULL,
  citizen_id  TEXT,
  session_key TEXT,
  project     TEXT,
  via         TEXT NOT NULL,
  payload     TEXT,
  accessed_at TEXT NOT NULL
);"""

_LINKS_DDL = """CREATE TABLE IF NOT EXISTS links (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  src                TEXT NOT NULL,
  dst                TEXT NOT NULL,
  rel                TEXT NOT NULL,
  source             TEXT NOT NULL,
  weight             REAL DEFAULT 1.0,
  evidence           TEXT,
  created_at         TEXT NOT NULL,
  removed_at         TEXT,
  last_reinforced_at TEXT,
  reinforce_count    INTEGER DEFAULT 0,
  suppressed         INTEGER DEFAULT 0,
  UNIQUE(src, dst, rel)
);"""

_PUSH_LOG_DDL = """CREATE TABLE IF NOT EXISTS push_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  session_key   TEXT,
  citizen_id    TEXT,
  project       TEXT,
  code          TEXT,
  candidate_id  TEXT,
  channel       TEXT NOT NULL,
  budget_tokens INTEGER,
  pushed_at     TEXT NOT NULL,
  reaction      TEXT,
  reacted_at    TEXT,
  latency_sec   INTEGER,
  content_hash  TEXT
);"""

_PUSH_SCHEMA = ("\n" + _ACCESS_LOG_DDL + "\n\n" + _LINKS_DDL
                + "\n\n" + _PUSH_LOG_DDL + "\n")

_AGENT_ACCOUNTS_DDL = """CREATE TABLE IF NOT EXISTS agent_accounts (
  account_id   TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  host         TEXT,
  kind         TEXT,
  created_at   TEXT NOT NULL,
  closed_at    TEXT,
  note         TEXT
);"""

_SESSION_TOKENS_DDL = """CREATE TABLE IF NOT EXISTS session_tokens (
  token_hash   TEXT PRIMARY KEY,   -- sha256(token)，明文只在签发时回一次
  citizen_id   TEXT NOT NULL,
  session_key  TEXT NOT NULL,
  evidence     TEXT NOT NULL,      -- machine | inherited | self-reported
  channel      TEXT NOT NULL,      -- cli | hook | parent
  parent_token TEXT,               -- inherited 时指向父
  issued_at    TEXT NOT NULL,
  expires_at   TEXT,
  revoked_at   TEXT,
  CHECK (channel IN ('cli','hook','parent')),
  CHECK (evidence IN ('machine','inherited','self-reported')),
  CHECK (channel != 'parent' OR parent_token IS NOT NULL),
  CHECK (channel != 'parent' OR evidence = 'inherited')
);"""

_PROJECT_STINTS_DDL = """CREATE TABLE IF NOT EXISTS project_stints (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  citizen_id    TEXT NOT NULL,
  account_id    TEXT,
  project       TEXT NOT NULL,
  from_ts       TEXT NOT NULL,
  to_ts         TEXT,               -- NULL = 当前开档期
  set_by        TEXT,
  closed_reason TEXT
);"""

_CITIZENS_ARCHIVE_DDL = """CREATE TABLE IF NOT EXISTS citizens_archive (
  citizen_id       TEXT PRIMARY KEY,
  display_name     TEXT,
  kind             TEXT,
  role             TEXT,
  host             TEXT,
  status           TEXT DEFAULT 'active',
  created_at       TEXT,
  is_owner         INTEGER DEFAULT 0,
  account_id       TEXT,
  device_id        TEXT,
  host_session_key TEXT,
  last_seen_at     TEXT,
  archived_at      TEXT
);"""

_CITIZENS_NEW_COLS = (
    ("account_id", "TEXT"),        # 属于哪个 AI 账号（跨会话聚合的血缘）
    ("device_id", "TEXT"),         # 设备（本地阶段留空）
    ("host_session_key", "TEXT"),  # 宿主给的会话标识（如 DSH_SESSION_ID）
    ("last_seen_at", "TEXT"),      # 最后活跃
    ("archived_at", "TEXT"),       # 归档时间（NULL = 未归档）
)

_STINT_OPEN_INDEX_NAME = "ux_stint_open"

_STINT_OPEN_INDEX_COLS = ("citizen_id", "project")

_STINT_OPEN_INDEX_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_stint_open "
    "ON project_stints(citizen_id, project) WHERE to_ts IS NULL")

_IDENTITY_INDEXES = (
    # 凭证撤销的两条主查询：按公民批量吊销 / 按会话查还有哪些活的
    "CREATE INDEX IF NOT EXISTS ix_session_tokens_citizen ON session_tokens(citizen_id)",
    "CREATE INDEX IF NOT EXISTS ix_session_tokens_session ON session_tokens(session_key)",
    # account_id 存在的唯一理由就是"按账号聚合会话"，这条单列索引是它的取数口。
    # 刻意不给 device_id / last_seen_at 建索引：还没有查询的东西不加索引。
    "CREATE INDEX IF NOT EXISTS ix_citizens_account ON citizens(account_id)",
)

_IDENTITY_TABLE_DDLS = (_CITIZENS_ARCHIVE_DDL, _AGENT_ACCOUNTS_DDL,
                        _SESSION_TOKENS_DDL, _PROJECT_STINTS_DDL)

_IDENTITY_SCHEMA = "\n" + "\n\n".join(_IDENTITY_TABLE_DDLS) + "\n"

_WORKSPACE_MAP_DDL = """CREATE TABLE IF NOT EXISTS workspace_map (
  path_prefix TEXT PRIMARY KEY,   -- 归一化后的路径前缀（正斜杠、无尾斜杠、全小写）
  project     TEXT NOT NULL,      -- 项目前缀（如 ANYI）
  note        TEXT,
  created_at  TEXT NOT NULL
);"""

_WORKSPACE_SCHEMA = "\n" + _WORKSPACE_MAP_DDL + "\n"

_NAME_ALIASES_DDL = """CREATE TABLE IF NOT EXISTS name_aliases (
  kind       TEXT NOT NULL,       -- 'project' | 'citizen'
  from_name  TEXT NOT NULL,       -- 别名（非规范名）
  to_name    TEXT NOT NULL,       -- 规范名
  created_at TEXT NOT NULL,
  created_by TEXT,
  reason     TEXT,
  PRIMARY KEY (kind, from_name)
);"""

_NAME_SCHEMA = "\n" + _NAME_ALIASES_DDL + "\n"

_BOARDS_DDL = """CREATE TABLE IF NOT EXISTS boards (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  scope      TEXT NOT NULL,               -- root | project | branch
  scope_key  TEXT NOT NULL,               -- root='' / project='anyi-studio' / branch='<名字>'
  period_key TEXT NOT NULL,               -- YYYYMMDD（按日一块板）
  path       TEXT NOT NULL,               -- 渲染文件相对路径（派生，可重建）
  status     TEXT NOT NULL DEFAULT 'open',-- open | closed
  created_at TEXT NOT NULL,
  closed_at  TEXT,
  UNIQUE(scope, scope_key, period_key)
);"""

_PRESENCE_DDL = """CREATE TABLE IF NOT EXISTS presence (
  board_id      INTEGER NOT NULL REFERENCES boards(id),
  citizen_id    TEXT NOT NULL,
  -- ↓ 2026-10-26（撞车自撞修复 T01 · 设计 45 §2.1）：会话维度**进主键**。
  --   为什么另加一列而不是复用 agent_id：agent_id 是"外部可读的宿主侧标识"，
  --   它的历史值里有非会话形态（`codebuddy-主AI/ffd54d84`），拿它当权限级主键
  --   等于把"猜出来的身份"焊进唯一约束。
  --   为什么是空串而不是 NULL：NULL 在唯一约束里互不相等（NULL != NULL），
  --   "同一公民的无会话行"每次插入都会变成新行——那比"合并成一行"更坏。
  --   '' = 无会话归属（旧行 / 无凭证路径 / 未知来源），**不是**"某个会话"。
  session_key   TEXT NOT NULL DEFAULT '',
  agent_id      TEXT,                     -- 会话/宿主侧标识（自动行会存 session_key）
  status        TEXT NOT NULL DEFAULT 'active',  -- active | idle | left
  doing         TEXT,                     -- 一句话：在做什么（≤80 字，撞车判断靠它）
  surface       TEXT,                     -- 占用面（文件/目录前缀，逗号分隔）
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  -- ↓ 2026-10-26（hooks 深度利用 T02）：自动占用的**缓存**三列（真相在 tool_activity）。
  --   auto_surface 只是"最近一次聚合的快照"，供外部读者/快照快速取用；
  --   看板渲染、推送、撞车判定都以 tool_activity 的读时聚合为准。
  --   否则「声明 vs 实际」又变成两份会漂的真相。
  auto_surface  TEXT,                     -- 最近一次聚合的目录（逗号分隔，≤3）
  auto_seen_at  TEXT,                     -- 最后一次写动作时间（UTC）
  auto_source   TEXT                      -- session_start | session_inject | remember | activity
  ,PRIMARY KEY (board_id, citizen_id, session_key)
);"""

_PRESENCE_NEW_COLS = (
    ("auto_surface", "TEXT"),
    ("auto_seen_at", "TEXT"),
    ("auto_source", "TEXT"),
)

_PRESENCE_V3_COLS = ("board_id", "citizen_id", "session_key", "agent_id", "status",
                     "doing", "surface", "first_seen_at", "last_seen_at",
                     "auto_surface", "auto_seen_at", "auto_source")

_PRESENCE_PK_COLS = ("board_id", "citizen_id", "session_key")

_PRESENCE_V2_DDL = """CREATE TABLE IF NOT EXISTS presence_v2 (
  board_id      INTEGER NOT NULL REFERENCES boards(id),
  citizen_id    TEXT NOT NULL,
  agent_id      TEXT,
  status        TEXT NOT NULL DEFAULT 'active',
  doing         TEXT,
  surface       TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  auto_surface  TEXT,
  auto_seen_at  TEXT,
  auto_source   TEXT
  ,PRIMARY KEY (board_id, citizen_id)
);"""

_PRESENCE_V2_COLS = ("board_id", "citizen_id", "agent_id", "status", "doing", "surface",
                     "first_seen_at", "last_seen_at", "auto_surface", "auto_seen_at",
                     "auto_source")

_BOARDS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_boards_scope ON boards(scope, scope_key, period_key)",
    "CREATE INDEX IF NOT EXISTS ix_boards_status ON boards(status, period_key)",
    "CREATE INDEX IF NOT EXISTS ix_presence_last ON presence(board_id, last_seen_at)",
)

_BOARD_POSTS_DDL = """CREATE TABLE IF NOT EXISTS board_posts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  board_id   INTEGER NOT NULL REFERENCES boards(id),
  citizen_id TEXT NOT NULL,
  kind       TEXT NOT NULL,               -- checkin | progress | done | note
  text       TEXT NOT NULL,               -- ≤200 字（渲染时硬截断）
  created_at TEXT NOT NULL
);"""

_BOARD_POSTS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_posts_board ON board_posts(board_id, id DESC)",
)

_BOARD_SEEN_DDL = """CREATE TABLE IF NOT EXISTS board_seen (
  board_id         INTEGER NOT NULL,
  citizen_id       TEXT NOT NULL,
  session_key      TEXT NOT NULL DEFAULT '',
  last_post_id     INTEGER NOT NULL DEFAULT 0,
  last_presence_at TEXT NOT NULL DEFAULT '',
  updated_at       TEXT NOT NULL,
  PRIMARY KEY (board_id, citizen_id, session_key)
);"""

_BOARD_SEEN_PK_COLS = ("board_id", "citizen_id", "session_key")

_BOARD_TABLE_DDLS = (_BOARDS_DDL, _PRESENCE_DDL, _BOARD_POSTS_DDL, _BOARD_SEEN_DDL)

_BOARD_SCHEMA = "\n" + "\n\n".join(_BOARD_TABLE_DDLS) + "\n"

_TOOL_ACTIVITY_DDL = """CREATE TABLE IF NOT EXISTS tool_activity (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  host        TEXT NOT NULL,          -- dsh | workbuddy（方言来源，便于分宿主统计）
  session_key TEXT NOT NULL,          -- dsh:<session_id> / codebuddy:<session_id>
  citizen_id  TEXT,                   -- 由 session_key 反查；查不到 NULL，不猜
  project     TEXT,                   -- workspace_map 最长前缀；可空（不猜）
  tool_name   TEXT NOT NULL,          -- 小写归一：write/edit/bash/...
  action      TEXT NOT NULL,          -- write | exec（本批只落这两种）
  path        TEXT,                   -- 相对项目根（无则相对 cwd），展示/聚合
  path_dir    TEXT,                   -- 相对目录，聚合键
  path_abs    TEXT,                   -- 归一化绝对路径，撞车判定（跨 cwd 可比）
  args_digest TEXT,                   -- sha256(参数摘要)[:16]，**不含内容**
  ok          INTEGER,                -- 1/0/NULL（NULL=未知，不假装成功）
  seen_at     TEXT NOT NULL           -- UTC ISO（%Y-%m-%dT%H:%M:%SZ）
);"""

_ACTIVITY_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_activity_seen ON tool_activity(seen_at)",
    "CREATE INDEX IF NOT EXISTS ix_activity_session "
    "ON tool_activity(session_key, seen_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_activity_citizen "
    "ON tool_activity(citizen_id, seen_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_activity_dir "
    "ON tool_activity(project, path_dir, seen_at DESC)",
)

_ACTIVITY_SCHEMA = "\n" + _TOOL_ACTIVITY_DDL + "\n"

_TOOL_ACTIVITY_COLS = (
    ("host", "TEXT"),
    ("session_key", "TEXT"),
    ("citizen_id", "TEXT"),
    ("project", "TEXT"),
    ("tool_name", "TEXT"),
    ("action", "TEXT"),
    ("path", "TEXT"),
    ("path_dir", "TEXT"),
    ("path_abs", "TEXT"),
    ("args_digest", "TEXT"),
    ("ok", "INTEGER"),
    ("seen_at", "TEXT"),
)

_ACCESS_LOG_COLS = (
    ("code", "TEXT"),
    ("citizen_id", "TEXT"),
    ("session_key", "TEXT"),
    ("project", "TEXT"),
    ("via", "TEXT"),
    ("payload", "TEXT"),
    ("accessed_at", "TEXT"),
)

_LINKS_COLS = (
    ("src", "TEXT"),
    ("dst", "TEXT"),
    ("rel", "TEXT"),
    ("source", "TEXT"),
    ("weight", "REAL DEFAULT 1.0"),
    ("evidence", "TEXT"),
    ("created_at", "TEXT"),
    ("removed_at", "TEXT"),
    # 再巩固预留（14 §6）：旧库升级也要补上这 3 列。
    # 声明必须是「可加到非空表上」的形态（INTEGER 带 DEFAULT），否则 ALTER 会失败。
    ("last_reinforced_at", "TEXT"),
    ("reinforce_count", "INTEGER DEFAULT 0"),
    ("suppressed", "INTEGER DEFAULT 0"),
)

_ACCESS_LOG_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_mal_code ON memory_access_log(code, accessed_at)",
    "CREATE INDEX IF NOT EXISTS ix_mal_citizen ON memory_access_log(citizen_id, accessed_at)",
    "CREATE INDEX IF NOT EXISTS ix_mal_session ON memory_access_log(session_key, accessed_at)",
)

_LINKS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_links_src ON links(src) WHERE removed_at IS NULL",
    "CREATE INDEX IF NOT EXISTS ix_links_dst ON links(dst) WHERE removed_at IS NULL",
    "CREATE INDEX IF NOT EXISTS ix_links_rel ON links(rel) WHERE removed_at IS NULL",
    # 再巩固预留：候选池只捞 suppressed=0 且未删除的边，这条部分索引就是它的取数口。
    "CREATE INDEX IF NOT EXISTS ix_links_suppressed ON links(suppressed) "
    "WHERE removed_at IS NULL",
)

_PUSH_LOG_COLS = (
    ("session_key", "TEXT"),
    ("citizen_id", "TEXT"),
    ("project", "TEXT"),
    ("code", "TEXT"),
    ("candidate_id", "TEXT"),
    ("channel", "TEXT"),
    ("budget_tokens", "INTEGER"),
    ("pushed_at", "TEXT"),
    ("reaction", "TEXT"),
    ("reacted_at", "TEXT"),
    ("latency_sec", "INTEGER"),
    # P0-2 第二段（2026-10-29）：推这份内容时它的指纹。结案（accepted/ignored）
    # 之后只有"当前指纹 != 记录指纹"才允许重推——读过的旧版不再打扰，新版该来。
    ("content_hash", "TEXT"),
)

_PUSH_LOG_INDEXES = (
    # 按会话回看"这个会话被推过什么"——时机策略的主查询就是它
    "CREATE INDEX IF NOT EXISTS ix_push_session ON push_log(session_key, pushed_at)",
    # 按记忆回看"这条被推过几次、反应如何"（采纳率按 code 聚合）
    "CREATE INDEX IF NOT EXISTS ix_push_code ON push_log(code, pushed_at)",
    # 待判行的取数口：read_body 的回填与 daily_report 的收口都只碰这些行
    "CREATE INDEX IF NOT EXISTS ix_push_reaction ON push_log(reaction) WHERE reaction IS NULL",
)

AUDIT_DIR_NAME = "_audit"

FALLBACK_PROJECT_NAME = "未分类"
