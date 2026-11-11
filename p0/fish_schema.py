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


# ============================================================
# 知识库表结构（2026-11-11 从 `fish_kb.py` 上移，E-11 拆包第三刀）
#
# 为什么从 `fish_kb` 搬到最底层的 schema 模块：主库的 `SCHEMA` 要拼上 `_KB_SCHEMA`
# （知识库表和记忆表在同一个 index.sqlite 里）。放在 fish_kb 里时，`SCHEMA` 的组合
# 就必须依赖 domain 模块 `fish_kb` —— 分层倒置。移到本模块后：
#
#     fish_common  ←  fish_schema  ←  fish_kb  ←  fish
#
# `fish_schema` 依旧**只依赖 stdlib**（这些全是纯 DDL 字符串），
# `fish_kb` 改为从这里 import，`fish.py` 的 `SCHEMA` 也终于能在本模块内拼装。
# ============================================================

_KB_VAULT_DDL = """CREATE TABLE IF NOT EXISTS kb_vault (
  vault_id       TEXT PRIMARY KEY,
  name           TEXT NOT NULL UNIQUE,
  path           TEXT NOT NULL,
  path_key       TEXT NOT NULL,
  mode           TEXT NOT NULL DEFAULT 'ro' CHECK (mode IN ('ro','rw')),
  ignore_globs   TEXT NOT NULL DEFAULT '[".obsidian/**",".trash/**","90-模板/**"]',
  max_open_bytes INTEGER NOT NULL DEFAULT 2097152,
  is_default     INTEGER NOT NULL DEFAULT 0,
  last_scan_at   TEXT,
  last_scan_files   INTEGER,
  last_scan_notes   INTEGER,
  last_scan_bytes   INTEGER,
  note           TEXT,
  status         TEXT NOT NULL DEFAULT 'active',
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);"""

_KB_ACCESS_DDL = """CREATE TABLE IF NOT EXISTS kb_access (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  vault_id    TEXT NOT NULL,
  path        TEXT NOT NULL,
  citizen_id  TEXT NOT NULL DEFAULT 'unattributed',
  account_id  TEXT,
  action      TEXT NOT NULL,
  result      TEXT NOT NULL DEFAULT 'ok',
  via         TEXT NOT NULL,
  ts          TEXT NOT NULL,
  bytes       INTEGER,
  sha256      TEXT,
  backup_path TEXT,
  meta        TEXT
);"""

_KB_VAULT_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_kb_vault_path ON kb_vault(path_key)",
    # 部分唯一索引：默认库最多一个（is_default=0 的行可以无限多）
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_kb_vault_default "
    "ON kb_vault(is_default) WHERE is_default = 1",
)

_KB_ACCESS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_kb_access_path ON kb_access(vault_id, path, ts DESC)",
    "CREATE INDEX IF NOT EXISTS ix_kb_access_citizen ON kb_access(citizen_id, ts DESC)",
    "CREATE INDEX IF NOT EXISTS ix_kb_access_action ON kb_access(action, ts DESC)",
)

_KB_PATH_STATS_VIEW = """CREATE VIEW IF NOT EXISTS kb_path_stats AS
SELECT vault_id,
       path,
       COUNT(*)                                                    AS hits,
       MAX(ts)                                                     AS last_at,
       SUM(CASE WHEN action='open' THEN 1 ELSE 0 END)               AS open_count,
       SUM(CASE WHEN action IN ('create','append','update') THEN 1 ELSE 0 END) AS write_count,
       SUM(CASE WHEN action='rollback' THEN 1 ELSE 0 END)           AS rollback_count,
       COUNT(DISTINCT citizen_id)                                   AS readers
FROM kb_access
GROUP BY vault_id, path;"""

_KB_PATH_STATS_COLUMNS = ("vault_id", "path", "hits", "last_at", "open_count",
                          "write_count", "rollback_count", "readers")

_KB_SCHEMA = "\n" + "\n\n".join((_KB_VAULT_DDL, _KB_ACCESS_DDL)) + "\n"

_KB_VAULT_COLS = ("vault_id", "name", "path", "path_key", "mode", "ignore_globs",
                  "max_open_bytes", "is_default", "last_scan_at", "last_scan_files",
                  "last_scan_notes", "last_scan_bytes", "note", "status",
                  "created_at", "updated_at")


