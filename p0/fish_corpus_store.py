#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语料库 `corpus.sqlite` 的唯一入口（原始对话语料层 · T01 · 设计 §3）。

为什么必须独立成库（D01）
------------------------
主库 `index.sqlite` 只有 6.7 MB，`events` 还是不可变的（生产快照 P:890-894）。
GB 级正文一旦进主库，`recall` / 注入包会跟着变慢，而且 `memory_fts` 会被语料洪水
稀释——"记忆检索"和"原文检索"是两种东西，混装之后两种都不好用。所以：

    index.sqlite  ←—— 只读引用（push_log 的 `conv:` 前缀）——→  corpus.sqlite
    记忆 / 看板 / 编码                                                语料正文

**这里不写主库一个字节**（设计 §4.3 ④）：所有落盘动作都只针对 `corpus.sqlite`。

三条纪律（都是踩过的坑，不是风格偏好）
------------------------------------
1. **游标必须落在帧边界**：DSH 日志是 zstd **多帧**追加，从中间字节起读只能拿到
   "未知帧描述符"（实测见 §4.1）。`bytes_total` 的含义因此是"已消费到某个完整帧的
   末尾"，不是"读过多少字节"。
2. **正确性靠 `BEGIN IMMEDIATE` + `busy_timeout`**（D06）：WAL 是单写多读，
   文件锁只能做"别白跑"的礼仪，做不了正确性——进程被杀会留死锁。
3. **幂等靠约束，不靠"我记得跑过"**：段表的 `UNIQUE(host,session_id,loc,part)`
   是唯一幂等键；`loc` 是宿主内稳定定位（`seq=1234` / `line=5678`），**不掺文本哈希**
   ——掺了之后源日志改写会产"新段"，而不是更新原段。

偏移空间（`offset_space`）为什么不统一
------------------------------------
DSH 的 `byte_from/byte_to` 是**解压流**里的偏移（zstd 的压缩偏移对不上 JSON 文本，
不声明就是假绿）；WB 是原文件偏移。所以每一行都显式带 `offset_space`，
`read()` 回原话时按它选回读方式。
"""
from __future__ import annotations

import contextlib
import hashlib
import re
import sqlite3
import time
from pathlib import Path

import fish_common

DB_NAME = "corpus.sqlite"

# 与主库同口径的 UTC 文本时间（`2026-10-27T04:06:43Z`）。所有时间列都存这个形态：
# 跨宿主、跨时区、跨机器比较时不用再猜"这是不是本地时间"（PRD §8.1 冻结形状）。
now_utc = fish_common.now_utc

# 游标/降级的枚举值。**只写在这里**：适配器与测试都从常量取，避免三处各写一份字符串
# （写错一个值不报错，只是统计对不上账——这类错最难查）。
FORMAT_DSH_V4 = "dsh.v4"
FORMAT_DSH_V3 = "dsh.v3"
FORMAT_DSH_LEGACY = "dsh.legacy"
FORMAT_WB_JSONL = "wb.jsonl"
FORMATS = (FORMAT_DSH_V4, FORMAT_DSH_V3, FORMAT_DSH_LEGACY, FORMAT_WB_JSONL)

SOURCE_OK = "ok"
SOURCE_DEGRADED = "degraded"
SOURCE_ORPHAN = "orphan"
SOURCE_STATUSES = (SOURCE_OK, SOURCE_DEGRADED, SOURCE_ORPHAN)

# C-P0-1/9：坏文件必须**列名**出现，所以原因码是闭集（新码在此登记再使用）。
REASON_TRUNCATED_FRAME = "truncated_frame"
REASON_BAD_JSON = "bad_json"
REASON_UNKNOWN_EVENT = "unknown_event"
REASON_OVERSIZE_LINE = "oversize_line"
REASON_UNSUPPORTED_FORMAT = "unsupported_format"
REASON_UNREADABLE = "unreadable"
REASON_ENCODING = "encoding"
# 设计给出的七码之外**新增一条**：会话头自相矛盾（如 `origin=subagent` 却没有
# `parentSession`，C-P0-3 ③ 要求判红）。把它勉强塞进上面任何一码都会让
# "为什么这个文件降级了"变得不可解释，故显式登记（见交付报告"偏离"）。
REASON_BAD_HEADER = "bad_header"
# 2026-10-27 三关复审新增两码（都属"我们**决定**不吞下这段数据"，必须可解释）：
# `oversize_frame` = 一帧解压输出超过上限（高压缩比炸弹 / 异常大帧）；
# `oversize_text`  = 单条消息的正文超过段上限被截断（截断是**声明过的**损失）。
REASON_OVERSIZE_FRAME = "oversize_frame"
REASON_OVERSIZE_TEXT = "oversize_text"
REASON_CODES = (REASON_TRUNCATED_FRAME, REASON_BAD_JSON, REASON_UNKNOWN_EVENT,
                REASON_OVERSIZE_LINE, REASON_UNSUPPORTED_FORMAT, REASON_UNREADABLE,
                REASON_ENCODING, REASON_BAD_HEADER, REASON_OVERSIZE_FRAME,
                REASON_OVERSIZE_TEXT)

# 段文本上限（≈0.5 MB/part，按 UTF-8 最坏 3 B/字反算）。两个适配器共用这一份：
# 单条异常消息不该把一整行撑到几百 MB（落库/回读/渲染都会连带变慢）。
MAX_PART_CHARS = 200000

# 会话被 `forget` 之后的**墓碑说明**（`corpus_session.project_note`）：
# 行保留是为了让"这段对话存在过、内容已删"可解释（删行会让对账差 1）。
FORGOTTEN_NOTE = "已 forget：标题与 cwd 已清除（正文与全文索引已删）"
PROJECT_FORGOTTEN = "forgotten"

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLES = (ROLE_USER, ROLE_ASSISTANT)

SCHEMA_VERSION = "1"

# 完整 DDL（设计 §3 逐字段落地）。**幂等**：全 `IF NOT EXISTS`，可反复执行。
DDL = """
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
"""

TABLES = ("corpus_meta", "corpus_source", "corpus_session", "corpus_segment",
          "corpus_tombstone", "corpus_degraded", "corpus_event_stat",
          "corpus_access_log", "corpus_fts")

# PRAGMA 口径。`cache_size` 取负数 = KiB（32 MiB）：语料读写不该把主库的页缓存挤掉。
# `secure_delete=ON`（M5）：`DELETE` 之后被释放的页先清零，明文才不会留在空闲页里
# ——"忘了"必须发生在字节层，而不只是发生在 `SELECT` 上。
BUSY_TIMEOUT_MS = 15000
# 擦除时的等待上限（安全 🟡 · 2026-10-27 复审）：`forget` 是**命令行动作**，而默认
# `busy_timeout=15 s` × 2 次重试在并发读者下实测要等 34 s——"删掉一条私密对话"不该
# 让人盯着终端半分钟。擦除期间把这个上限**只降不升**地压到 2 s：两轮最坏 ≈4.6 s，
# 失败照旧如实报 `busy`（不假装擦过），等待结束再复原调用方的口径。
ERASE_BUSY_TIMEOUT_MS = 2000
PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA cache_size=-32000",
    "PRAGMA secure_delete=ON",
    "PRAGMA busy_timeout=%d" % BUSY_TIMEOUT_MS,
)


class CorpusStoreError(RuntimeError):
    """语料库层面的失败（打不开/结构不对）。**与"库里没数据"区分开**。"""


# ------------------------------------------------------------------ 连接与建表


def db_path(root) -> Path:
    """库文件位置：`<root>/corpus.sqlite`（root 就是 `p0/data`，与主库同级）。"""
    return Path(root) / DB_NAME


def connect(path, *, readonly: bool = False,
            busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """打开语料库并设好 PRAGMA；`readonly=True` 走 `mode=ro` URI（MCP 侧只读）。

    为什么 `isolation_level=None`（自动提交）：本模块要自己发 `BEGIN IMMEDIATE`
    来控制"读游标 → 写段 → 写游标"这一段的原子性。Python 默认的隐式事务会在
    意料之外的地方开事务，`BEGIN IMMEDIATE` 就会撞上"事务里不能开事务"。

    `busy_timeout_ms` 可覆盖（M3）：审计/旁路写口要**短超时**——它等 15 秒就等于
    把"读一次语料"变成一次可能卡 15 秒的调用，而这发生在用户消息边界。
    """
    p = Path(path)
    if readonly:
        uri = "file:%s?mode=ro" % p.as_posix()
        conn = sqlite3.connect(uri, uri=True, timeout=15.0, isolation_level=None)
    else:
        conn = sqlite3.connect(str(p), timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in PRAGMAS:
        try:
            conn.execute(pragma)
        except sqlite3.OperationalError:
            # 只读连接上部分 PRAGMA 会被拒（如 journal_mode 切换）；不是错误。
            if not readonly:
                raise
    if int(busy_timeout_ms) != BUSY_TIMEOUT_MS:
        conn.execute("PRAGMA busy_timeout=%d" % int(busy_timeout_ms))
    return conn


def open_db(root, *, readonly: bool = False,
            busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """按 root 打开（必要时建父目录），并保证结构就位。**写模式才会建表。**"""
    p = db_path(root)
    if not readonly:
        p.parent.mkdir(parents=True, exist_ok=True)
    elif not p.exists():
        raise CorpusStoreError("语料库不存在：%s（先跑 corpus ingest）" % p)
    conn = connect(p, readonly=readonly, busy_timeout_ms=busy_timeout_ms)
    if not readonly:
        migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> dict:
    """建表 + 登记 schema 版本。可反复执行（幂等），返回 `{table: 行数}` 供体检用。

    **版本号是自愈式写入，不是"只报不改"**（2026-10-27 对齐注释与行为）：本层的
    所有升级都走 `CREATE ... IF NOT EXISTS` 幂等 DDL，所以"源码比库里新"这件事
    不需要人拍板就能收敛；而**改列/删列**那种真迁移仍不在这里做（那要人拍板）。
    为了不把"悄悄升版"变成不可追溯的事，旧版本号先落进 `corpus_meta` 的
    `schema_version_from`，体检/回溯时看得见这次是被谁改上去的。
    """
    conn.executescript(DDL)
    row = conn.execute("SELECT v FROM corpus_meta WHERE k='schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO corpus_meta (k, v, updated_at) VALUES ('schema_version', ?, ?)",
            (SCHEMA_VERSION, now_utc()))
    elif row[0] != SCHEMA_VERSION:
        # 幂等 DDL 已经把结构补齐了，这里只把版本号写成源码版本，并留下旧值。
        conn.execute(
            "INSERT INTO corpus_meta (k, v, updated_at) VALUES ('schema_version_from', ?, ?)"
            " ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
            (str(row[0]), now_utc()))
        conn.execute("UPDATE corpus_meta SET v=?, updated_at=? WHERE k='schema_version'",
                     (SCHEMA_VERSION, now_utc()))
    counts = {}
    for table in TABLES:
        counts[table] = conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
    return counts


@contextlib.contextmanager
def write_txn(conn: sqlite3.Connection):
    """`BEGIN IMMEDIATE` 包住一次批写；异常一律 ROLLBACK（不留半条脏段）。

    为什么 IMMEDIATE 而不是默认的 DEFERRED：D06——"读游标 → 写段 → 写游标"必须
    在一次写事务里完成，DEFERRED 会先拿读锁、升级写锁时可能直接 `database is locked`
    （SQLITE_BUSY 不可重试的那种死法）。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        try:
            conn.rollback()
        except sqlite3.Error as exc:
            fish_common.warn_ignored('fish_corpus_store.py:319 write_txn', exc)
        raise
    else:
        conn.commit()


