-- fish 主库 DDL 快照（只读导出，勿手工编辑）
-- 来源：I:\app\DSH-Home\workspace\learn\大肥鱼生命系统\p0\data\index.sqlite
-- 生成：scripts/gen-spec.mjs ｜ Python 3.11.9
-- 指纹：2ba9df3052c437896eec61b1a597fc5429318fa48723c698c80441c52cf01ba8
-- 口径：排除 FTS5 影子表（memory_fts_* 与 kb_fts_* 的 config/content/data/docsize/idx）与 sqlite_sequence；
--       保留 memory_fts / kb_fts 本体定义（影子期只读校验依赖它们）。
-- ===== table (25) =====
-- agent_accounts
CREATE TABLE agent_accounts (
  account_id   TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  host         TEXT,
  kind         TEXT,
  created_at   TEXT NOT NULL,
  closed_at    TEXT,
  note         TEXT
);

-- board_posts
CREATE TABLE board_posts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  board_id   INTEGER NOT NULL REFERENCES boards(id),
  citizen_id TEXT NOT NULL,
  kind       TEXT NOT NULL,               -- checkin | progress | done | note
  text       TEXT NOT NULL,               -- ≤200 字（渲染时硬截断）
  created_at TEXT NOT NULL
);

-- board_seen
CREATE TABLE "board_seen" (
  board_id         INTEGER NOT NULL,
  citizen_id       TEXT NOT NULL,
  session_key      TEXT NOT NULL DEFAULT '',
  last_post_id     INTEGER NOT NULL DEFAULT 0,
  last_presence_at TEXT NOT NULL DEFAULT '',
  updated_at       TEXT NOT NULL,
  PRIMARY KEY (board_id, citizen_id, session_key)
);

-- boards
CREATE TABLE boards (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  scope      TEXT NOT NULL,               -- root | project | branch
  scope_key  TEXT NOT NULL,               -- root='' / project='anyi-studio' / branch='<名字>'
  period_key TEXT NOT NULL,               -- YYYYMMDD（按日一块板）
  path       TEXT NOT NULL,               -- 渲染文件相对路径（派生，可重建）
  status     TEXT NOT NULL DEFAULT 'open',-- open | closed
  created_at TEXT NOT NULL,
  closed_at  TEXT,
  UNIQUE(scope, scope_key, period_key)
);

-- citizens
CREATE TABLE citizens (
  citizen_id   TEXT PRIMARY KEY,
  display_name TEXT,
  kind         TEXT,
  role         TEXT,
  host         TEXT,
  status       TEXT DEFAULT 'active',
  created_at   TEXT
, is_owner INTEGER DEFAULT 0, account_id TEXT, device_id TEXT, host_session_key TEXT, last_seen_at TEXT, archived_at TEXT);

-- citizens_archive
CREATE TABLE citizens_archive (
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
);

-- code_index
CREATE TABLE code_index (
  code        TEXT PRIMARY KEY,
  title       TEXT,
  project     TEXT,
  owner       TEXT,
  mem_type    TEXT,
  visibility  TEXT,
  status      TEXT DEFAULT 'active',
  tags        TEXT,
  file_path   TEXT,
  occurred_at TEXT,
  recorded_at TEXT
, last_accessed_at TEXT, access_count INTEGER DEFAULT 0, dormant INTEGER DEFAULT 0, session_actor TEXT, attachments TEXT, superseded_by TEXT);

-- code_registry
CREATE TABLE code_registry (
  prefix    TEXT PRIMARY KEY,
  namespace TEXT,
  last_date TEXT,
  last_seq  INTEGER DEFAULT 0,
  status    TEXT DEFAULT 'active'
);

-- config
CREATE TABLE config (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  type       TEXT NOT NULL DEFAULT 'string',
  updated_at TEXT
);

