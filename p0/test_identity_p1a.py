#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fish 身份层 · P1-a（纯结构）验收
=================================
本轮只做结构，**不动一句既有查询**。六件事 → 七条判据：

  A  新库结构齐全：citizens 13 列、4 张新表 + citizens_all 视图 + 4 条索引都在
  B  旧库升级补列：8 列旧 citizens（含 1 行数据）→ 13 列，原有数据不丢
  C  citizens_all 能查到热表里的行
  D  归档后视图仍能查到它，且**只有一条**
  E  ux_stint_open 生效（同一 citizen 的第二个开档期被拒）
  F  session_tokens 的 4 条 CHECK 逐条生效
  G  既有 citizens 引用零改动（28 条 SQL 逐条冻结比对）

纪律（03 §G / 09 RX-2，与 test_push_p0 同一套）：**每条判据都要先证明它在坏输入下会红**。
所以每条判据都配一条 `*_mutation_*`：把对应结构摘掉/改坏，再把**同一组断言**重跑一遍，
它必须报错。若它照样绿，说明断言没有判别力（恒绿判据）。

变异形态（10 §6.1）：
  · code    —— mock.patch 摘掉补列清单 / 索引清单 / 建表 DDL
  · fixture —— 造缺列旧库、缺 CHECK 旧表、少一列的归档表、写错的视图、被改过的源码

为什么判据 A 要连"归档表与热表逐列一致"一起断言：citizens_all 是 UNION ALL，只按**列位置**
拼，不看列名。两张表结构一旦漂开，视图会静默错位（而不是报错）——那是本轮最昂贵的失手。
"""
import ast
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fish  # noqa: E402

# 13 列的**顺序**就是 citizens_all 的拼接口。刻意手写一份、不从 fish 里读：
# 判据必须独立于被测实现，否则"实现自己和自己一致"是恒绿。
CITIZEN_COLUMNS = (
    "citizen_id", "display_name", "kind", "role", "host", "status", "created_at",
    "is_owner", "account_id", "device_id", "host_session_key", "last_seen_at",
    "archived_at",
)
NEW_TABLES = ("citizens_archive", "agent_accounts", "session_tokens", "project_stints")
NEW_INDEXES = ("ux_stint_open", "ix_session_tokens_citizen", "ix_session_tokens_session",
               "ix_citizens_account")

# 旧库夹具：**恰好是本轮改动前**的库形态——citizens 8 列、事件/编码/配置表都是老样子，
# 并且真有 1 行公民数据（"升级不动已有数据"这条要有东西可动才算验过）。
LEGACY_SCHEMA = """
CREATE TABLE events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, ts TEXT NOT NULL,
  type TEXT NOT NULL, actor TEXT NOT NULL, subject TEXT NOT NULL, payload TEXT NOT NULL,
  cause TEXT, refs TEXT, schema TEXT NOT NULL DEFAULT 'evt@1');
CREATE TABLE code_registry (prefix TEXT PRIMARY KEY, namespace TEXT, last_date TEXT,
  last_seq INTEGER DEFAULT 0, status TEXT DEFAULT 'active');
CREATE TABLE code_index (
  code TEXT PRIMARY KEY, title TEXT, project TEXT, owner TEXT, mem_type TEXT,
  visibility TEXT, status TEXT DEFAULT 'active', tags TEXT, file_path TEXT,
  occurred_at TEXT, recorded_at TEXT);
CREATE TABLE citizens (
  citizen_id   TEXT PRIMARY KEY,
  display_name TEXT,
  kind         TEXT,
  role         TEXT,
  host         TEXT,
  status       TEXT DEFAULT 'active',
  created_at   TEXT,
  is_owner     INTEGER DEFAULT 0
);
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT, type TEXT, updated_at TEXT);
INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at,is_owner)
  VALUES ('C-OLD','老公民','resident','role-x','workbuddy','active',
          '2026-01-01T00:00:00Z',0);
INSERT INTO config (key,value,type,updated_at)
  VALUES ('timezone','Asia/Shanghai','string','2026-01-01T00:00:00Z');