# ------------------------------------------------------------------ 指针（门牌）


def sid8(session_id) -> str:
    """会话短址：剥掉可选 `session-` 前缀后取前 8 位。

    为什么剥：实测 515 个 DSH 会话里 77 个带 `session-`、438 个是裸 uuid，两种写法
    必须算出**同一个**短址，否则推送里同一会话有两个门牌（D04）。
    """
    s = str(session_id or "")
    if s.startswith("session-"):
        s = s[len("session-"):]
    return s[:8]


def canonical_pointer(host, session_id, loc, part: int = 0) -> str:
    """`log:<host>:<session_id>#<loc>[.<part>]`——可直接解析、粘进对话不歧义。"""
    base = "log:%s:%s#%s" % (host, session_id, loc)
    return base if not part else "%s.%d" % (base, part)


def push_pointer(host, session_id, loc, part: int = 0) -> str:
    """推送用缩写：`conv:<host>:<sid8>#<loc>[.<part>]`。

    为什么要两个形态（D04）：`push_log` 一行有 120 token 预算，会话真身 id 是
    `session-3931d23f-17c9-4de8-…`（46 字符）挤不进去；而 `read()` 又必须可解析，
    所以缩写只用于"给人看的那一行"，落库与回读仍走 canonical。
    """
    base = "conv:%s:%s#%s" % (host, sid8(session_id), loc)
    return base if not part else "%s.%d" % (base, part)


def session_key(host, session_id) -> str:
    """台账/范围里标识一个会话的键：`<host>:<session_id>`（`*` = 跨宿主）。

    为什么不用 `(host, session_id)` 元组当台账键：`corpus_tombstone` 的主键是
    两个文本列，写入前必须序列化成字符串；序列化形态**只有一份**（这里），
    否则 `forget` 写的键与 `rebuild` 读的键会各写一套，删掉的东西就复活了。
    """
    return "%s:%s" % (host, session_id)


_UTC_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def is_utc_stamp(text) -> bool:
    """是否是 `2026-10-27T04:06:43Z` 形态（库内时间列的**唯一**合法写法）。"""
    return bool(_UTC_STAMP_RE.match(str(text or "")))


class PointerError(CorpusStoreError):
    """门牌解析失败。三档语义（设计 §3"解析失败三档"），**不许猜一个最像的**。

    - `bad_pointer`        —— 形态就不对（拼错前缀、缺 `#loc`、loc 不是 seq=/line=）
    - `not_found`          —— 形态对，但库里没有这一段（或会话不存在）
    - `ambiguous_pointer`  —— `sid8` 命中了多个会话，必须由人消歧（回候选）
    """

    CODES = ("bad_pointer", "not_found", "ambiguous_pointer")

    def __init__(self, code, message, candidates=None):
        if code not in self.CODES:
            raise CorpusStoreError("未登记的门牌错误码：%s" % code)
        super().__init__(message)
        self.code = code
        self.candidates = list(candidates or [])