# ============================================================
# 主库 DDL 的拼装 + 连接（2026-11-11 从 `fish.py` 搬入，E-11 拆包第三刀）
#
# `SCHEMA` 是**组合者**：把下面这些分片按固定顺序拼起来。上一刀把分片搬进来之后，
# 它终于也能在这里拼了——而不再需要 `fish.py` 去 import `fish_kb`（那是分层倒置）。
# 同批把 `connect` 也搬来：它只依赖 `sqlite3` 与 `Path`，跟建表是一件事。
#
# 分层现在是干净的：
#     fish_common  ←  fish_schema  ←  fish_kb / fish_rebuild  ←  fish
# 本模块除 stdlib 外不 import 任何东西，所以在 J2 的允许侧，谁都可以安全依赖它。
# ============================================================
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  seq       INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id  TEXT NOT NULL UNIQUE,
  ts        TEXT NOT NULL,
  type      TEXT NOT NULL,
  actor     TEXT NOT NULL,
  subject   TEXT NOT NULL,
  payload   TEXT NOT NULL,
  cause     TEXT,
  refs      TEXT,
  schema    TEXT NOT NULL DEFAULT 'evt@1'
);

CREATE TRIGGER IF NOT EXISTS ev_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;

CREATE TRIGGER IF NOT EXISTS ev_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;

CREATE TABLE IF NOT EXISTS code_registry (
  prefix    TEXT PRIMARY KEY,
  namespace TEXT,
  last_date TEXT,
  last_seq  INTEGER DEFAULT 0,
  status    TEXT DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS code_index (
  code             TEXT PRIMARY KEY,
  title            TEXT,
  project          TEXT,
  owner            TEXT,
  mem_type         TEXT,
  visibility       TEXT,
  status           TEXT DEFAULT 'active',
  tags             TEXT,
  file_path        TEXT,
  occurred_at      TEXT,
  recorded_at      TEXT,
  last_accessed_at TEXT,
  access_count     INTEGER DEFAULT 0,
  dormant          INTEGER DEFAULT 0,
  session_actor    TEXT,
  attachments      TEXT,
  superseded_by    TEXT
);

CREATE TABLE IF NOT EXISTS citizens (
  citizen_id       TEXT PRIMARY KEY,
  display_name     TEXT,
  kind             TEXT,
  role             TEXT,
  host             TEXT,
  status           TEXT DEFAULT 'active',
  created_at       TEXT,
  is_owner         INTEGER DEFAULT 0,
  -- ↓ P1-a 追加的 5 列（旧库靠 migrate 的 ALTER 补齐，见 _CITIZENS_NEW_COLS）。
  --   这一段必须与 _CITIZENS_ARCHIVE_DDL 逐列一致：citizens_all 按**列位置**拼。
  account_id       TEXT,
  device_id        TEXT,
  host_session_key TEXT,
  last_seen_at     TEXT,
  archived_at      TEXT
);
-- 注意：owner 的唯一索引不在 SCHEMA 里创建。
-- 旧库的 citizens 表可能还没有 is_owner 列，此时建索引会报 no such column。
-- 索引统一在 migrate() 里「先 ALTER 加列、再建索引」，保证新旧库都能走通。

CREATE TABLE IF NOT EXISTS config (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  type       TEXT NOT NULL DEFAULT 'string',
  updated_at TEXT
);

-- 全文检索索引（ADR-005）。**派生表，不是真相源**：可由 code_index + 正文文件重建。
-- 为什么自己切 token 而不让 FTS 分：内置 unicode61 不做中文分词，「协作约定」是整 token，
-- 查「协作」永远 0 条（2026-10-22 实测）。详见 ADR-005 背景表。
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  code UNINDEXED,
  title_idx,
  body_idx,
  tokenize='unicode61'
);
""" + _PUSH_SCHEMA + _IDENTITY_SCHEMA + _WORKSPACE_SCHEMA + _NAME_SCHEMA + _BOARD_SCHEMA \
    + _ACTIVITY_SCHEMA + _KB_SCHEMA

def connect(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    db = root / "index.sqlite"
    conn = sqlite3.connect(str(db), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