"""

# session_tokens 的正/反例。正例放在前面是纪律：先证明"这张表能收合法行"，
# 否则后面那些"被拒"可能来自别的原因（少列、类型不对、NOT NULL……），而不是 CHECK。
TOKEN_GOOD = (
    ("tok-cli", "C-T", "sess-1", "machine", "cli", None),
    ("tok-hook", "C-T", "sess-1", "machine", "hook", None),
    ("tok-parent", "C-T", "sess-2", "inherited", "parent", "tok-hook"),
)
TOKEN_BAD = (
    ("tok-bad-1", "C-T", "sess-3", "inherited", "parent", None,
     "channel='parent' 却没给 parent_token"),
    ("tok-bad-2", "C-T", "sess-3", "bogus", "cli", None,
     "evidence='bogus' 不在词表里"),
    ("tok-bad-3", "C-T", "sess-3", "machine", "bogus", None,
     "channel='bogus' 不在词表里"),
    ("tok-bad-4", "C-T", "sess-3", "machine", "parent", "tok-hook",
     "parent 渠道只能继承证据（第 4 条 CHECK）"),
)

# 判据 G 的基线：本轮改动**之前**，三个文件里所有引用 citizens 的 SQL 语句，
# 按「函数 → 语句」冻结（行号见交付报告的对照表；行号会漂，语句不会）。
# 口径：AST 取字符串常量（隐式拼接已被解析器折叠），f-string 用 {} 占位；
# 只要出现 citizens 且以 SQL 关键字开头就算一条。fish.py 25 + mcp_server.py 2 +
# daily_report.py 1 = 28 条。任务书写的"24 处"是按另一种口径数的（不计 migrate 里
# 的 PRAGMA/ALTER/CREATE INDEX、且漏了 daily_report 那处）；口径不同不影响判据——
# 判据只认"一条都不许变"。
FROZEN_CITIZENS_SQL = {
    "fish.py": {
        "migrate": (
            "ALTER TABLE citizens ADD COLUMN is_owner INTEGER DEFAULT 0",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_citizens_owner "
            "ON citizens(is_owner) WHERE is_owner = 1",
            "PRAGMA table_info(citizens)",
            "SELECT 1 FROM citizens WHERE is_owner=1",
            "UPDATE citizens SET is_owner=1 WHERE citizen_id=?",
        ),
        "resolve_owner": (
            "SELECT citizen_id FROM citizens WHERE is_owner=1 AND status='active'",
        ),
        "resolve_owner_arg": (
            "SELECT 1 FROM citizens WHERE citizen_id=?",
            "SELECT citizen_id,display_name,kind FROM citizens WHERE status='active' "
            "ORDER BY created_at",
        ),
        "set_owner": (
            "SELECT kind,status,display_name FROM citizens WHERE citizen_id=?",
            "UPDATE citizens SET is_owner=0 WHERE is_owner=1",
            "UPDATE citizens SET is_owner=1 WHERE citizen_id=?",
        ),
        "find_citizen_by_name": (
            # 2026-10-24 补记：那句 ORDER BY created_at, citizen_id LIMIT 1 是
            # "同名多公民时结果必须确定"那一步的**有记录的修改**（理由见 fish.py 里
            # 该函数的 docstring）。判据 G 的目的是抓"没登记的改动"，不是禁止一切改动——
            # 有据可查的改动就该同步进基线，否则判据会从那天起一直红着（真实发生过）。
            "SELECT citizen_id,display_name,kind,role,host FROM citizens "
            "WHERE display_name=? AND status='active' "
            "ORDER BY created_at, citizen_id LIMIT 1",
        ),
        "cmd_citizen_add": (
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
        ),
        "cmd_whoami": (
            "SELECT citizen_id,display_name,kind,role,host FROM citizens WHERE citizen_id=?",
            "SELECT display_name,kind FROM citizens WHERE citizen_id=?",
        ),
        "build_injection_pack": (
            "SELECT display_name,kind FROM citizens WHERE citizen_id=?",
            "SELECT display_name,kind,role,host FROM citizens WHERE citizen_id=?",
        ),
        "cmd_owner": (
            "SELECT display_name,kind,is_owner FROM citizens WHERE citizen_id=?",
        ),
        "cmd_citizen_retire": (
            "SELECT display_name,kind,status,is_owner FROM citizens WHERE citizen_id=?",
            "SELECT display_name,status FROM citizens WHERE citizen_id=?",
            "UPDATE citizens SET status='retired' WHERE citizen_id=?",
        ),
        "cmd_citizen_list": (
            "SELECT citizen_id,display_name,kind,host,status,is_owner,created_at "
            "FROM citizens ORDER BY status='retired', created_at",
        ),
        "cmd_report": (
            "SELECT display_name,kind,status FROM citizens WHERE status='active' "
            "ORDER BY created_at",
        ),
        "cmd_healthcheck": ("SELECT citizen_id FROM citizens",),
        "rebuild_projection": ("SELECT citizen_id FROM citizens",),
    },
    "mcp_server.py": {
        "tool_whoami": ("SELECT display_name,kind FROM citizens WHERE citizen_id=?",),
        "tool_register": (
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
        ),
    },
    "daily_report.py": {
        "_counts": ("SELECT COUNT(*) FROM citizens",),
    },
}

# 本轮**新增**的函数里允许出现 citizens 引用（P1-a 的补列、视图定义与视图自愈）。
# 除此之外出现新的 citizens 引用函数 → 判据 G 红：那说明有人动了查询面却没声明。
DECLARED_NEW_FUNCS = {
    "fish.py": {"_ensure_identity_tables", "_ensure_citizens_all_view",
                "_citizens_all_view_ddl",
                # P1-b（session 命令族）新增的 4 个会话公民读写点。按本判据的纪律，
                # 它们是**声明**出来的新增引用（不是被改的旧查询）：上面 25 条基线一字未改。
                # 这 4 个函数里的 citizens 语句由 test_identity_p1b 判据 I 逐条冻结，
                # 所以"声明"不等于开空白支票。
                "find_session_citizen", "find_or_create_session_citizen",
                "load_citizen", "touch_citizen",
                # 2026-10-24（规矩/注意事项接入这一轮补登）：`find_duplicate_display_names`
                # 是"同名多公民要能报出来"那一步新增的**读点**，当时漏了声明，导致本判据
                # 从那时起一直红着。它只做 GROUP BY 检测、不改上面 25 条基线里的任何一条，
                # 所以按同一纪律补声明（不是放宽判据）。
                "find_duplicate_display_names",
                # 2026-10-25（`EVAL-261025-16`）：`board_presence_rows` 加了
                # `LEFT JOIN citizens` 拿 display_name（看板原来只显示 ULID，
                # 人每次要拿它去 whoami 对照才知道是谁）。
                # 这是**声明**出来的新引用：上面 25 条基线一字未改。
                "board_presence_rows",
                # 2026-10-25（阶段 B · 名字认领）：`rename_citizen` 里新增 2 条
                # citizens 语句（SELECT 现状 / UPDATE display_name）。同样是**声明**
                # 出来的新引用，上面 25 条基线一字未改。
                # 为什么需要它：用户开场指定名字时，系统里原本**没有任何路径**能把
                # 名字落到当前公民上（全库 6 条 UPDATE citizens 无一条碰 display_name），
                # 名字只能以"孤儿公民"的形式存在。
                "rename_citizen",
                # 2026-10-25（阶段 D · 名字治理）：`resolve_name` 里新增 1 条 citizens
                # 查询（按 display_name 解析规范名 + 状态）。同样是**声明**出来的新引用，
                # 上面 25 条基线一字未改。
                # 为什么必须集中在它里面：别名解析要同时覆盖 project / citizen 两种
                # 命名空间，散落到各调用方就是 E-06（顺手的 SQL 忘条件）的复发。
                "resolve_name",
                # 2026-10-25（外部评估 `EVAL-261025-05` 修复）：`start_session` 里新增
                # 1 条 citizens 查询（按 citizen_id 校验 `--citizen` 指向的公民是否存在/已归档）。
                # 同样是**声明**出来的新引用，上面 25 条基线一字未改。
                # 为什么必须加：`--citizen` 是"同一 AI 在 MCP 与 CLI 两条通道归属分裂"
                # 的唯一显式出口——不校验就会把会话绑到不存在的公民上，或绑到已归档的。
                "start_session",
                # 2026-10-25（`EVAL-261025-21` 修复）：`fix_owner` 里新增 1 条 citizens_all
                # 查询（校验新归属公民存在且活跃）。同样是**声明**出来的新引用，
                # 上面 25 条基线一字未改；它的写入只碰 code_index.owner + 追加事件。
                "fix_owner"},
    "mcp_server.py": set(),
    "daily_report.py": set(),
}

_SQL_START = re.compile(r"^(SELECT|INSERT|UPDATE|DELETE|PRAGMA|ALTER|CREATE|REPLACE)\b", re.I)


def _table_info(conn, name):
    """库自己说的结构：[(列名, 类型, notnull, 默认值, pk)]。不经过被测实现的记忆。"""
    return [(r[1], (r[2] or "").upper(), r[3], r[4], r[5])
            for r in conn.execute("PRAGMA table_info(%s)" % name)]


def _objects(conn, kind):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type=?", (kind,))}


def _normalize(text):
    return " ".join(text.split())


def _node_sql(node):
    """一个节点下所有"以 SQL 关键字开头且提到 citizens"的字符串（f-string 用 {} 占位）。"""
    found = []
    for sub in ast.walk(node):
        text = None
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            text = _normalize(sub.value)
        elif isinstance(sub, ast.JoinedStr):
            text = _normalize("".join(
                str(v.value) if isinstance(v, ast.Constant) else "{}" for v in sub.values))
        if text and "citizens" in text and _SQL_START.match(text):
            found.append(text)
    return found


def citizens_sql_by_function(path):
    """{函数名: (SQL 语句...)} —— 从源码里机械取，不靠正则猜。"""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            hits = _node_sql(node)
            if hits:
                found[node.name] = tuple(sorted(set(hits)))
    return found


class FishIdentityP1aTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fish-id-p1a-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ 夹具

    def _root(self, name):
        root = Path(self.tmp) / name
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _fresh(self, name="fresh", schema=None, run_migrate=True):
        """新库：生产装配路径（connect + SCHEMA + migrate）。"""
        conn = fish.connect(self._root(name))
        conn.executescript(fish.SCHEMA if schema is None else schema)
        if run_migrate:
            fish.migrate(conn)
        return conn

    def _legacy(self, name="legacy"):
        """旧库：直接写盘（不走 fish.connect，模拟"上一个版本留下的库"）。"""
        root = self._root(name)
        raw = sqlite3.connect(str(root / "index.sqlite"))
        try:
            raw.executescript(LEGACY_SCHEMA)
            raw.commit()
        finally:
            raw.close()
        return root

    def _insert_citizen(self, conn, citizen_id, account=None):
        conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at,"
            "is_owner,account_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (citizen_id, "热公民", "visitor", "assistant", "dsh", "active",
             fish.now_utc(), 0, account))
        conn.commit()

    # ------------------------------------------------------------ 共享断言（变异用例要重跑）

    def _assert_structure(self, conn, where):
        """判据 A 的断言：13 列 / 4 表 / 1 视图 / 4 索引，且归档表与热表逐列一致。"""
        self.assertEqual([r[1] for r in conn.execute("PRAGMA table_info(citizens)")],
                         list(CITIZEN_COLUMNS),
                         f"{where}：citizens 应有 13 列且顺序如上（缺列/乱序都红）")
        tables = _objects(conn, "table")
        for name in NEW_TABLES:
            self.assertIn(name, tables, f"{where}：缺表 {name}")
        self.assertIn("citizens_all", _objects(conn, "view"), f"{where}：缺视图 citizens_all")
        indexes = _objects(conn, "index")
        for name in NEW_INDEXES:
            self.assertIn(name, indexes, f"{where}：缺索引 {name}")
        # UNION ALL 只按列位置拼：结构漂一格就静默错位，所以逐列（含类型/默认值）比
        self.assertEqual(_table_info(conn, "citizens_archive"), _table_info(conn, "citizens"),
                         f"{where}：citizens_archive 与 citizens 结构不一致——视图会静默错位")
        self.assertEqual([r[1] for r in conn.execute("PRAGMA table_info(citizens_all)")],
                         list(CITIZEN_COLUMNS), f"{where}：citizens_all 暴露的列不对")
        # 视图定义本身的两条硬要求（不是审美）：显式列名、UNION ALL
        ddl = " ".join(conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='view' AND name='citizens_all'"
        ).fetchone()[0].split()).lower()
        self.assertNotIn("select *", ddl, f"{where}：视图用了 SELECT *，列一漂就静默错位")
        self.assertNotIn("union distinct", ddl, f"{where}：视图不是 UNION ALL")
        self.assertIn("union all", ddl, f"{where}：视图不是 UNION ALL——归档是搬不是复制")
        self.assertIn("from citizens_archive", ddl, f"{where}：视图没查归档表")

    def _assert_open_stint_locked(self, conn, citizen_id):
        """判据 E 的断言：**一人一项目一个**开档期（2026-10-24 语义变更）。

        两件事必须同时成立才算过：
          · 同一 citizen + 同一 project 的第二个开档期 → 被拒（重复开档，库层挡住）；
          · 同一 citizen + **不同** project 的开档期 → 放行（多项目并行是合法状态）。
        两个方向都写，是因为单测一边都证不出"锁在正确的边界上"：只测"拒"的话，
        把索引换成"永远拒绝"也能过；只测"放"的话，把索引删掉也能过。
        """
        conn.execute("INSERT INTO project_stints (citizen_id, project, from_ts) "
                     "VALUES (?,?,?)", (citizen_id, "P-A", fish.now_utc()))
        conn.commit()
        try:
            conn.execute("INSERT INTO project_stints (citizen_id, project, from_ts) "
                         "VALUES (?,?,?)", (citizen_id, "P-A", fish.now_utc()))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
        else:
            self.fail("同一 citizen 在同一项目上插了两条开档期——ux_stint_open 没生效")
        conn.execute("INSERT INTO project_stints (citizen_id, project, from_ts) "
                     "VALUES (?,?,?)", (citizen_id, "P-B", fish.now_utc()))
        conn.commit()
        self.assertEqual(
            [r[0] for r in conn.execute(
                "SELECT project FROM project_stints WHERE citizen_id=? AND to_ts IS NULL "
                "ORDER BY project", (citizen_id,))],
            ["P-A", "P-B"], "不同项目必须能同时开着（多项目并行）")

    def _assert_token_checks(self, conn):
        """判据 F 的断言：3 条正例可入库，4 条反例逐条被拒。"""
        for token_hash, cid, sess, evidence, channel, parent in TOKEN_GOOD:
            conn.execute(
                "INSERT INTO session_tokens (token_hash,citizen_id,session_key,evidence,"
                "channel,parent_token,issued_at) VALUES (?,?,?,?,?,?,?)",
                (token_hash, cid, sess, evidence, channel, parent, fish.now_utc()))
        conn.commit()
        for row in TOKEN_BAD:
            token_hash, cid, sess, evidence, channel, parent, why = row
            with self.assertRaises(sqlite3.IntegrityError, msg=f"CHECK 没生效：{why}"):
                conn.execute(
                    "INSERT INTO session_tokens (token_hash,citizen_id,session_key,evidence,"
                    "channel,parent_token,issued_at) VALUES (?,?,?,?,?,?,?)",
                    (token_hash, cid, sess, evidence, channel, parent, fish.now_utc()))
            conn.rollback()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM session_tokens").fetchone()[0],
                         len(TOKEN_GOOD), "被拒的行不该留下痕迹，合法的行必须都在")

    # ------------------------------------------------------------ 判据 A

    def test_A_fresh_db_structure_complete(self):
        """判据 A：新库建完就有 13 列 + 4 表 + 视图 + 4 索引（记录、数据都不需要）。"""
        conn = self._fresh()
        try:
            self._assert_structure(conn, "新库（SCHEMA + migrate）")
        finally:
            conn.close()

    def test_A_mutation_dropping_one_ddl_goes_red(self):
        """变异自证（code 级）：把 agent_accounts 的 DDL 从**两个消费者**里一起摘掉 → 必红。

        为什么要两边一起摘：这正是本实现的双保险（SCHEMA 与 _IDENTITY_TABLE_DDLS 共用一份
        DDL）。只摘一边，另一边会把表建回来，那样"红"就测不出来了；真删 DDL 的人必然两边都删。
        """
        broken = fish.SCHEMA.replace(fish._AGENT_ACCOUNTS_DDL, "")
        self.assertNotEqual(broken, fish.SCHEMA, "变异没生效：SCHEMA 里没匹配到 agent_accounts")
        kept = tuple(d for d in fish._IDENTITY_TABLE_DDLS if "agent_accounts" not in d)
        self.assertEqual(len(kept), 3, "夹具前提：摘掉一张表后应剩 3 条 DDL")
        with mock.patch.object(fish, "_IDENTITY_TABLE_DDLS", kept):
            conn = self._fresh("mut-a-ddl", schema=broken)
        try:
            self.assertNotIn("agent_accounts", _objects(conn, "table"), "变异没生效：表还是建了")
            with self.assertRaises(AssertionError):
                self._assert_structure(conn, "变异：摘掉 agent_accounts 的 DDL")
        finally:
            conn.close()

    def test_A_mutation_schema_only_goes_red(self):
        """变异自证（fixture 级）：只跑 SCHEMA、不跑 migrate → 视图与 4 条索引都没有 → 必红。

        这条守的是「索引与视图只放 migrate」这条决策：它们依赖旧库补完列之后的最终列集，
        放 SCHEMA 就会在旧库上撞 no such column。所以"只跑 SCHEMA"必然缺它们。
        """
        conn = self._fresh("mut-a-schema-only", run_migrate=False)
        try:
            self.assertNotIn("citizens_all", _objects(conn, "view"),
                             "夹具前提：不跑 migrate 时视图不该存在")
            with self.assertRaises(AssertionError):
                self._assert_structure(conn, "变异：只跑 SCHEMA 不跑 migrate")
        finally:
            conn.close()

    # ------------------------------------------------------------ 判据 B

    def test_B_legacy_db_gains_columns(self):
        """判据 B：8 列旧 citizens（含 1 行数据）→ 升级后 13 列，**原有数据不丢**。

        为什么单靠 DDL 不够：旧库里 citizens 已存在，`CREATE TABLE IF NOT EXISTS` 对它
        什么都不做，唯一能把列补上的是 migrate 里的幂等 ALTER。
        """
        root = self._legacy()
        conn = fish.connect(root)
        try:
            before = _table_info(conn, "citizens")
            self.assertEqual(len(before), 8, "夹具前提：旧库 citizens 应恰好 8 列")
            old_row = conn.execute(
                "SELECT citizen_id,display_name,kind,role,host,status,created_at,is_owner "
                "FROM citizens").fetchall()

            conn.executescript(fish.SCHEMA)      # IF NOT EXISTS：对旧表无操作
            fish.migrate(conn)                   # 必须在这里补列

            self._assert_structure(conn, "旧库升级后")
            # 原有数据原样都在；新列落在 NULL 上（不是被填成 ''/0 的假值）
            self.assertEqual(
                conn.execute(
                    "SELECT citizen_id,display_name,kind,role,host,status,created_at,is_owner,"
                    "account_id,device_id,host_session_key,last_seen_at,archived_at "
                    "FROM citizens").fetchall(),
                [tuple(r) + (None, None, None, None, None) for r in old_row],
                "升级动了旧数据，或新列的默认值不是 NULL")
            # 视图能查到老行（结构对了但数据进不来，等于没升级）
            self.assertEqual(
                conn.execute("SELECT citizen_id FROM citizens_all").fetchall(), [("C-OLD",)],
                "升级后 citizens_all 查不到旧行")

            # 幂等：真实升级会反复跑，第二遍不许报错也不许多出列
            conn.executescript(fish.SCHEMA)
            fish.migrate(conn)
            self.assertEqual([r[1] for r in conn.execute("PRAGMA table_info(citizens)")],
                             list(CITIZEN_COLUMNS), "重复 migrate 改变了 citizens 的列")
        finally:
            conn.close()

    def test_B2_legacy_db_migrate_only(self):
        """纪律：**只跑 migrate（不跑 SCHEMA）** 也必须把身份层结构补齐。

        push 地基那轮定下的同一条纪律（test_push_p0 判据 L 形态 3）：升级路径不能隐含
        依赖"调用方先跑过 SCHEMA"——真实世界里打开旧库的第一步就是 migrate。
        """
        root = self._legacy("legacy-migrate-only")
        conn = fish.connect(root)
        try:
            fish.migrate(conn)               # 刻意不跑 SCHEMA
            self._assert_structure(conn, "旧库只跑 migrate")
            self.assertEqual(
                conn.execute("SELECT citizen_id FROM citizens_all").fetchall(), [("C-OLD",)])
        finally:
            conn.close()

    def test_B_mutation_dropping_alter_goes_red(self):
        """变异自证（code 级）：把 5 列的补列清单摘掉 → 判据 B 必须红。两个变体各证一件事：

          变体 1（只摘 ALTER）→ migrate 自己就崩（视图/索引先于列）：
                   这正是 07 的 is_owner 事故形状——依赖新列的 DDL 抢在补列前面。
          变体 2（ALTER + 视图 + 索引一起摘）→ migrate 能跑完，但 8 列还在，
                   **共享断言**必须报错——这一条才证明断言本身有判别力。
        """
        # 变体 1
        root = self._legacy("mut-b-1")
        conn = fish.connect(root)
        try:
            conn.executescript(fish.SCHEMA)
            with mock.patch.object(fish, "_CITIZENS_NEW_COLS", ()), \
                    self.assertRaises(sqlite3.OperationalError):
                fish.migrate(conn)
        finally:
            conn.close()

        # 变体 2
        root = self._legacy("mut-b-2")
        conn = fish.connect(root)
        try:
            conn.executescript(fish.SCHEMA)
            with mock.patch.object(fish, "_CITIZENS_NEW_COLS", ()), \
                    mock.patch.object(fish, "_IDENTITY_INDEXES", ()), \
                    mock.patch.object(fish, "_ensure_citizens_all_view", lambda c: None):
                fish.migrate(conn)           # 跑得完，但列补不出来
            with self.assertRaises(AssertionError):
                self._assert_structure(conn, "变异：摘掉补列清单")
        finally:
            conn.close()

    # ------------------------------------------------------------ 判据 C

    def _assert_view_has_hot_row(self, conn, citizen_id="C-HOT"):
        rows = conn.execute(
            "SELECT citizen_id,display_name,account_id,device_id,archived_at "
            "FROM citizens_all WHERE citizen_id=?", (citizen_id,)).fetchall()
        self.assertEqual(rows, [(citizen_id, "热公民", "ACC-1", None, None)],
                         "citizens_all 查不到热表里的行（视图写错成别的表了？）")

    def test_C_view_sees_hot_row(self):
        """判据 C：插一个公民 → citizens_all 能查到，且新列的值原样透出。"""
        conn = self._fresh("c")
        try:
            self._insert_citizen(conn, "C-HOT", account="ACC-1")
            self._assert_view_has_hot_row(conn)
        finally:
            conn.close()

    def test_C_mutation_view_reads_archive_only_goes_red(self):
        """变异自证（fixture 级）：把视图改成**只查归档表**（漏掉热表那半条 SELECT）→ 必红。"""
        conn = self._fresh("mut-c")
        try:
            self._insert_citizen(conn, "C-HOT", account="ACC-1")
            cols = ", ".join(CITIZEN_COLUMNS)
            conn.execute("DROP VIEW citizens_all")
            conn.execute(f"CREATE VIEW citizens_all AS SELECT {cols} FROM citizens_archive")
            conn.commit()
            with self.assertRaises(AssertionError):
                self._assert_view_has_hot_row(conn)
        finally:
            conn.close()

    # ------------------------------------------------------------ 判据 D

    def _archive_row(self, conn, citizen_id):
        """把一行从热表**搬**到归档表。本轮不实现归档逻辑，所以由测试自己搬。"""
        cols = ", ".join(CITIZEN_COLUMNS)
        conn.execute(f"INSERT INTO citizens_archive ({cols}) "
                     f"SELECT {cols} FROM citizens WHERE citizen_id=?", (citizen_id,))
        conn.execute("UPDATE citizens_archive SET archived_at=? WHERE citizen_id=?",
                     (fish.now_utc(), citizen_id))
        conn.execute("DELETE FROM citizens WHERE citizen_id=?", (citizen_id,))
        conn.commit()

    def _assert_archived_once(self, conn, citizen_id):
        rows = conn.execute(
            "SELECT citizen_id,display_name,archived_at FROM citizens_all WHERE citizen_id=?",
            (citizen_id,)).fetchall()
        self.assertEqual(len(rows), 1,
                         f"归档后 citizens_all 应恰好 1 条，实际 {len(rows)} 条"
                         f"（UNION ALL 会把两张表里都有的行都列出来 → 搬迁假成功）")
        self.assertEqual(rows[0][0], citizen_id)
        self.assertIsNotNone(rows[0][2], "归档行的 archived_at 应该有值")
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM citizens WHERE citizen_id=?",
                         (citizen_id,)).fetchone()[0], 0,
            "搬完之后热表里不该还有它")

    def test_D_archived_row_visible_once(self):
        """判据 D：手工把一行移到 archive → 视图仍能查到它，且**只有一条**。"""
        conn = self._fresh("d")
        try:
            self._insert_citizen(conn, "C-ARC", account="ACC-2")
            self.assertIsNotNone(
                conn.execute("SELECT citizen_id FROM citizens_all WHERE citizen_id='C-ARC'")
                .fetchone(), "前提：归档前视图应该看得到它")
            self._archive_row(conn, "C-ARC")
            self._assert_archived_once(conn, "C-ARC")
        finally:
            conn.close()

    def test_D_mutation_duplicate_row_goes_red(self):
        """变异自证（fixture 级）：搬迁只"复制"没"删除" → 视图看到两条 → 必红。

        这就是 UNION ALL 存在的理由：若它是 UNION，重复行会被去重吞掉，这条断言
        就永远绿——坏数据被视图洗白，搬迁 bug 再也查不出来。
        """
        conn = self._fresh("mut-d")
        try:
            self._insert_citizen(conn, "C-ARC", account="ACC-2")
            cols = ", ".join(CITIZEN_COLUMNS)
            conn.execute(f"INSERT INTO citizens_archive ({cols}) "
                         f"SELECT {cols} FROM citizens WHERE citizen_id=?", ("C-ARC",))
            conn.commit()                    # 只复制，不删除：热表里还留着那一行
            with self.assertRaises(AssertionError):
                self._assert_archived_once(conn, "C-ARC")
        finally:
            conn.close()

    # ------------------------------------------------------------ 判据 E

    def test_E_open_stint_unique(self):
        """判据 E：一人一项目一个开档期；收档后可再开；不同 citizen 互不影响。

        2026-10-24：不变量从「一人一个」改成「一人一项目一个」——同项目重复开仍被拒，
        不同项目并行放行（`_assert_open_stint_locked` 两个方向都断言）。
        """
        conn = self._fresh("e")
        try:
            self._assert_open_stint_locked(conn, "C-STINT")
            conn.execute("UPDATE project_stints SET to_ts=? "
                         "WHERE citizen_id=? AND to_ts IS NULL",
                         (fish.now_utc(), "C-STINT"))
            conn.execute("INSERT INTO project_stints (citizen_id, project, from_ts) "
                         "VALUES (?,?,?)", ("C-STINT", "P-A", fish.now_utc()))
            conn.execute("INSERT INTO project_stints (citizen_id, project, from_ts) "
                         "VALUES (?,?,?)", ("C-OTHER", "P-1", fish.now_utc()))
            conn.commit()
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM project_stints").fetchone()[0], 4,
                "收档后应能再开同一项目；不同公民各自也能开（部分索引只管开档期）")
        finally:
            conn.close()

    def test_E_mutation_without_index_goes_red(self):
        """变异自证（code 级）：不建 ux_stint_open → 同项目第二个开档期插得进去 → 必红。

        2026-10-24：开档期索引的生命周期从「清单里一条幂等 CREATE」改成
        `_ensure_stint_open_index`（名字没变、定义变了，必须先 DROP 再重建）。
        变异靶点因此换到那个函数上——**被测的东西没变**：摘掉索引后唯一性必须消失。
        """
        with mock.patch.object(fish, "_ensure_stint_open_index", lambda conn: None):
            conn = self._fresh("mut-e")
        try:
            self.assertNotIn("ux_stint_open", _objects(conn, "index"),
                             "变异没生效：索引还是建了")
            with self.assertRaises(AssertionError):
                self._assert_open_stint_locked(conn, "C-STINT")
        finally:
            conn.close()

    # ------------------------------------------------------------ 判据 F

    def test_F_session_token_checks(self):
        """判据 F：session_tokens 的 4 条 CHECK 逐条生效（含本轮补的第 4 条）。"""
        conn = self._fresh("f")
        try:
            self._assert_token_checks(conn)
        finally:
            conn.close()

    def test_F_mutation_table_without_checks_goes_red(self):
        """变异自证（fixture 级）：把表换成**没有 CHECK** 的同名列版 → 反例全被收下 → 必红。"""
        conn = self._fresh("mut-f")
        try:
            conn.execute("DROP TABLE session_tokens")
            conn.execute(
                "CREATE TABLE session_tokens (token_hash TEXT PRIMARY KEY, "
                "citizen_id TEXT NOT NULL, session_key TEXT NOT NULL, evidence TEXT NOT NULL, "
                "channel TEXT NOT NULL, parent_token TEXT, issued_at TEXT NOT NULL, "
                "expires_at TEXT, revoked_at TEXT)")
            conn.commit()
            with self.assertRaises(AssertionError):
                self._assert_token_checks(conn)
        finally:
            conn.close()

    # ------------------------------------------------------------ 判据 G

    def test_G_existing_queries_untouched(self):
        """判据 G：本轮改动前那 28 条引用 citizens 的 SQL，逐条仍在原函数里、一字未改。"""
        counts = {name: sum(len(v) for v in frozen.values())
                  for name, frozen in FROZEN_CITIZENS_SQL.items()}
        self.assertEqual(counts, {"fish.py": 25, "mcp_server.py": 2, "daily_report.py": 1},
                         f"基线自检：口径应为 fish 25 / mcp 2 / daily_report 1，实际 {counts}")
        for fname, frozen in FROZEN_CITIZENS_SQL.items():
            got = citizens_sql_by_function(HERE / fname)
            for func, stmts in frozen.items():
                with self.subTest(file=fname, func=func):
                    self.assertIn(func, got, f"{fname}::{func} 不见了（函数被改名/删除？）")
                    self.assertEqual(
                        got[func], tuple(sorted(set(stmts))),
                        f"{fname}::{func} 里的 citizens 语句被改过——本轮一个字都不许改")
            extra = set(got) - set(frozen) - DECLARED_NEW_FUNCS[fname]
            self.assertEqual(extra, set(),
                             f"{fname} 出现了未声明的 citizens 引用函数：{sorted(extra)}")

    def test_G_mutation_editing_one_query_goes_red(self):
        """变异自证（fixture 级）：把其中一条语句改掉一个字 → 判据 G 必须红。

        改的是 resolve_owner 那句（owner 有效性判定，24 处里最关键的一处）。
        在临时副本上改，真源码一个字节都不动。
        """
        source = (HERE / "fish.py").read_text(encoding="utf-8")
        marker = "SELECT citizen_id FROM citizens WHERE is_owner=1 AND status='active'"
        self.assertIn(marker, source, "变异夹具失效：源码里找不到这句")
        broken = source.replace(marker, "SELECT citizen_id FROM citizens WHERE is_owner=1")
        self.assertNotEqual(broken, source)
        copy = Path(self.tmp) / "fish_broken.py"
        copy.write_text(broken, encoding="utf-8")
        got = citizens_sql_by_function(copy)
        with self.assertRaises(AssertionError):
            self.assertEqual(got["resolve_owner"],
                             FROZEN_CITIZENS_SQL["fish.py"]["resolve_owner"],
                             "变异没被抓住：改了查询却没红灯")


if __name__ == "__main__":
    unittest.main(verbosity=2)