-- events
CREATE TABLE events (
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

-- kb_access
CREATE TABLE kb_access (
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
);

-- kb_fts
CREATE VIRTUAL TABLE kb_fts USING fts5(
    rel_path UNINDEXED,
    vault_id UNINDEXED,
    title,
    headings,
    summary,
    body_excerpt,
    tokenize='unicode61'
);

-- kb_index
CREATE TABLE kb_index (
  vault_id     TEXT NOT NULL,
  rel_path     TEXT NOT NULL,
  mtime        REAL NOT NULL,
  size         INTEGER NOT NULL,
  content_hash TEXT,
  title        TEXT,
  headings     TEXT,
  summary      TEXT,
  excerpt      TEXT,
  word_count   INTEGER,
  indexed_at   TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'active',
  error        TEXT,
  PRIMARY KEY (vault_id, rel_path)
);

-- kb_vault
CREATE TABLE kb_vault (
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
);

-- links
CREATE TABLE links (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  src        TEXT NOT NULL,
  dst        TEXT NOT NULL,
  rel        TEXT NOT NULL,
  source     TEXT NOT NULL,
  weight     REAL DEFAULT 1.0,
  evidence   TEXT,
  created_at TEXT NOT NULL,
  removed_at TEXT, last_reinforced_at TEXT, reinforce_count INTEGER DEFAULT 0, suppressed INTEGER DEFAULT 0,
  UNIQUE(src, dst, rel)
);

-- maintenance_task
CREATE TABLE maintenance_task (
  task_name      TEXT PRIMARY KEY,
  enabled        INTEGER NOT NULL DEFAULT 1,
  interval_hours INTEGER NOT NULL,
  last_run_at    TEXT,
  last_status    TEXT,
  last_error     TEXT,
  last_affected  INTEGER,
  last_seconds   REAL
);

-- memory_access_log
CREATE TABLE memory_access_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  code        TEXT NOT NULL,
  citizen_id  TEXT,
  session_key TEXT,
  project     TEXT,
  via         TEXT NOT NULL,
  payload     TEXT,
  accessed_at TEXT NOT NULL
);

-- memory_fts
CREATE VIRTUAL TABLE memory_fts USING fts5(
  code UNINDEXED,
  title_idx,
  body_idx,
  tokenize='unicode61'
);

-- name_aliases
CREATE TABLE name_aliases (
  kind       TEXT NOT NULL,       -- 'project' | 'citizen'
  from_name  TEXT NOT NULL,       -- 别名（非规范名）
  to_name    TEXT NOT NULL,       -- 规范名
  created_at TEXT NOT NULL,
  created_by TEXT,
  reason     TEXT,
  PRIMARY KEY (kind, from_name)
);

-- presence
CREATE TABLE "presence" (
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
);

-- project_stints
CREATE TABLE project_stints (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  citizen_id    TEXT NOT NULL,
  account_id    TEXT,
  project       TEXT NOT NULL,
  from_ts       TEXT NOT NULL,
  to_ts         TEXT,               -- NULL = 当前开档期
  set_by        TEXT,
  closed_reason TEXT
);

-- push_log
CREATE TABLE push_log (
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
  latency_sec   INTEGER
, content_hash TEXT);

-- session_tokens
CREATE TABLE session_tokens (
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
);

-- tool_activity
CREATE TABLE tool_activity (
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
);

-- workspace_map
CREATE TABLE workspace_map (
  path_prefix TEXT PRIMARY KEY,   -- 归一化后的路径前缀（正斜杠、无尾斜杠、全小写）
  project     TEXT NOT NULL,      -- 项目前缀（如 ANYI）
  note        TEXT,
  created_at  TEXT NOT NULL
);

-- ===== view (2) =====
-- citizens_all
CREATE VIEW citizens_all AS SELECT citizen_id, display_name, kind, role, host, status, created_at, is_owner, account_id, device_id, host_session_key, last_seen_at, archived_at FROM citizens UNION ALL SELECT citizen_id, display_name, kind, role, host, status, created_at, is_owner, account_id, device_id, host_session_key, last_seen_at, archived_at FROM citizens_archive;

-- kb_path_stats
CREATE VIEW kb_path_stats AS
SELECT vault_id,
       path,
       COUNT(*)                                                    AS hits,
       MAX(ts)                                                     AS last_at,
       SUM(CASE WHEN action='open' THEN 1 ELSE 0 END)               AS open_count,
       SUM(CASE WHEN action IN ('create','append','update') THEN 1 ELSE 0 END) AS write_count,
       SUM(CASE WHEN action='rollback' THEN 1 ELSE 0 END)           AS rollback_count,
       COUNT(DISTINCT citizen_id)                                   AS readers
FROM kb_access
GROUP BY vault_id, path;