_POINTER_RE = re.compile(
    r"^(?P<kind>log|conv):(?P<host>[A-Za-z0-9_.-]+):(?P<sid>[^#\s]+)"
    r"#(?P<loc>(?:seq|line)=\d+)(?:\.(?P<part>\d+))?$")


def parse_pointer(pointer) -> dict:
    """纯字符串解析（不查库）。失败 → `PointerError('bad_pointer')`。"""
    text = str(pointer or "").strip()
    if not text:
        raise PointerError("bad_pointer", "门牌是空串")
    match = _POINTER_RE.match(text)
    if match is None:
        raise PointerError(
            "bad_pointer",
            "门牌形态不对：%r（要 log:<host>:<session_id>#seq=123[.1] 或 "
            "conv:<host>:<sid8>#seq=123）" % text)
    return {"kind": match.group("kind"), "host": match.group("host"),
            "sid": match.group("sid"), "loc": match.group("loc"),
            "part": int(match.group("part") or 0)}


def resolve_pointer(conn, pointer) -> dict:
    """解析 + 补全：`conv:` 按 `sid8` 查回完整 `session_id`（多命中必须歧义报错）。

    返回 `{kind, host, sid, session_id, loc, part, pointer, code}`。
    """
    parsed = parse_pointer(pointer)
    host = parsed["host"]
    if parsed["kind"] == "log":
        session_id = parsed["sid"]
    else:
        rows = conn.execute(
            "SELECT session_id FROM corpus_session WHERE host=? AND sid8=?"
            " ORDER BY session_id", (host, parsed["sid"])).fetchall()
        ids = [row[0] for row in rows]
        if not ids:
            raise PointerError("not_found",
                               "conv 缩写 %r 找不到会话（host=%s, sid8=%s）"
                               % (pointer, host, parsed["sid"]))
        if len(ids) > 1:
            raise PointerError(
                "ambiguous_pointer",
                "conv 缩写 %r 命中 %d 个会话，请用完整 log: 门牌" % (pointer, len(ids)),
                candidates=[canonical_pointer(host, sid, parsed["loc"], parsed["part"])
                            for sid in ids[:5]])
        session_id = ids[0]
    return {"kind": parsed["kind"], "host": host, "sid": parsed["sid"],
            "session_id": session_id, "loc": parsed["loc"], "part": parsed["part"],
            "pointer": canonical_pointer(host, session_id, parsed["loc"], parsed["part"]),
            "code": push_pointer(host, session_id, parsed["loc"], parsed["part"])}



# ------------------------------------------------------------------ 文本上限与映射


def cap_part_text(text) -> tuple:
    """把一段正文夹到 `MAX_PART_CHARS`，返回 `(文本, 是否被截断)`。

    为什么在这里夹而不是"信源不会超"：一条异常消息能把一整行撑到几百 MB，
    落库/回读/渲染都会连带变慢；截断是**声明过的**损失（进 `corpus_degraded`
    的 `oversize_text`），静默保留反而更糟（那种坏没人能当场发现）。
    """
    value = text or ""
    if len(value) <= MAX_PART_CHARS:
        return value, False
    return value[:MAX_PART_CHARS], True


def path_id_key(host, path_session_id) -> str:
    """`corpus_meta` 里"目录名 → 头部 id"映射的键（R1）。

    为什么要落盘：目录名与头部 id 不一致时，每轮都重新读一遍头部就会回到
    "重取 sid → 再判断不一致 → 再重取"的无限递归（实测 `RecursionError`）。
    映射落盘之后，第二轮的游标查找直接用真 id，**不再触发任何重入**。
    """
    return "path_id_mismatch:%s:%s" % (host, path_session_id)


def set_path_session(conn, host, path_session_id, true_session_id) -> None:
    """登记"目录名 ≠ 头部 id"（只写事实，不改任何段/游标）。"""
    set_meta(conn, path_id_key(host, path_session_id), str(true_session_id))


def resolve_path_session(conn, host, path_session_id) -> str:
    """目录名 → 真会话 id；没有映射就原样返回（**不猜**）。"""
    value = get_meta(conn, path_id_key(host, path_session_id))
    return str(value) if value else str(path_session_id)


def scopes_from_tombstones(tombstones) -> dict:
    """台账行 → 四档作用域的结构化视图（**全库唯一一份解析**）。

    为什么不能只解析 `session`/`host`（2026-10-27 复审 🟠-1）：`forget` 支持四种作用域
    （PRD C-P0-8），而采集侧只跳会话时，`pointer`/`range` 的删除会在下一次采集里复活
    ——源被重写 → 游标归零 → 重采，原文重新进库，台账只写不读。

    返回 `{"sessions": {(host, sid)}, "hosts": {h},
    "pointers": [(host, sid, loc, part)], "ranges": [(host|None, from, to)]}`。
    解析不了的台账行**跳过但不丢**（行仍在表里可人查）：坏键当成"没这个范围"会复活数据，
    当成"删全库"会误删，两种都不对，所以只忽略这一条并让 `doctor`/账本继续看得到它。
    """
    scopes = {"sessions": set(), "hosts": set(), "pointers": [], "ranges": []}
    for row in tombstones or ():
        scope, key = row["scope_type"], str(row["scope_key"])
        if scope == "session":
            host, _, sid = key.partition(":")
            scopes["sessions"].add((host, sid))
        elif scope == "host":
            scopes["hosts"].add(key)
        elif scope == "pointer":
            try:
                parsed = parse_pointer(key)
            except PointerError:
                continue
            if parsed["kind"] != "log":
                continue                   # 写侧只写 canonical（log:），conv: 缩写作废
            scopes["pointers"].append((parsed["host"], parsed["sid"], parsed["loc"],
                                       int(parsed["part"])))
        elif scope == "range":
            host, _, window = key.partition(":")
            start, _, end = window.partition("..")
            if is_utc_stamp(start) and is_utc_stamp(end):
                scopes["ranges"].append(
                    (None if host in ("", "*") else host, start, end))
    return scopes


def tombstone_scopes(conn) -> dict:
    """台账 → 四档作用域（采集/统计/重放共用；见 `scopes_from_tombstones`）。"""
    return scopes_from_tombstones(list_tombstones(conn))


def tombstone_skip_sets(conn) -> tuple:
    """台账 → `(跳过会话集合, 跳过宿主集合)`，键是 `(host, session_id)`。

    为什么放在 store 而不是 gov：采集侧（`ingest`）与治理侧（`rebuild`）必须用
    **同一份**口径判断"这段对话被删过"，否则 forget 之后日常采集会把它采回来
    （M1：删除只在某一次 `forget` 里成立，第二天就被日采复活）。
    """
    scopes = tombstone_scopes(conn)
    return scopes["sessions"], scopes["hosts"]


