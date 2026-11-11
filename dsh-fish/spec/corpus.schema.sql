-- 语料库（corpus.sqlite）DDL 快照（只读导出，勿手工编辑）
-- 来源：p0/fish_corpus_store.py 的 DDL 常量（import 后原样导出，不解析字面量）
-- 生成：scripts/gen-spec.mjs ｜ Python 3.11.9 ｜ schema_version=1
-- 表：corpus_meta, corpus_source, corpus_session, corpus_segment, corpus_tombstone, corpus_degraded, corpus_event_stat, corpus_access_log, corpus_fts
-- 口径：语料库与主库零耦合（主库 DDL 在 schema.sql）；本文件只描述 corpus.sqlite。

CREATE TABLE IF NOT EXISTS corpus_meta (
  k TEXT PRIMARY KEY, v TEXT NOT NULL, updated_at TEXT NOT NULL);

-- 文件级游标：一行 = 一个源日志文件（幂等键 host+session_id）
CREATE TABLE IF NOT EXISTS corpus_source (
  host TEXT NOT NULL, session_id TEXT NOT NULL, path TEXT NOT NULL,
  format TEXT NOT NULL,
  bytes_total INTEGER NOT NULL DEFAULT 0,
  mtime_ns INTEGER NOT NULL DEFAULT 0,
  events_seen INTEGER NOT NULL DEFAULT 0, segments_kept INTEGER NOT NULL DEFAULT 0,
  last_seq_seen INTEGER, last_line_seen INTEGER,
  status TEXT NOT NULL DEFAULT 'ok',
  degraded_reason TEXT,
  first_ingested_at TEXT NOT NULL, last_ingested_at TEXT NOT NULL,
  PRIMARY KEY (host, session_id));

-- 会话级打标（段只存 session_id，标签挂会话上：一会话一个项目，R4）
CREATE TABLE IF NOT EXISTS corpus_session (
  host TEXT NOT NULL, session_id TEXT NOT NULL,
  sid8 TEXT NOT NULL,
  title TEXT, cwd TEXT,
  workspace TEXT NOT NULL DEFAULT '',
  project TEXT NOT NULL DEFAULT '',
  project_source TEXT NOT NULL,
  project_note TEXT,
  citizen_id TEXT, identity_source TEXT,
  origin TEXT NOT NULL DEFAULT 'main',
  origin_basis TEXT NOT NULL DEFAULT 'default',
  parent_session TEXT, delegation_depth INTEGER NOT NULL DEFAULT 0,
  role TEXT, agent_preset TEXT,
  conversation_type TEXT NOT NULL DEFAULT 'unknown',
  started_at TEXT, last_event_at TEXT,
  segments INTEGER NOT NULL DEFAULT 0, shadowed INTEGER NOT NULL DEFAULT 0,
  extra TEXT,
  PRIMARY KEY (host, session_id));

-- 段：一行 = 一个消息里的一个 text part（幂等键 host+session_id+loc+part）
CREATE TABLE IF NOT EXISTS corpus_segment (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  host TEXT NOT NULL, session_id TEXT NOT NULL,
  loc TEXT NOT NULL,
  part INTEGER NOT NULL DEFAULT 0,
  role TEXT NOT NULL CHECK (role IN ('user','assistant')),
  msg_id TEXT, turn INTEGER, step INTEGER,
  occurred_at TEXT NOT NULL,
  text TEXT NOT NULL,
  chars INTEGER NOT NULL, redacted_count INTEGER NOT NULL DEFAULT 0,
  raw_sha256 TEXT NOT NULL,
  byte_from INTEGER NOT NULL, byte_to INTEGER NOT NULL,
  offset_space TEXT NOT NULL,
  shadowed INTEGER NOT NULL DEFAULT 0,
  ingested_at TEXT NOT NULL,
  UNIQUE (host, session_id, loc, part),
  FOREIGN KEY (host, session_id) REFERENCES corpus_session(host, session_id));

CREATE INDEX IF NOT EXISTS ix_seg_time ON corpus_segment(occurred_at);
CREATE INDEX IF NOT EXISTS ix_seg_live
  ON corpus_segment(host, session_id, occurred_at) WHERE shadowed=0;
CREATE INDEX IF NOT EXISTS ix_seg_role ON corpus_segment(role);

-- 全文索引：派生表，可完整重建（范式照 memory_fts，P:955 与 P:3857-3897）。
-- 无触发器：bigram 预切只能在 Python 侧做，SQL 触发器切不出同一套 token。
CREATE VIRTUAL TABLE IF NOT EXISTS corpus_fts
  USING fts5(seg_id UNINDEXED, text_idx, tokenize='unicode61');

-- 删除台账：重建时跳过（C-P0-8「删了不复活」）
CREATE TABLE IF NOT EXISTS corpus_tombstone (
  scope_type TEXT NOT NULL,
  scope_key TEXT NOT NULL, created_at TEXT NOT NULL, reason TEXT,
  segments_deleted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (scope_type, scope_key));

-- 坏文件台账：C-P0-1/9 要求"必须列名出现"，所以它是一张表而不是一行日志
CREATE TABLE IF NOT EXISTS corpus_degraded (
  host TEXT NOT NULL, path TEXT NOT NULL, session_id TEXT, reason_code TEXT NOT NULL,
  reason TEXT, segments_kept INTEGER NOT NULL DEFAULT 0, hits INTEGER NOT NULL DEFAULT 1,
  first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, PRIMARY KEY (host, path));

-- "未知事件类型 → 计数跳过"的落点（C-P0-1 红②：不许整文件失败）
CREATE TABLE IF NOT EXISTS corpus_event_stat (
  host TEXT NOT NULL, session_id TEXT NOT NULL, event_type TEXT NOT NULL,
  seen INTEGER NOT NULL DEFAULT 0, kept INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0, last_at TEXT NOT NULL,
  PRIMARY KEY (host, session_id, event_type));

-- C-P0-8：**永不存 query 原文**（只存哈希与长度）
CREATE TABLE IF NOT EXISTS corpus_access_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, citizen_id TEXT, session_key TEXT,
  action TEXT NOT NULL,
  query_hash TEXT, qlen INTEGER, result_count INTEGER, pointers TEXT,
  via TEXT NOT NULL, ts TEXT NOT NULL);

CREATE INDEX IF NOT EXISTS ix_cal_ts ON corpus_access_log(ts);