-- ===== index (31) =====
-- ix_activity_citizen (on tool_activity)
CREATE INDEX ix_activity_citizen ON tool_activity(citizen_id, seen_at DESC);

-- ix_activity_dir (on tool_activity)
CREATE INDEX ix_activity_dir ON tool_activity(project, path_dir, seen_at DESC);

-- ix_activity_seen (on tool_activity)
CREATE INDEX ix_activity_seen ON tool_activity(seen_at);

-- ix_activity_session (on tool_activity)
CREATE INDEX ix_activity_session ON tool_activity(session_key, seen_at DESC);

-- ix_boards_scope (on boards)
CREATE INDEX ix_boards_scope ON boards(scope, scope_key, period_key);

-- ix_boards_status (on boards)
CREATE INDEX ix_boards_status ON boards(status, period_key);

-- ix_citizens_account (on citizens)
CREATE INDEX ix_citizens_account ON citizens(account_id);

-- ix_kb_access_action (on kb_access)
CREATE INDEX ix_kb_access_action ON kb_access(action, ts DESC);

-- ix_kb_access_citizen (on kb_access)
CREATE INDEX ix_kb_access_citizen ON kb_access(citizen_id, ts DESC);

-- ix_kb_access_path (on kb_access)
CREATE INDEX ix_kb_access_path ON kb_access(vault_id, path, ts DESC);

-- ix_kb_index_mtime (on kb_index)
CREATE INDEX ix_kb_index_mtime ON kb_index(mtime);

-- ix_kb_index_status (on kb_index)
CREATE INDEX ix_kb_index_status ON kb_index(status);

-- ix_links_dst (on links)
CREATE INDEX ix_links_dst ON links(dst) WHERE removed_at IS NULL;

-- ix_links_rel (on links)
CREATE INDEX ix_links_rel ON links(rel) WHERE removed_at IS NULL;

-- ix_links_src (on links)
CREATE INDEX ix_links_src ON links(src) WHERE removed_at IS NULL;

-- ix_links_suppressed (on links)
CREATE INDEX ix_links_suppressed ON links(suppressed) WHERE removed_at IS NULL;

-- ix_mal_citizen (on memory_access_log)
CREATE INDEX ix_mal_citizen ON memory_access_log(citizen_id, accessed_at);

-- ix_mal_code (on memory_access_log)
CREATE INDEX ix_mal_code ON memory_access_log(code, accessed_at);

-- ix_mal_session (on memory_access_log)
CREATE INDEX ix_mal_session ON memory_access_log(session_key, accessed_at);

-- ix_posts_board (on board_posts)
CREATE INDEX ix_posts_board ON board_posts(board_id, id DESC);

-- ix_presence_last (on presence)
CREATE INDEX ix_presence_last ON presence(board_id, last_seen_at);

-- ix_push_code (on push_log)
CREATE INDEX ix_push_code ON push_log(code, pushed_at);

-- ix_push_reaction (on push_log)
CREATE INDEX ix_push_reaction ON push_log(reaction) WHERE reaction IS NULL;

-- ix_push_session (on push_log)
CREATE INDEX ix_push_session ON push_log(session_key, pushed_at);

-- ix_session_tokens_citizen (on session_tokens)
CREATE INDEX ix_session_tokens_citizen ON session_tokens(citizen_id);

-- ix_session_tokens_session (on session_tokens)
CREATE INDEX ix_session_tokens_session ON session_tokens(session_key);

-- ux_citizens_owner (on citizens)
CREATE UNIQUE INDEX ux_citizens_owner ON citizens(is_owner) WHERE is_owner = 1;

-- ux_kb_vault_default (on kb_vault)
CREATE UNIQUE INDEX ux_kb_vault_default ON kb_vault(is_default) WHERE is_default = 1;

-- ux_kb_vault_path (on kb_vault)
CREATE UNIQUE INDEX ux_kb_vault_path ON kb_vault(path_key);

-- ux_links_edge (on links)
CREATE UNIQUE INDEX ux_links_edge ON links(src,dst,rel);

-- ux_stint_open (on project_stints)
CREATE UNIQUE INDEX ux_stint_open ON project_stints(citizen_id, project) WHERE to_ts IS NULL;

-- ===== trigger (2) =====
-- ev_no_delete (on events)
CREATE TRIGGER ev_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;

-- ev_no_update (on events)
CREATE TRIGGER ev_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;