def is_session_forgotten(conn, host, session_id, skip_sessions=None,
                         skip_hosts=None) -> bool:
    """这段对话是否被台账删过——**全库唯一判据**（采集跳过 / 统计 / 重放共用）。

    为什么必须收敛成一份（2026-10-27 评审 🟠-1）：CLI 的唯一形态 `forget session:<sid>`
    **不带 host**，台账键于是写成 `session_key("*", sid)` = `*:<sid>`，语义是"这个会话，
    不管记在哪个宿主名下"。只比 `(host, sid)` 的那几处实现会让它删了等于没删——源被重写
    （游标归零）之后 ingest 把原话采回来（实测 `purged=0 / segments_new=3`），
    `stats.sessions` 也多算一份。三处各写一份判断，迟早有一处漏掉 `*`，所以只留这一份。

    `skip_sessions` / `skip_hosts` 可以传已算好的台账快照（循环里避免每行查一次库）；
    都不传时自己读一次台账。
    """
    if skip_sessions is None and skip_hosts is None:
        skip_sessions, skip_hosts = tombstone_skip_sets(conn)
    host, session_id = str(host), str(session_id)
    if host in (skip_hosts or ()):
        return True
    sessions = skip_sessions or ()
    return (host, session_id) in sessions or ("*", session_id) in sessions


def segment_tombstone_where(scopes) -> tuple:
    """段级台账（`pointer`/`range`）→ `(where_sql, args)`；式样为空时 `(None, [])`。

    段级作用域只能落到**段表**上（会话级在那里是"整份跳过"，段级要精确到
    `(host,session_id,loc,part)` 或时间段），所以单独给一份 SQL 片段，采集收口、
    统计口径、重放收口都拼这一份。
    """
    clauses, args = [], []
    for host, sid, loc, part in (scopes or {}).get("pointers") or ():
        clauses.append("(host=? AND session_id=? AND loc=? AND part=?)")
        args.extend([host, sid, loc, int(part)])
    for host, start, end in (scopes or {}).get("ranges") or ():
        if host:
            clauses.append("(host=? AND occurred_at>=? AND occurred_at<=?)")
            args.extend([host, start, end])
        else:
            clauses.append("(occurred_at>=? AND occurred_at<=?)")
            args.extend([start, end])
    if not clauses:
        return None, []
    return "(" + " OR ".join(clauses) + ")", args


def purge_segment_tombstones(conn, scopes=None) -> dict:
    """按 `pointer`/`range` 台账把**已经进库**的段再删一次（采集/重放收口）。

    为什么必须在采集侧也做（2026-10-27 复审 🟠-1）：这两种作用域是段级的，采集侧
    "跳会话"挡不住它们——源被重写（游标归零）或换个宿主重采之后，同一段会被再采一次。
    返回 `{"deleted", "sessions", "pointers", "ranges"}`；没有段级台账时零成本返回。
    """
    scopes = tombstone_scopes(conn) if scopes is None else scopes
    where, args = segment_tombstone_where(scopes)
    info = {"deleted": 0, "sessions": 0,
            "pointers": len((scopes or {}).get("pointers") or ()),
            "ranges": len((scopes or {}).get("ranges") or ())}
    if where is None:
        return info
    targets = [(row[0], row[1]) for row in conn.execute(
        "SELECT DISTINCT host, session_id FROM corpus_segment WHERE " + where,
        args).fetchall()]
    ids = [int(row[0]) for row in conn.execute(
        "SELECT id FROM corpus_segment WHERE " + where, args).fetchall()]
    info["sessions"] = len(targets)
    if not ids:
        return info
    with write_txn(conn):
        info["deleted"] = delete_segments(conn, ids)
        for host, sid in targets:
            refresh_session_counts(conn, host, sid)
            # 游标里的段数也是"事实读数"：不跟着改就会在 `stats.by_host.segments_kept`
            # 与段表之间长期对不上（复审 🟠-1 的"stats 同口径"）。
            conn.execute(
                "UPDATE corpus_source SET segments_kept=("
                "SELECT COUNT(*) FROM corpus_segment WHERE host=? AND session_id=?)"
                " WHERE host=? AND session_id=?", (host, sid, host, sid))
    return info


# ------------------------------------------------------------------ 游标（corpus_source）


def read_cursor(conn, host, session_id) -> "dict | None":
    """读文件级游标；没有行返回 None（= 这个文件还没采过）。"""
    row = conn.execute(
        "SELECT * FROM corpus_source WHERE host=? AND session_id=?",
        (host, session_id)).fetchone()
    return dict(row) if row else None


def upsert_source(conn, source: dict) -> None:
    """写文件级游标（全字段覆盖）。`path` 变了要跟着改——源文件可能被搬。"""
    ts = now_utc()
    conn.execute(
        "INSERT INTO corpus_source (host, session_id, path, format, bytes_total, mtime_ns,"
        " events_seen, segments_kept, last_seq_seen, last_line_seen, status,"
        " degraded_reason, first_ingested_at, last_ingested_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(host, session_id) DO UPDATE SET"
        "  path=excluded.path, format=excluded.format,"
        "  bytes_total=excluded.bytes_total, mtime_ns=excluded.mtime_ns,"
        "  events_seen=excluded.events_seen, segments_kept=excluded.segments_kept,"
        "  last_seq_seen=excluded.last_seq_seen, last_line_seen=excluded.last_line_seen,"
        "  status=excluded.status, degraded_reason=excluded.degraded_reason,"
        "  last_ingested_at=excluded.last_ingested_at",
        (source["host"], source["session_id"], source["path"],
         source.get("format", FORMAT_DSH_V4), int(source.get("bytes_total") or 0),
         int(source.get("mtime_ns") or 0), int(source.get("events_seen") or 0),
         int(source.get("segments_kept") or 0), source.get("last_seq_seen"),
         source.get("last_line_seen"), source.get("status", SOURCE_OK),
         source.get("degraded_reason"),
         source.get("first_ingested_at") or ts, ts))


def set_source_status(conn, host, session_id, status: str, reason=None) -> None:
    """只改状态（降级/孤儿标记），不动游标——下次还要从原位置续传。"""
    conn.execute(
        "UPDATE corpus_source SET status=?, degraded_reason=?, last_ingested_at=?"
        " WHERE host=? AND session_id=?",
        (status, reason, now_utc(), host, session_id))


def list_sources(conn, host=None) -> list:
    """所有文件的游标行（`stats` 与孤儿检测用）。"""
    if host:
        rows = conn.execute("SELECT * FROM corpus_source WHERE host=? ORDER BY host, session_id",
                            (host,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM corpus_source ORDER BY host, session_id").fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ 会话（corpus_session）


SESSION_FIELDS = (
    "host", "session_id", "sid8", "title", "cwd", "workspace", "project",
    "project_source", "project_note", "citizen_id", "identity_source", "origin",
    "origin_basis", "parent_session", "delegation_depth", "role", "agent_preset",
    "conversation_type", "started_at", "last_event_at", "extra",
)


def upsert_session(conn, session: dict) -> None:
    """写会话级标签。`segments`/`shadowed` 是**派生计数**，由 `refresh_session_counts` 校正。

    为什么标题/时间只"补不清空"：`session/title` 事件可能在标题定稿前后各来一次，
    后到的空标题不该把已有标题抹掉（抹掉之后推送里就只剩 `sid8` 了）。
    """
    s = {k: session.get(k) for k in SESSION_FIELDS}
    s["sid8"] = s.get("sid8") or sid8(s.get("session_id"))
    s["workspace"] = s.get("workspace") or ""
    s["project"] = s.get("project") or ""
    s["project_source"] = s.get("project_source") or "unmapped"
    s["origin"] = s.get("origin") or "main"
    s["origin_basis"] = s.get("origin_basis") or "default"
    s["conversation_type"] = s.get("conversation_type") or "unknown"
    s["delegation_depth"] = int(s.get("delegation_depth") or 0)
    conn.execute(
        "INSERT INTO corpus_session (host, session_id, sid8, title, cwd, workspace,"
        " project, project_source, project_note, citizen_id, identity_source, origin,"
        " origin_basis, parent_session, delegation_depth, role, agent_preset,"
        " conversation_type, started_at, last_event_at, segments, shadowed, extra)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,?)"
        " ON CONFLICT(host, session_id) DO UPDATE SET"
        "  sid8=excluded.sid8,"
        "  title=COALESCE(NULLIF(excluded.title,''), corpus_session.title),"
        "  cwd=COALESCE(NULLIF(excluded.cwd,''), corpus_session.cwd),"
        "  workspace=excluded.workspace, project=excluded.project,"
        "  project_source=excluded.project_source, project_note=excluded.project_note,"
        "  citizen_id=excluded.citizen_id, identity_source=excluded.identity_source,"
        "  origin=excluded.origin, origin_basis=excluded.origin_basis,"
        "  parent_session=COALESCE(excluded.parent_session, corpus_session.parent_session),"
        "  delegation_depth=MAX(excluded.delegation_depth, corpus_session.delegation_depth),"
        "  role=COALESCE(NULLIF(excluded.role,''), corpus_session.role),"
        "  agent_preset=COALESCE(NULLIF(excluded.agent_preset,''), corpus_session.agent_preset),"
        "  conversation_type=excluded.conversation_type,"
        "  started_at=COALESCE(NULLIF(excluded.started_at,''), corpus_session.started_at),"
        "  last_event_at=COALESCE(NULLIF(excluded.last_event_at,''), corpus_session.last_event_at),"
        "  extra=COALESCE(excluded.extra, corpus_session.extra)",
        (s["host"], s["session_id"], s["sid8"], s["title"], s["cwd"], s["workspace"],
         s["project"], s["project_source"], s["project_note"], s["citizen_id"],
         s["identity_source"], s["origin"], s["origin_basis"], s["parent_session"],
         s["delegation_depth"], s["role"], s["agent_preset"], s["conversation_type"],
         s["started_at"], s["last_event_at"], s["extra"]))


def get_session(conn, host, session_id) -> "dict | None":
    row = conn.execute("SELECT * FROM corpus_session WHERE host=? AND session_id=?",
                       (host, session_id)).fetchone()
    return dict(row) if row else None


def refresh_session_counts(conn, host, session_id) -> tuple:
    """校正会话的派生计数（段数 / 被压缩数）与游标里的段数。返回 `(segments, shadowed)`。

    为什么不累加：幂等重跑会 UPDATE 同一批段（不是新插入），累加会单调虚高；
    `COUNT(*)` 每次都从事实算，重跑多少次都是同一个数（C-P0-4）。

    为什么**顺手**改 `corpus_source.segments_kept`（评审 🟡-1）：`stats` 的
    `by_host.segments_kept` 读的是游标表、`stats.segments` 读的是段表实况，两份口径。
    旧实现只同步会话表，于是 `forget pointer/range` 之后两个读数会一直不一致，
    要等下一次 ingest 才收敛——"删完立刻看，统计还说着旧数"是最容易误导人的一种。
    """
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(shadowed),0) FROM corpus_segment"
        " WHERE host=? AND session_id=?", (host, session_id)).fetchone()
    seg, shadowed = int(row[0]), int(row[1])
    conn.execute("UPDATE corpus_session SET segments=?, shadowed=? WHERE host=? AND session_id=?",
                 (seg, shadowed, host, session_id))
    conn.execute("UPDATE corpus_source SET segments_kept=? WHERE host=? AND session_id=?",
                 (seg, host, session_id))
    return seg, shadowed


def count_segments(conn, host, session_id) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM corpus_segment WHERE host=? AND session_id=?",
        (host, session_id)).fetchone()[0])


def mark_session_forgotten(conn, host, session_id) -> None:
    """把会话行**墓碑化**（M1）：清标题/cwd/workspace，标 `project_source='forgotten'`。

    为什么不删这一行：`segments=0` + 台账命中能解释"这段对话存在过、内容已删除"；
    把行删掉之后，`stats` 里"采了 40 个会话、库里有 39 行"就变成无法解释的差 1。
    为什么要清 title/cwd：标题与路径本身就是内容（"XX项目 token 泄露"），
    `forget` 之后它们留在库里等于没忘干净。
    """
    conn.execute(
        "UPDATE corpus_session SET title=NULL, cwd=NULL, workspace='', project='',"
        " project_source=?, project_note=?, segments=0, shadowed=0"
        " WHERE host=? AND session_id=?",
        (PROJECT_FORGOTTEN, FORGOTTEN_NOTE, host, session_id))
    conn.execute("UPDATE corpus_source SET segments_kept=0 WHERE host=? AND session_id=?",
                 (host, session_id))


def forgotten_sessions(conn) -> tuple:
    """`(会话集合, 宿主集合)`：`stats` 不计已删（与采集侧同一份台账口径）。"""
    return tombstone_skip_sets(conn)


# ------------------------------------------------------------------ 段（corpus_segment + FTS）


def upsert_segment(conn, seg: dict) -> tuple:
    """写一段（含 FTS），返回 `(seg_id, created)`。

    `created=False` 表示这次是**更新**已有段：C-P0-4「重跑段数不变」靠的就是这个
    区分——判据看段数，而"更新"不增行。
    """
    host, sid = seg["host"], seg["session_id"]
    loc, part = seg["loc"], int(seg.get("part") or 0)
    row = conn.execute(
        "SELECT id FROM corpus_segment WHERE host=? AND session_id=? AND loc=? AND part=?",
        (host, sid, loc, part)).fetchone()
    text = seg["text"] or ""
    ts = seg.get("ingested_at") or now_utc()
    if row is None:
        cur = conn.execute(
            "INSERT INTO corpus_segment (host, session_id, loc, part, role, msg_id, turn,"
            " step, occurred_at, text, chars, redacted_count, raw_sha256, byte_from,"
            " byte_to, offset_space, shadowed, ingested_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (host, sid, loc, part, seg["role"], seg.get("msg_id"), seg.get("turn"),
             seg.get("step"), seg["occurred_at"], text, int(seg.get("chars") or len(text)),
             int(seg.get("redacted_count") or 0), seg["raw_sha256"],
             int(seg.get("byte_from") or 0), int(seg.get("byte_to") or 0),
             seg.get("offset_space") or "decompressed", int(seg.get("shadowed") or 0), ts))
        seg_id, created = int(cur.lastrowid), True
    else:
        seg_id = int(row[0])
        conn.execute(
            "UPDATE corpus_segment SET role=?, msg_id=?, turn=?, step=?, occurred_at=?,"
            " text=?, chars=?, redacted_count=?, raw_sha256=?, byte_from=?, byte_to=?,"
            " offset_space=?, shadowed=?, ingested_at=?"
            " WHERE id=?",
            (seg["role"], seg.get("msg_id"), seg.get("turn"), seg.get("step"),
             seg["occurred_at"], text, int(seg.get("chars") or len(text)),
             int(seg.get("redacted_count") or 0), seg["raw_sha256"],
             int(seg.get("byte_from") or 0), int(seg.get("byte_to") or 0),
             seg.get("offset_space") or "decompressed", int(seg.get("shadowed") or 0),
             ts, seg_id))
        created = False
    # FTS 与段同事务：DELETE + INSERT 是 memory_fts 的既有幂等范式（P:3892-3897）。
    index_fts(conn, seg_id, text)
    return seg_id, created


def index_fts(conn, seg_id: int, text: str) -> None:
    """把一段写进全文索引（覆盖式）。切词必须走 `fish_common` 的唯一一份实现（D08/D09）。"""
    conn.execute("DELETE FROM corpus_fts WHERE seg_id=?", (seg_id,))
    conn.execute("INSERT INTO corpus_fts (seg_id, text_idx) VALUES (?,?)",
                 (seg_id, fish_common.fts_text(text)))


def mark_shadowed(conn, host, session_id, locs) -> int:
    """`compaction/prune` 命中 → 只打标不删（实测被裁消息仍在日志里）。

    为什么保留：PRD §3.1 铁律说"被裁消息仍照采"，删掉就等于让"当时的上下文"
    变成一个无法解释的空洞；打标之后检索可以按需排除，而回原话仍然回得到。
    """
    n = 0
    for loc in locs:
        cur = conn.execute(
            "UPDATE corpus_segment SET shadowed=1 WHERE host=? AND session_id=? AND loc=?",
            (host, session_id, loc))
        n += cur.rowcount or 0
    return n


def get_segment(conn, host, session_id, loc, part: int = 0) -> "dict | None":
    row = conn.execute(
        "SELECT s.*, ss.title AS session_title, ss.project, ss.workspace,"
        " ss.citizen_id, ss.origin, ss.cwd FROM corpus_segment s"
        " LEFT JOIN corpus_session ss ON ss.host=s.host AND ss.session_id=s.session_id"
        " WHERE s.host=? AND s.session_id=? AND s.loc=? AND s.part=?",
        (host, session_id, loc, part)).fetchone()
    return dict(row) if row else None


def delete_segments(conn, ids) -> int:
    """按段 id 删（**先 FTS 后正文**），返回删除行数。不写 tombstone（那是治理层的事）。

    为什么先删 FTS：反过来的话，中途失败会留下"索引里有、正文没了"的段——
    `search` 能命中一个读不出来的门牌，那是最难解释的一种坏。
    """
    ids = [int(i) for i in (ids or ())]
    if not ids:
        return 0
    total = 0
    for start in range(0, len(ids), _DELETE_CHUNK):
        chunk = ids[start:start + _DELETE_CHUNK]
        marks = ",".join("?" * len(chunk))
        conn.execute("DELETE FROM corpus_fts WHERE seg_id IN (%s)" % marks, chunk)
        cur = conn.execute("DELETE FROM corpus_segment WHERE id IN (%s)" % marks, chunk)
        total += int(cur.rowcount or 0)
    return total


# SQLite 的变量上限默认 999，一次删几千条会直接抛错（而"删了一半"更糟）。
_DELETE_CHUNK = 500


def _file_bytes(conn) -> dict:
    """库文件与 `-wal` 的当前字节数（擦除回执要能被人核对，DEL-01）。"""
    info = {"db_bytes": None, "wal_bytes": None}
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return info
    path = str(row[2] or "") if row else ""
    if not path:
        return info                            # 内存库：没有"盘上字节"可读
    try:
        info["db_bytes"] = Path(path).stat().st_size
    except OSError:
        info["db_bytes"] = None
    wal = Path(path + "-wal")
    try:
        info["wal_bytes"] = wal.stat().st_size if wal.exists() else 0
    except OSError:
        info["wal_bytes"] = None
    return info


def _busy_timeout_of(conn) -> "int | None":
    """读连接的 `busy_timeout`（毫秒）；读不到返回 None（不当成错误）。"""
    try:
        row = conn.execute("PRAGMA busy_timeout").fetchone()
        return int(row[0]) if row is not None else None
    except (sqlite3.Error, TypeError, IndexError, ValueError):
        return None


def _set_busy_timeout(conn, value) -> None:
    """设 `busy_timeout`；失败**不抛**（擦除的主逻辑不该被一条 PRAGMA 打断）。"""
    if value is None:
        return
    try:
        conn.execute("PRAGMA busy_timeout=%d" % int(value))
    except (sqlite3.Error, TypeError, ValueError) as exc:
        fish_common.warn_ignored('fish_corpus_store.py:921 _set_busy_timeout', exc)


def secure_erase(conn, *, vacuum: bool = True, retries: int = 2) -> dict:
    """把删掉的明文从**空闲页与 WAL** 里真正擦掉（M5 / 安全 DEL-01）。

    为什么必须显式做：`DELETE` 只把页标记为空闲，明文仍留在 `corpus.sqlite`
    与 `corpus.sqlite-wal` 里——"忘了"在字节层根本没发生（安全官实测：`forget`
    之后 `strings corpus.sqlite | grep <秘密>` 仍能命中）。三步：

      ① `secure_delete=ON`（连接级，`PRAGMAS` 已开）：后续覆盖写把释放的页清零；
      ② `VACUUM`：整库重写一遍，旧空闲页被挤掉（库级操作，**不能在事务里**跑）；
      ③ `wal_checkpoint(TRUNCATE)`：把 WAL 截回 0 字节（否则明文躺在 -wal 里）。

    **`busy` 不等于成功**（2026-10-27 安全 DEL-01）：checkpoint 的返回值是
    `(busy, log, checkpointed)`，`busy=1` 表示有别的连接正在读写、WAL 只合了一部分
    ——WAL 没被截断，明文可能还在里面。旧实现把这种现象当成功（`error` 恒 None），
    回执于是撒谎。现在：`ok=False` + `error` 写清原因，并**重试一次**（短暂让路）。

    回执字段：`ok / vacuumed / checkpoint / checkpoint_busy / attempts /
    busy_timeout_ms / wal_bytes / db_bytes / error`。

    **等待上限**（2026-10-27 安全 🟡）：擦除期间把 `busy_timeout` 压到
    `ERASE_BUSY_TIMEOUT_MS`（只降不升），结束后复原调用方的口径。旧实现沿用它继承来的
    15 s：并发读者下 `wal_checkpoint(TRUNCATE)` 会等满 15 s × 2 次 = 34 s，`forget`
    于是变成一次"挂住半分钟"的猜测。现在最坏 ≈4.6 s，且失败仍然如实报 `busy`。

    **诚实边界**（见 `docs/依赖说明-corpus.md` §6）：SSD 磨损均衡、文件系统快照、
    备份文件里的旧副本都不在本函数能力内；要做到磁盘级不可恢复需要全盘 scrub。
    本函数只保证"这两个文件里现在读不到"，且**需要无并发读者**——有并发时它会明说失败。
    """
    info = {"ok": False, "vacuum": bool(vacuum), "vacuumed": False,
            "checkpoint": None, "checkpoint_busy": None,
            "attempts": 0, "db_bytes": None, "wal_bytes": None, "error": None,
            "busy_timeout_ms": None}
    attempts = max(1, int(retries))
    restore_ms = _busy_timeout_of(conn)
    _set_busy_timeout(conn, ERASE_BUSY_TIMEOUT_MS
                      if restore_ms is None else min(restore_ms, ERASE_BUSY_TIMEOUT_MS))
    info["busy_timeout_ms"] = _busy_timeout_of(conn)
    try:
        for attempt in range(attempts):
            info["attempts"] = attempt + 1
            try:
                conn.execute("PRAGMA secure_delete=ON")
                if vacuum and not info["vacuumed"]:
                    conn.execute("VACUUM")
                    info["vacuumed"] = True
                row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            except sqlite3.Error as exc:        # 只读连接/被占用：如实报，不假装擦过
                info["error"] = "%s: %s" % (type(exc).__name__, exc)
                break
            if row is None:
                info["error"] = "wal_checkpoint(TRUNCATE) 没有返回行：无法确认 WAL 已截断"
                continue
            info["checkpoint"] = list(row)
            info["checkpoint_busy"] = int(row[0])
            if int(row[0]) == 0:
                info["ok"] = True
                info["error"] = None
                break
            info["error"] = ("checkpoint busy=1：有其它连接正在读写，WAL 未截断"
                             "（明文可能仍在 -wal 里）")
            if attempt + 1 < attempts:
                time.sleep(0.2 * (attempt + 1))   # 让路一拍再重试（不猜"马上就好"）
    finally:
        _set_busy_timeout(conn, restore_ms)      # 复原调用方的等待口径（不偷偷改它）
    info.update(_file_bytes(conn))
    return info

# 检索的过滤键（设计 §6）：全 AND；同一键多值 = OR；空列表 = 不限。
# **`project=['']` 是"只要未归属"**，不是"不限"——这正是 `''`≠NULL 的意义。
FILTER_COLUMNS = {
    "host": "s.host", "project": "ss.project", "workspace": "ss.workspace",
    "session": "s.session_id", "citizen": "ss.citizen_id", "role": "s.role",
    "origin": "ss.origin",
}
FILTER_KEYS = tuple(FILTER_COLUMNS)

_FTS_SELECT = (
    "SELECT s.id AS seg_id, s.host, s.session_id, s.loc, s.part, s.role,"
    " s.occurred_at, s.text, s.chars, s.redacted_count, s.shadowed,"
    " ss.sid8, ss.title AS session_title, ss.project, ss.workspace,"
    " ss.citizen_id, ss.origin, bm25(corpus_fts) AS bm25"
    " FROM corpus_fts"
    " JOIN corpus_segment s ON s.id=corpus_fts.seg_id"
    " LEFT JOIN corpus_session ss ON ss.host=s.host AND ss.session_id=s.session_id"
    " WHERE corpus_fts MATCH ?")


def build_filter_sql(filters, *, include_shadowed: bool = False, time_from=None,
                     time_to=None) -> tuple:
    """过滤条件 → `(where_sql, args)`。未知键/本地时间形态**响亮失败**。"""
    where, args = [], []
    for key, value in (filters or {}).items():
        column = FILTER_COLUMNS.get(key)
        if column is None:
            raise CorpusStoreError("未知过滤键 %r（只认 %s）"
                                   % (key, "/".join(FILTER_KEYS)))
        values = value if isinstance(value, (list, tuple, set)) else [value]
        values = [str(v) for v in values if v is not None]
        if not values:
            continue                       # 空列表 = 不限（不是"限定为空"）
        where.append("%s IN (%s)" % (column, ",".join("?" * len(values))))
        args.extend(values)
    if not include_shadowed:
        where.append("s.shadowed=0")       # 默认不出现被压缩出上下文的段
    for name, value, op in (("time_from", time_from, ">="), ("time_to", time_to, "<=")):
        if not value:
            continue
        if not is_utc_stamp(value):
            raise CorpusStoreError(
                "%s 必须是 UTC 'Z' 形式（如 2026-10-27T00:00:00Z），收到 %r"
                "——本地时间会静默差 8 小时" % (name, value))
        where.append("s.occurred_at" + op + "?")
        args.append(str(value))
    return (" AND ".join(where) if where else "1=1"), args


def search_segments(conn, match_expr, *, filters=None, include_shadowed: bool = False,
                    time_from=None, time_to=None, limit: int = 64) -> list:
    """一次 SQL 把 MATCH + 全部过滤下推（过滤先于排序，排序看不到被过滤掉的段）。"""
    where, args = build_filter_sql(filters, include_shadowed=include_shadowed,
                                   time_from=time_from, time_to=time_to)
    sql = _FTS_SELECT + " AND " + where + " ORDER BY bm25(corpus_fts) LIMIT ?"
    rows = conn.execute(sql, [match_expr] + args + [int(limit)]).fetchall()
    return [dict(row) for row in rows]


def count_matching(conn, match_expr, *, filters=None, include_shadowed: bool = False,
                   time_from=None, time_to=None) -> int:
    """同一次过滤下的**总命中数**（`meta.total_matched` 与 `returned` 要能对账）。"""
    where, args = build_filter_sql(filters, include_shadowed=include_shadowed,
                                   time_from=time_from, time_to=time_to)
    sql = ("SELECT COUNT(*) FROM corpus_fts"
           " JOIN corpus_segment s ON s.id=corpus_fts.seg_id"
           " LEFT JOIN corpus_session ss ON ss.host=s.host AND ss.session_id=s.session_id"
           " WHERE corpus_fts MATCH ? AND " + where)
    return int(conn.execute(sql, [match_expr] + args).fetchone()[0])



# ------------------------------------------------------------------ 降级 / 事件统计


def record_degraded(conn, host, path, session_id, reason_code: str, reason: str,
                    segments_kept: int = 0) -> None:
    """登记一个坏文件（同文件反复失败只增 `hits`，不刷屏）。

    `segments_kept` 传**累计已采到的段数**（不是本次新增数）：C-P0-1 要求回执里能
    看出"这个文件到底采到多少"，本次新增数在反复重试时会忽大忽小，不可用作读数。
    """
    if reason_code not in REASON_CODES:
        raise CorpusStoreError("未登记的降级原因码：%s" % reason_code)
    ts = now_utc()
    conn.execute(
        "INSERT INTO corpus_degraded (host, path, session_id, reason_code, reason,"
        " segments_kept, hits, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,1,?,?)"
        " ON CONFLICT(host, path) DO UPDATE SET"
        "  session_id=excluded.session_id, reason_code=excluded.reason_code,"
        "  reason=excluded.reason, segments_kept=excluded.segments_kept,"
        "  hits=corpus_degraded.hits+1, last_seen_at=excluded.last_seen_at",
        (host, str(path), session_id, reason_code, reason, int(segments_kept), ts, ts))


def bump_event_stat(conn, host, session_id, event_type: str, *, seen: int = 1,
                    kept: int = 0, skipped: int = 0) -> None:
    """累加某类事件的计数（幂等重跑会重复累加——**这是有意的**：它记的是"见过多少次"）。"""
    conn.execute(
        "INSERT INTO corpus_event_stat (host, session_id, event_type, seen, kept, skipped,"
        " last_at) VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(host, session_id, event_type) DO UPDATE SET"
        "  seen=corpus_event_stat.seen+excluded.seen,"
        "  kept=corpus_event_stat.kept+excluded.kept,"
        "  skipped=corpus_event_stat.skipped+excluded.skipped,"
        "  last_at=excluded.last_at",
        (host, session_id, event_type, int(seen), int(kept), int(skipped), now_utc()))


def list_degraded(conn, host=None) -> list:
    if host:
        rows = conn.execute(
            "SELECT * FROM corpus_degraded WHERE host=? ORDER BY last_seen_at DESC",
            (host,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM corpus_degraded ORDER BY last_seen_at DESC").fetchall()
    return [dict(r) for r in rows]


def list_event_stats(conn, host=None, session_id=None) -> list:
    sql = "SELECT * FROM corpus_event_stat WHERE 1=1"
    args = []
    if host:
        sql += " AND host=?"
        args.append(host)
    if session_id:
        sql += " AND session_id=?"
        args.append(session_id)
    sql += " ORDER BY host, session_id, event_type"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


# ------------------------------------------------------------------ 删除台账（tombstone）

TOMBSTONE_SCOPES = ("session", "host", "pointer", "range")


def upsert_tombstone(conn, scope_type, scope_key, *, reason=None,
                     segments_deleted: int = 0) -> None:
    """写（累加）一条删除台账。**`forget` 必须在删段之前调用它**（D14）。

    `segments_deleted` 累加而不是覆盖：同一范围被删两次是两次真实动作，
    覆盖会让"这条到底删掉了多少"变成一个随最后一次动作漂移的数字。
    """
    if scope_type not in TOMBSTONE_SCOPES:
        raise CorpusStoreError("未登记的台账范围：%s（只认 %s）"
                               % (scope_type, "/".join(TOMBSTONE_SCOPES)))
    key = str(scope_key or "").strip()
    if not key:
        raise CorpusStoreError("台账键不能为空")
    ts = now_utc()
    conn.execute(
        "INSERT INTO corpus_tombstone (scope_type, scope_key, created_at, reason,"
        " segments_deleted) VALUES (?,?,?,?,?)"
        " ON CONFLICT(scope_type, scope_key) DO UPDATE SET"
        "  created_at=excluded.created_at,"
        "  reason=COALESCE(excluded.reason, corpus_tombstone.reason),"
        "  segments_deleted=corpus_tombstone.segments_deleted+excluded.segments_deleted",
        (scope_type, key, ts, reason, int(segments_deleted)))


def list_tombstones(conn, scope_type=None) -> list:
    if scope_type:
        rows = conn.execute(
            "SELECT * FROM corpus_tombstone WHERE scope_type=?"
            " ORDER BY created_at, scope_key", (scope_type,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM corpus_tombstone ORDER BY created_at, scope_key").fetchall()
    return [dict(row) for row in rows]



# ------------------------------------------------------------------ 审计 / 元数据

# 停滞哨兵（C-P0-9 红②）的三个读数键。**放在 store**：`fish_corpus` 写、`gov.doctor`
# 读，键名分两处写就是两套口径（复审 🟡：报告说 doctor 打印停滞，实际它读不到）。
# 指纹键从 `stall.mtime_sig` 改成 `stall.source_sig`（复审 🟠-2：纯 mtime 触碰不是
# "源在动"，指纹必须只由**事实**决定：游标位置 / 事件数 / 段数）。
META_STALL_STREAK = "stall.zero_streak"
META_STALL_SIG = "stall.source_sig"
META_STALL_ALARM = "stall.alarm"

# 采集时间窗口（2026-10-27 用户决策：只投影最近 3 天，更早留待后续回填）的两个读数键。
# 同样放在 store：`fish_corpus.ingest` 写、`fish_corpus.stats` 读——键名分两处写就是
# 两套口径（谁写谁读对不上时，读数会静默变成 0）。存的是**上一轮的口径与计数**，
# 不是文件清单（被窗口跳过的文件不入库，这里只留"跳过了几个"）。
META_INGEST_WINDOW_DAYS = "ingest.window_days"
META_INGEST_SKIPPED_BY_WINDOW = "ingest.skipped_by_window"


def write_access_log(conn, *, action: str, via: str, citizen_id=None, session_key=None,
                     query=None, result_count=None, pointers=None) -> None:
    """写一条访问审计。**query 只存 sha256 + 长度**（C-P0-8 ②：永不存原文）。"""
    qhash = None
    qlen = None
    if query is not None:
        qhash = hashlib.sha256(str(query).encode("utf-8")).hexdigest()
        qlen = len(str(query))
    conn.execute(
        "INSERT INTO corpus_access_log (citizen_id, session_key, action, query_hash, qlen,"
        " result_count, pointers, via, ts) VALUES (?,?,?,?,?,?,?,?,?)",
        (citizen_id, session_key, action, qhash, qlen, result_count, pointers, via,
         now_utc()))


def list_access_log(conn, limit: int = 20) -> list:
    rows = conn.execute(
        "SELECT * FROM corpus_access_log ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def text_sha256(text) -> str:
    """**脱敏前**切片的 sha256（`raw_sha256`）。

    为什么存脱敏前：它要能证明"这段来自哪里、有没有被改过"，同时**不回存秘密**
    ——哈希是一去不回的，脱敏后的正文才落盘（C-P0-7 的正向判据靠它核来源）。
    """
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def get_meta(conn, key: str, default=None):
    row = conn.execute("SELECT v FROM corpus_meta WHERE k=?", (key,)).fetchone()
    return row[0] if row else default

def set_meta(conn, key: str, value) -> None:
    conn.execute(
        "INSERT INTO corpus_meta (k, v, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
        (key, str(value), now_utc()))


# ------------------------------------------------------------------ 类门面（设计 §3 类图）


class CorpusStore:
    """`CorpusStore`（类图）：把上面这些函数收在一个可传递的对象里。

    为什么保留函数层而不是只留类：适配器与测试里大量"单条 upsert"的调用点，
    传一个 `conn` 比传一个对象更省一层间接；但类图要求有一个能注入的写口，
    所以两者都在——**类只做转发，不复制实现**（复制实现就是两份口径的开始）。
    """

    def __init__(self, path):
        self.path = Path(path)
        self.conn = None

    # 生命周期 ----------------------------------------------------------------
    def connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        """打开连接（`readonly=True` 走 `mode=ro`）。重复调用返回同一连接。"""
        if self.conn is None:
            self.conn = connect(self.path, readonly=readonly)
        return self.conn

    def migrate(self, conn=None) -> dict:
        return migrate(conn if conn is not None else self.connect())

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        self.connect()
        self.migrate()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # 游标 --------------------------------------------------------------------
    def read_cursor(self, host, session_id):
        return read_cursor(self.connect(), host, session_id)

    def set_cursor(self, source: dict) -> None:
        upsert_source(self.connect(), source)

    # 标签与正文 --------------------------------------------------------------
    def upsert_session(self, session: dict) -> None:
        upsert_session(self.connect(), session)

    def upsert_segment(self, seg: dict) -> tuple:
        return upsert_segment(self.connect(), seg)

    def degraded(self, host, path, session_id, reason_code, reason, segments_kept=0) -> None:
        record_degraded(self.connect(), host, path, session_id, reason_code, reason,
                        segments_kept)

    def event_stat(self, host, session_id, event_type, *, seen=1, kept=0, skipped=0) -> None:
        bump_event_stat(self.connect(), host, session_id, event_type, seen=seen,
                        kept=kept, skipped=skipped)

    def txn(self):
        return write_txn(self.connect())
