#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fish 推送模块 · P0 地基验收（13-推送系统设计 §2 / §10-P0）
============================================================
三件事，五条判据：

  A  memory_access_log 五路都写（recall / resolve / read_body / inject / push）
  B  supersedes 双写（links 里出现对应边，且与 superseded_by 同向同值）
  C  正文编码提及建边（含中文前缀，07 D-14）
  D  FTS 语义相似建边
  E  旧库升级不崩（两张表 / 索引都能补出来，含「同名表缺列」形态）

追加判据（links 加「再巩固」三列，14 §6 修正 1）：
  F  旧库升级能补这 3 列（ALTER 在 migrate 的补列清单里）
  G  新库**建表那一刻**就带这 3 列（不靠 migrate 兜底），且两个计数列默认 0
  H  ix_links_suppressed 存在，且是 `removed_at IS NULL` 上的部分索引

追加判据（push_log 建表 + 最小写入链路，16 §5.6 / 论文《When Should the Agent
Speak?》§6.3 —— 记忆系统缺的那个查询：用户对我过去干预的响应历史）：
  I  inject 写 push_log：注入包里展示的每条编码一行，全部待判（NULL）
  J  read_body / resolve 回填 accepted：同一会话、24h 窗口内、尚未判过
  K  超时未反应 → daily_report 判 ignored；窗口内的推送不许被误判
  L  旧库升级能建 push_log（表 + 三条索引，含「同名表缺列」形态）

纪律（03 §G、09 RX-2、07 D-10 的教训）：**每条判据都必须先证明它在坏输入下会红**。
所以每条判据都配一条 `*_mutation_*` 用例：运行时把对应写入点 / 通道摘掉，再把
**同一组断言**重跑一遍——它必须报错。若它照样绿，说明断言没有判别力（恒绿判据）。

变异的三种形态（10 §6.1 的纪律）在本文件里都是 `code` / `fixture` 级：
  · code    —— mock.patch 摘掉一个写入点或一条通道（可重跑，不靠改源码）
  · fixture —— 造一个缺列的旧库 / 缺约束的旧表
"""
import contextlib
import datetime
import io
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fish  # noqa: E402
import mcp_server  # noqa: E402
import daily_report  # noqa: E402

ACTOR_NAME = "wb-push"
ACTOR_ID = "CIT-PUSH-0001"
# 会话名（自报级）与它的**会话标识**（session_key）不是一回事：P1-c 起归属记的是
# token 行里的 session_key，无宿主 session id 时它是 `self:<自报名>`。
SESSION_ALIAS = "sess-push"
SESSION = f"self:{SESSION_ALIAS}"

# 旧库形态：没有两张推送地基表，也没有 is_owner / session_actor / memory_fts
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
CREATE TABLE citizens (citizen_id TEXT PRIMARY KEY, display_name TEXT, kind TEXT,
  role TEXT, host TEXT, status TEXT, created_at TEXT);
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT, type TEXT, updated_at TEXT);
INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status)
  VALUES ('TEST','t','',0,'active');
INSERT INTO code_index (code,title,project,mem_type,status)
  VALUES ('TEST-261022-01','旧条目','TEST','fact','active');
"""

# 两条「几乎同一段话」的正文：用来验语义边（Jaccard 必然高）
SIM_BODY = ("部署前必须核对 PIT 检查清单：先冻结写入，再导出快照，"
            "最后验证断链数为零并回填对账报告。")

# 「再巩固」预留三列（14 §6 修正 1）：(列名, 声明类型, DDL 默认值文本)。
# 默认值写成字符串，是因为它取自 PRAGMA table_info 的 dflt_value——SQLite 原样返回文本。
REINFORCE_COLS = (
    ("last_reinforced_at", "TEXT", None),
    ("reinforce_count", "INTEGER", "0"),
    ("suppressed", "INTEGER", "0"),
)

# 旧库形态的 links：**恰好是 P0 交付前的 9 列**，三列再巩固字段一个都没有。
# 用它当夹具才说得清结果是"migrate 补出来的"，而不是"夹具本来就有"。
LEGACY_LINKS_DDL = """
CREATE TABLE links (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  src        TEXT NOT NULL,
  dst        TEXT NOT NULL,
  rel        TEXT NOT NULL,
  source     TEXT NOT NULL,
  weight     REAL DEFAULT 1.0,
  evidence   TEXT,
  created_at TEXT NOT NULL,
  removed_at TEXT,
  UNIQUE(src, dst, rel)
);
INSERT INTO links (src,dst,rel,source,weight,evidence,created_at)
  VALUES ('TEST-261022-01','TEST-261022-02','related','explicit',0.8,'旧库里的边',
          '2026-10-22T00:00:00Z');
"""


def _link_columns(conn):
    """{列名: (类型, 默认值文本)} —— 读库的元数据，不经过被测实现。"""
    return {r[1]: ((r[2] or "").upper(), r[4])
            for r in conn.execute("PRAGMA table_info(links)")}


def _patched_access_log(drop_via):
    """变异：把某个 via 的写入「摘掉」，其余照常（= 删掉一个写入点）。"""
    real = fish.append_access_log

    def patched(conn, codes, **kw):
        if kw.get("via") == drop_via:
            return 0
        return real(conn, codes, **kw)

    return mock.patch.object(fish, "append_access_log", patched)


def _age_push_rows(conn, code, hours, now=None):
    """把某编码的**待判**推送行改成 N 小时前推的（手工造"隔了一天没人理"）。

    为什么不靠 sleep：判据 K 要证明的是"窗口过期"这件事，不是"时间会流逝"。
    直接改 pushed_at 才能把 25 小时压缩进毫秒级测试。
    """
    now_dt = now or datetime.datetime.now(datetime.timezone.utc)
    old = (now_dt - datetime.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = conn.execute(
        "UPDATE push_log SET pushed_at=? WHERE code=? AND reaction IS NULL", (old, code))
    conn.commit()
    return cur.rowcount


class _TokenizedServer:
    """给每次工具调用自动补上 token 的薄包装（**夹具专用，不碰被测实现**）。

    为什么不在每个用例里手传 token：本文件有十几处 `srv.call_tool(...)`，手抄一遍
    必然漏一处，而漏掉的那处会静默退化成"验证未归属分支"——最难发现的一种假绿。
    包装只做一件事：`args.setdefault("token", self.token)`。
    """

    def __init__(self, srv, token):
        self.srv = srv
        self.token = token

    @property
    def conn(self):
        return self.srv.conn

    def call_tool(self, name, args=None):
        merged = dict(args or {})
        merged.setdefault("token", self.token)
        return self.srv.call_tool(name, merged)

    def handle(self, req):
        return self.srv.handle(req)


class FishPushP0Test(unittest.TestCase):

    PUSH_KEYS = {
        "push.enabled": (True, "bool"),
        "push.serendipity_ratio": (0.10, "float"),
        "push.open_budget_tokens": (450, "int"),
        "push.after_tool_budget_tokens": (100, "int"),
        "push.situational_budget_tokens": (80, "int"),
        "push.after_tool_every_n_calls": (10, "int"),
        "push.gap_window_days": (14, "int"),
        "push.gap_others_read_min": (3, "int"),
        "push.situational_window_min": (10, "int"),
        "push.gap_early_phase_ratio": (0.30, "float"),
    }

    # push_log 的列契约：**精确集合**断言（多一列少一列都红），能拦住"悄悄改表结构"。
    PUSH_LOG_COLUMNS = {
        "id", "session_key", "citizen_id", "project", "code", "candidate_id",
        "channel", "budget_tokens", "pushed_at", "reaction", "reacted_at", "latency_sec"}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fish-push-p0-")
        self.root = os.path.join(self.tmp, "data")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ 夹具

    def _root(self, name=None):
        root = Path(self.root if name is None else os.path.join(self.tmp, name))
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _seed(self, root):
        """建库 + 两个前缀（含一个中文前缀）+ 一个登记公民。

        公民必须带 `host_session_key`：P1-c 起身份由 `session start` 按会话标识复用
        （自报名 → `self:<名字>`）。预设它，夹具签出来的 token 才会落在 ACTOR_ID 上，
        用例断言里的"当前行动者"才还是同一个公民——断言一个字都不用改。
        """
        conn = fish.connect(root)
        try:
            conn.executescript(fish.SCHEMA)
            fish.migrate(conn)
            for prefix in ("TEST", "企业智能"):
                conn.execute(
                    "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
                    "VALUES (?,?,?,?,?)", (prefix, prefix, "", 0, "active"))
            conn.execute(
                "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,"
                "created_at,host_session_key,last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (ACTOR_ID, ACTOR_NAME, "visitor", "assistant", "workbuddy", "active",
                 fish.now_utc(), SESSION, fish.now_utc()))
            conn.commit()
        finally:
            conn.close()

    def _server(self, root, actor=ACTOR_NAME, session=SESSION_ALIAS):
        """起一个真的 FishServer（生产装配路径：connect + SCHEMA + migrate），并给它签发凭证。

        P1-c 的契约：MCP 不再从进程内存 / 环境变量取身份（一个进程被所有会话共享），
        归属只来自**本次调用带的 token**。夹具因此改成"先用 CLI 的签发路径拿一条真
        token，再让它随每次工具调用传进去"——断言一个字都没改：citizen_id 仍是
        ACTOR_ID、session_key 仍是 SESSION。凭证丢了/没传，这些断言会立刻红（那正是判据）。
        """
        with mock.patch.dict(os.environ, {"FISH_ACTOR": actor,
                                          "FISH_SESSION_ACTOR": session}):
            srv = mcp_server.FishServer(root)
            issued = fish.start_session(
                srv.conn, environ={"FISH_ACTOR": actor, "FISH_SESSION_ACTOR": session})
        return _TokenizedServer(srv, issued["token"])

    def _remember(self, srv, *, project="TEST", title="记忆", body="", **kw):
        args = {"project": project, "title": title, "body": body}
        args.update(kw)
        return srv.call_tool("remember", args).splitlines()[0]

    def _make_legacy_db(self, root, script):
        root.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(root / "index.sqlite"))
        try:
            c.executescript(script)
            c.commit()
        finally:
            c.close()

    def _drive_read_tools(self, srv):
        """依次触发五个写入点（remember 只为造一条可读的记忆）。"""
        code = self._remember(srv, title="推送 P0 夹具",
                              body="夹具正文：部署前核对 PIT 检查清单。")
        srv.call_tool("recall", {"query": code})
        srv.call_tool("resolve_code", {"code": code})
        srv.call_tool("read_body", {"code": code})
        srv.call_tool("inject", {})
        return code

    # ------------------------------------------------------------ 判据 A

    def _assert_five_vias(self, conn):
        """判据 A 的断言（变异用例会把它重跑一遍，必须能红）。"""
        rows = conn.execute(
            "SELECT code,citizen_id,session_key,project,via,accessed_at "
            "FROM memory_access_log ORDER BY id").fetchall()
        self.assertTrue(rows, "memory_access_log 一行都没写：五个写入点全丢")
        vias = [r[4] for r in rows]
        for want in ("recall", "resolve", "read_body", "inject", "push"):
            self.assertIn(want, vias, f"缺写入点 via={want}（现有 {sorted(set(vias))}）")
        # 归属三件套：不认识"谁读的"的记录不能进画像（13 §3.2 的画像字段都靠它）
        for code, cit, sess, proj, via, ts in rows:
            self.assertEqual(cit, ACTOR_ID,
                             f"citizen_id 应为当前行动者 {ACTOR_ID}，实际 {cit!r}（via={via}）")
            self.assertEqual(sess, SESSION,
                             f"session_key 应为当前会话，实际 {sess!r}（via={via}）")
            self.assertEqual(proj, "TEST",
                             f"project 应从编码反查，实际 {proj!r}（via={via}）")
            self.assertIsNotNone(fish.parse_utc(ts),
                                 f"accessed_at 必须是 ISO8601 UTC，实际 {ts!r}（via={via}）")

    def test_A_five_write_points(self):
        """判据 A：五个写入点各写一行，via 五值齐全，字段来源正确。"""
        root = self._root()
        self._seed(root)
        srv = self._server(root)
        try:
            self._drive_read_tools(srv)
        finally:
            srv.conn.close()
        conn = fish.connect(root)          # 独立连接读 = 真值不在被测实现手里
        try:
            self._assert_five_vias(conn)
            # 读入口也要可用：P1 的画像 / 遗漏检测都从它取数，不能让它成为死代码
            only_recall = fish.access_log_rows(conn, via="recall")
            self.assertEqual({r[4] for r in only_recall}, {"recall"},
                             "access_log_rows 的 via 过滤没生效")
            self.assertTrue(only_recall, "access_log_rows 按 via 过滤后不该是空的")
        finally:
            conn.close()

    def test_A_mutation_dropping_any_write_point_goes_red(self):
        """变异自证：五个写入点**逐个**摘掉，判据 A 都必须红。

        这是"删掉任一写入点 → 该 via 缺失 → 红"的可重跑版本。
        """
        for drop in ("recall", "resolve", "read_body", "inject", "push"):
            with self.subTest(dropped=drop):
                root = self._root(f"mut-a-{drop}")
                self._seed(root)
                srv = self._server(root)
                try:
                    with _patched_access_log(drop):
                        self._drive_read_tools(srv)
                finally:
                    srv.conn.close()
                conn = fish.connect(root)
                try:
                    with self.assertRaises(AssertionError):
                        self._assert_five_vias(conn)
                finally:
                    conn.close()

    # ------------------------------------------------------------ 判据 B

    def _assert_supersedes_edge(self, conn, old, new):
        rows = conn.execute(
            "SELECT src,dst,rel,source,weight,evidence,removed_at FROM links "
            "WHERE rel='supersedes'").fetchall()
        self.assertTrue(rows, "links 里没有任何 supersedes 边（只写了 superseded_by？）")
        hit = [r for r in rows if (r[0], r[1]) == (old, new)]
        self.assertEqual(len(hit), 1, f"应有 1 条 {old}→{new} 的 supersedes 边，实际 {rows}")
        src, dst, rel, source, weight, evidence, removed = hit[0]
        self.assertEqual(source, "explicit", "supersede 是显式动作，source 必须是 explicit")
        self.assertIsNone(removed, "新写的边不能被标成已移除")
        self.assertTrue(evidence, "边必须带 evidence，否则不可解释（13 §2.2）")
        # 双写的另一半必须在：links 与 superseded_by 是同一事实的两种存储
        row = conn.execute("SELECT status,superseded_by FROM code_index WHERE code=?",
                           (old,)).fetchone()
        self.assertEqual(row[0], "superseded")
        self.assertEqual(row[1], new, "superseded_by 与 links 必须同向同值")

    def test_B_supersedes_dual_write(self):
        """判据 B：supersede 后 links 有 rel='supersedes' 的边（两条写入路径都验）。"""
        root = self._root()
        self._seed(root)
        root_p = Path(root)
        conn = fish.connect(root)
        try:
            old, _ = fish.record_memory(conn, root_p, project="TEST", title="旧结论",
                                        body="旧正文：结论是 A。", owner=ACTOR_ID)
            new, _ = fish.record_memory(conn, root_p, project="TEST", title="新结论",
                                        body="新正文：结论是 B。", owner=ACTOR_ID)
            # 路径 1：显式的替代动作
            fish.supersede_memory(conn, old, new, reason="新证据推翻旧结论", actor=ACTOR_ID)
            self._assert_supersedes_edge(conn, old, new)
            # 单向存、双向查（13 §2.2）：从两端都能查到同一条边
            from_old = fish.links_for(conn, old, rel="supersedes")
            from_new = fish.links_for(conn, new, rel="supersedes")
            self.assertEqual([(r["code"], r["direction"]) for r in from_old],
                             [(new, "out")], "从旧条目出发应看到 out 方向的替代边")
            self.assertEqual([(r["code"], r["direction"]) for r in from_new],
                             [(old, "in")], "从新条目出发应看到 in 方向的同一条边")
            # 路径 2：remember 的 supersedes 参数（写入时顺带替代）
            third, _ = fish.record_memory(conn, root_p, project="TEST", title="第三条",
                                          body="第三条正文。", owner=ACTOR_ID)
            fourth, _ = fish.record_memory(conn, root_p, project="TEST", title="第四条",
                                           body="第四条正文。", owner=ACTOR_ID,
                                           supersedes=[third])
            self._assert_supersedes_edge(conn, third, fourth)
        finally:
            conn.close()

    def test_B_mutation_links_off_goes_red(self):
        """变异自证：只写 superseded_by、不写 links（旧行为）→ 判据 B 必须红。"""
        root = self._root("mut-b")
        self._seed(root)
        root_p = Path(root)
        conn = fish.connect(root)
        try:
            old, _ = fish.record_memory(conn, root_p, project="TEST", title="旧")
            new, _ = fish.record_memory(conn, root_p, project="TEST", title="新")
            with mock.patch.object(fish, "add_link", lambda *a, **k: False):
                fish.supersede_memory(conn, old, new, reason="变异：不写边")
            # 前提：状态那一半照写了（证明红来自 links 缺失，不是别处崩了）
            self.assertEqual(
                conn.execute("SELECT superseded_by FROM code_index WHERE code=?",
                             (old,)).fetchone()[0], new)
            with self.assertRaises(AssertionError):
                self._assert_supersedes_edge(conn, old, new)
        finally:
            conn.close()

    # ------------------------------------------------------------ 判据 C

    def _assert_mention_edge(self, conn, src, dst):
        rows = conn.execute(
            "SELECT rel,source,weight,evidence FROM links "
            "WHERE rel='related' AND src=? AND dst=?", (src, dst)).fetchall()
        self.assertEqual(len(rows), 1, f"缺 {src}→{dst} 的 related 边（正文提及）")
        rel, source, weight, evidence = rows[0]
        self.assertEqual(source, "explicit", "正文提及是显式线索，source 必须是 explicit")
        self.assertEqual(evidence, "正文提及", "evidence 必须写明为什么建这条边")
        self.assertAlmostEqual(weight, 0.8, places=3, msg="13 §2.3 规定提及边 weight=0.8")

    def test_C_body_mention_creates_edge(self):
        """判据 C：正文提到已存在的编码 → 建 related 边（含中文前缀形态）。"""
        root = self._root()
        self._seed(root)
        root_p = Path(root)
        conn = fish.connect(root)
        try:
            old, _ = fish.record_memory(conn, root_p, project="TEST", title="旧结论",
                                        body="正文：部署先冻结写入。", owner=ACTOR_ID)
            new, _ = fish.record_memory(
                conn, root_p, project="TEST", title="接着旧结论",
                body=f"这条接着 {old} 往下推：先导出快照。", owner=ACTOR_ID)
            self._assert_mention_edge(conn, new, old)

            # 中文前缀（07 D-14）：中文没有空格，正则会把前一个字并进前缀，
            # 必须靠库里真实编码集合裁回来——否则这批记忆永远建不出"正文提及"边
            cn, _ = fish.record_memory(conn, root_p, project="企业智能", title="中文前缀条目",
                                       body="正文：会议纪要归档口径。", owner=ACTOR_ID)
            new2, _ = fish.record_memory(
                conn, root_p, project="TEST", title="引用中文前缀条目",
                body=f"参见{cn}的结论，归档口径一致。", owner=ACTOR_ID)
            self._assert_mention_edge(conn, new2, cn)
        finally:
            conn.close()

    def test_C_mutation_regex_off_goes_red(self):
        """变异自证：关掉正文正则扫描 → 判据 C 必须红。

        同时关掉语义通道，把这条判据锁在"提及"这一条路上（否则语义边会替它兜底，
        变异就测不出正则有没有工作）。
        """
        root = self._root("mut-c")
        self._seed(root)
        root_p = Path(root)
        conn = fish.connect(root)
        try:
            old, _ = fish.record_memory(conn, root_p, project="TEST", title="旧结论",
                                        body="正文：部署先冻结写入。", owner=ACTOR_ID)
            with mock.patch.object(fish, "CODE_MENTION_RE", re.compile(r"^\x00$")), \
                    mock.patch.object(fish, "similar_codes", lambda *a, **k: []):
                new, _ = fish.record_memory(
                    conn, root_p, project="TEST", title="接着旧结论",
                    body=f"这条接着 {old} 往下推：先导出快照。", owner=ACTOR_ID)
            with self.assertRaises(AssertionError):
                self._assert_mention_edge(conn, new, old)
        finally:
            conn.close()

    # ------------------------------------------------------------ 判据 D

    def _assert_semantic_edge(self, conn, src, dst):
        rows = conn.execute(
            "SELECT weight,evidence,removed_at FROM links WHERE rel='related' "
            "AND source='semantic' AND src=? AND dst=?", (src, dst)).fetchall()
        self.assertEqual(len(rows), 1, f"缺 {src}→{dst} 的语义 related 边（FTS 相似 top-3）")
        weight, evidence, removed = rows[0]
        self.assertEqual(evidence, "正文相似")
        self.assertIsNone(removed, "新写的边不能被标成已移除")
        self.assertGreater(weight, 0.5,
                           f"两条正文几乎相同，Jaccard 必然 >0.5，实际 {weight}")
        self.assertLessEqual(weight, 1.0, "weight 必须是 0~1 相似度，不是命中强度")

    def test_D_semantic_similarity_creates_edge(self):
        """判据 D：两条正文高度相似的记忆 → 出现 source='semantic' 的边。"""
        root = self._root()
        self._seed(root)
        root_p = Path(root)
        conn = fish.connect(root)
        try:
            base, _ = fish.record_memory(conn, root_p, project="TEST", title="部署检查清单",
                                         body=SIM_BODY, owner=ACTOR_ID)
            new, _ = fish.record_memory(
                conn, root_p, project="TEST", title="部署检查清单（补充）",
                body=SIM_BODY + "补充：回滚要留存旧库路径。", owner=ACTOR_ID)
            self._assert_semantic_edge(conn, new, base)
        finally:
            conn.close()

    def test_D_mutation_fts_match_off_goes_red(self):
        """变异自证：关掉 FTS 相似匹配（top-3 恒空）→ 判据 D 必须红。"""
        root = self._root("mut-d")
        self._seed(root)
        root_p = Path(root)
        conn = fish.connect(root)
        try:
            base, _ = fish.record_memory(conn, root_p, project="TEST", title="部署检查清单",
                                         body=SIM_BODY, owner=ACTOR_ID)
            with mock.patch.object(fish, "similar_codes", lambda *a, **k: []):
                new, _ = fish.record_memory(
                    conn, root_p, project="TEST", title="部署检查清单（补充）",
                    body=SIM_BODY + "补充：回滚要留存旧库路径。", owner=ACTOR_ID)
            with self.assertRaises(AssertionError):
                self._assert_semantic_edge(conn, new, base)
        finally:
            conn.close()

    # ------------------------------------------------------------ 判据 E

    def _assert_push_schema(self, conn):
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("memory_access_log", "links"):
            self.assertIn(t, tables, f"升级后缺表 {t}")
        al_cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_access_log)")}
        self.assertEqual(al_cols, {"id", "code", "citizen_id", "session_key", "project",
                                   "via", "payload", "accessed_at"},
                         f"memory_access_log 列不对：{sorted(al_cols)}")
        lk_cols = {r[1] for r in conn.execute("PRAGMA table_info(links)")}
        # 12 列 = P0 的 9 列 + 再巩固预留 3 列（14 §6）。仍是**精确集合**断言：
        # 多一列少一列都红，所以它照样能拦住"悄悄改表结构"。
        self.assertEqual(lk_cols, {"id", "src", "dst", "rel", "source", "weight",
                                   "evidence", "created_at", "removed_at",
                                   "last_reinforced_at", "reinforce_count", "suppressed"},
                         f"links 列不对：{sorted(lk_cols)}")
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        for name in ("ix_mal_code", "ix_mal_citizen", "ix_mal_session",
                     "ix_links_src", "ix_links_dst", "ix_links_rel"):
            self.assertIn(name, idx, f"升级后缺索引 {name}")

    def test_E_legacy_db_upgrade(self):
        """判据 E：没有这两张表的旧库，升级（SCHEMA + migrate）必须不报错且表建出来。"""
        # 形态 1：旧库根本没有这两张表
        root = self._root("legacy")
        self._make_legacy_db(root, LEGACY_SCHEMA)
        conn = fish.connect(root)
        try:
            conn.executescript(fish.SCHEMA)
            fish.migrate(conn)
            conn.executescript(fish.SCHEMA)   # 幂等：真实升级会反复跑
            fish.migrate(conn)
            self._assert_push_schema(conn)
            # 新表真的能用（不是"建了张空壳"）
            fish.append_access_log(conn, "TEST-261022-01", via="manual",
                                   citizen_id="C1", session_key="s")
            fish.add_link(conn, "TEST-261022-01", "TEST-261022-02", "related")
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM memory_access_log").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0], 1)
            # 去重：同一条边再写一次不会变两行（INSERT OR IGNORE 的幂等）
            self.assertFalse(
                fish.add_link(conn, "TEST-261022-01", "TEST-261022-02", "related"),
                "重复边必须被幂等吞掉")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0], 1)
            # 结构不变量：links 上覆盖 (src,dst,rel) 的唯一索引**只能有一条**。
            # 多了就是"新建表时又补了一条重复唯一索引"（白占空间 + 拖慢写入）。
            uniq = [r[1] for r in conn.execute("PRAGMA index_list(links)") if r[2]]
            over3 = [n for n in uniq
                     if tuple(x[2] for x in conn.execute(f"PRAGMA index_info({n})"))
                     == ("src", "dst", "rel")]
            self.assertEqual(len(over3), 1, f"links 上不该有重复的唯一索引：{over3}")
        finally:
            conn.close()

        # 形态 2：**同名表存在但列不全** —— 就是 citizens.is_owner 那次事故的形状
        root2 = self._root("legacy-crippled")
        self._make_legacy_db(root2, LEGACY_SCHEMA + """
CREATE TABLE links (id INTEGER PRIMARY KEY AUTOINCREMENT, src TEXT NOT NULL,
  dst TEXT NOT NULL);
CREATE TABLE memory_access_log (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL);
""")
        conn = fish.connect(root2)
        try:
            conn.executescript(fish.SCHEMA)   # IF NOT EXISTS 对旧表无操作
            fish.migrate(conn)                # 必须在这里补列 + 建索引
            self._assert_push_schema(conn)
            # 旧表没带 UNIQUE 时，迁移必须补上唯一索引——否则去重静默失效（边越写越多）
            self.assertTrue(fish.add_link(conn, "TEST-261022-01", "TEST-261022-02", "related"))
            self.assertFalse(fish.add_link(conn, "TEST-261022-01", "TEST-261022-02", "related"),
                             "迁移后重复边必须仍被吞掉（旧表缺 UNIQUE 是这次补的）")
        finally:
            conn.close()

    def test_E_mutation_index_before_column_goes_red(self):
        """变异自证（fixture 级）：把「建索引」提到「补列」之前 → 升级必须报错。

        这正是 is_owner 那次事故：SCHEMA 先于 migrate 执行，旧表缺列时
        CREATE INDEX 报 no such column，整个升级路径在启动时崩掉。
        """
        root = self._root("mut-e")
        self._make_legacy_db(root, LEGACY_SCHEMA + """
CREATE TABLE links (id INTEGER PRIMARY KEY AUTOINCREMENT, src TEXT NOT NULL,
  dst TEXT NOT NULL);
""")
        conn = fish.connect(root)
        try:
            conn.executescript(fish.SCHEMA)      # 旧表不会被 IF NOT EXISTS 改动
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("CREATE INDEX IF NOT EXISTS ix_links_src ON links(src) "
                             "WHERE removed_at IS NULL")
            # 正确顺序（真实现）必须能走过去
            fish.migrate(conn)
            self._assert_push_schema(conn)
        finally:
            conn.close()

    def test_E_mutation_migrate_alone_must_build_tables(self):
        """变异自证：只跑 migrate（不跑 SCHEMA）也必须建出两张表。

        若建表语句只写在 SCHEMA 里，这条红——那意味着旧库升级隐含依赖
        "调用方必须两件事都做"这种书上才有的约定。
        """
        root = self._root("mut-e2")
        self._make_legacy_db(root, LEGACY_SCHEMA)
        conn = fish.connect(root)
        try:
            fish.migrate(conn)
            self._assert_push_schema(conn)
        finally:
            conn.close()

    # ------------------------------------------------------------ 判据 F（配置登记）

    def test_F_push_config_registered(self):
        """P0 只登记不实现：10 个 push.* 键必须在 CONFIG_DEFAULTS 且能被 config list 读出。"""
        root = self._root()
        self._seed(root)
        conn = fish.connect(root)
        try:
            for key, (default, typ) in self.PUSH_KEYS.items():
                self.assertIn(key, fish.CONFIG_DEFAULTS, f"配置键未登记：{key}")
                self.assertEqual(fish.CONFIG_DEFAULTS[key][0], default,
                                 f"{key} 默认值不对")
                self.assertEqual(fish.CONFIG_DEFAULTS[key][1], typ, f"{key} 类型不对")
                self.assertEqual(fish.get_config(conn, key), default, f"{key} 读出来不对")
        finally:
            conn.close()
        # `fish config list` 是这套旋钮的唯一人可读出口：必须能看到
        buf = io.StringIO()
        ns = type("A", (), {"root": str(root), "action": "list",
                            "key": None, "value": None})()
        with contextlib.redirect_stdout(buf):
            fish.cmd_config(ns)
        out = buf.getvalue()
        for key in self.PUSH_KEYS:
            self.assertIn(key, out, f"`fish config list` 读不出 {key}")

    def test_F_mutation_unregistered_key_goes_red(self):
        """变异自证：键没登记时读它必须 KeyError（"登记"这件事本身可判红）。

        T02 拆包：`CONFIG_DEFAULTS` / `get_config` 已下沉 `fish_common`——patch 必须打在
        **使用方模块**（`fish_common`）上；`mock.patch.object(fish, ...)` 只改了 fish.py 的
        导入副本（from-import 快照），`get_config` 读的还是 `fish_common.CONFIG_DEFAULTS`。
        """
        import fish_common
        root = self._root("mut-f")
        self._seed(root)
        mutated = {k: v for k, v in fish_common.CONFIG_DEFAULTS.items()
                   if k != "push.gap_window_days"}
        conn = fish.connect(root)
        try:
            with mock.patch.object(fish_common, "CONFIG_DEFAULTS", mutated):
                with self.assertRaises(KeyError):
                    fish.get_config(conn, "push.gap_window_days")
        finally:
            conn.close()

    # ------------------------------------------------------------ 判据 G（硬约束）

    def test_G_tool_contract_unchanged(self):
        """硬约束：12 个工具的 inputSchema **只允许**多出显式声明的可选字段。

        为什么不是"字段集合原样不变"：P1-c 按设计给每个工具加了可选 token（旧调用不带
        照旧能跑）；R1（EVAL-261024-02）又给 inject 加了可选 `project`（显式声明当前
        项目，替代"最近更新者猜测"）。所以基线集合 + {token} + inject 的 {project} 才是
        本轮的正确契约；任何**别的**增删改仍然当场红，token/project 若被塞进 required
        也当场红（那会断掉所有旧调用）。
        """
        names = [t["name"] for t in mcp_server.TOOLS]
        # 2026-10-25（阶段 B）：新增 rename_citizen（名字认领）。有意的契约变更——
        # 用户裁定"开场指定名字却无法绑定"是 BUG；该工具自身的边界见 test_rename_citizen M1/M2。
        # 2026-10-25 晚加 session_start（用户批准，MCP 面唯一的签发口）——声明式契约变更。
        self.assertEqual(names, [
            "remember", "recall", "neighbors", "resolve_code", "read_body",
            "add_project", "supersede", "whoami", "register", "rename_citizen",
            "close_project", "reopen_project", "inject", "healthcheck",
            "session_start", "board",
            # 2026-10-26（T05 / B3）：知识库读 / 写两个工具（裁决 C2：读与写是**两个权限位**，
            # 不合成一个；不然后果是"只想看目录"的调用也拿到一份写权限的说明书）。
            "kb", "kb_write",
            # 2026-10-27（语料层 T03）：18 → 19，新增 `corpus`（三读动作；
            # 声明式契约变更，旧 18 个工具的描述/schema 由 golden 逐字锁住）。
            "corpus"])
        expect = {
            "remember": {"project", "title", "body", "mem_type", "tags", "cause",
                         "occurred_at", "supersedes", "attachments"},
            "recall": {"query", "code", "project", "tags", "scope", "limit",
                       "include_superseded"},
            "neighbors": {"code", "n", "strategy"},
            "resolve_code": {"code"},
            "read_body": {"code"},
            "add_project": {"prefix", "namespace"},
            "supersede": {"code", "by", "reason"},
            # 2026-10-26（反馈批 C · FB-03）：whoami 新增可选 `all`（相关公民清单）。
            # 与 test_identity_p1c 的同一处基线变更同源：声明式加一个可选字段。
            "whoami": {"all"},
            "register": {"display_name", "kind", "role", "host", "session_name"},
            "rename_citizen": {"name", "reason"},
            "close_project": {"prefix", "reason"},
            "reopen_project": {"prefix", "reason"},
            # 2026-10-26（反馈批 B · FB-02）：inject 的 `sections` / `include_board`
            # 同为**声明过的**基线变更（B 批漏登记，C 批同步；与 p1c 同一处口径）。
            "inject": {"budget", "project", "sections", "include_board"},
            "healthcheck": set(),
            # 2026-10-25 加（用户批准的工具数变更）：MCP 面唯一的签发口。
            "session_start": {"citizen", "display_name", "session_name", "project", "kind"},
            "board": {"action", "project", "doing", "surface", "kind", "text",
                      "status", "assume_surface"},
            # 2026-10-26（T05 / B3）：字段与架构设计 §3.3 逐字一致；token 由 `_attach_token` 补。
            # 第二轮（R15）：`kb` 增了可选 `allow_sensitive`（敏感文件正文的二次授权）。
            # 第四轮（S6 / S8）：`kb_write` 也增了可选 `allow_sensitive`——写面同样要求
            # 显式二次授权（凭证/.env 默认拒写），所以这条冻结字段必须同步；`token` 依旧
            # 不在 required 里（`test_02` 钉着）。
            "kb": {"action", "vault", "path", "query", "depth", "limit", "max_chars",
                   "allow_sensitive"},
            "kb_write": {"action", "vault", "path", "content", "frontmatter",
                         "base_sha256", "reason", "allow_sensitive"},
            # 2026-10-27（语料层 T03）：18 → 19，新增 `corpus`（三读动作）。
            # token 由 `_attach_token` 统一补；这里只列业务字段。
            "corpus": {"action", "host", "limit", "max_chars", "pointer", "project",
                       "query", "ranker", "role"},
        }
        for t in mcp_server.TOOLS:
            extra = {"token"} | ({"bind_token"} if t["name"] == "session_start" else set())
            self.assertEqual(set(t["inputSchema"]["properties"]), expect[t["name"]] | extra,
                             f"{t['name']} 的入参变了（冻结字段 + 可选 token + inject 的 project）")
            self.assertNotIn("token", t["inputSchema"].get("required") or [],
                             f"{t['name']} 把 token 变成必填了：旧调用会当场断")
        # 新增的 project 只允许出现在 inject，且必须**可选**（旧调用不带它照旧跑）。
        inject = next(t for t in mcp_server.TOOLS if t["name"] == "inject")
        self.assertNotIn("project", inject["inputSchema"].get("required") or [],
                         "inject 把 project 变成必填了：所有旧调用会当场断")

    # ---------------------------------- 判据 F/G/H（links 再巩固三列，14 §6 修正 1）

    def _assert_reinforce_columns(self, conn, where=""):
        """F/G 共用断言：三列都在，声明与默认值都对（变异用例会把它重跑一遍）。"""
        cols = _link_columns(conn)
        for name, typ, default in REINFORCE_COLS:
            self.assertIn(name, cols, f"{where} links 缺列 {name}")
            self.assertEqual(
                cols[name], (typ, default),
                f"{where} links.{name} 的声明不对：{cols[name]}（期望 {typ} / 默认 {default}）")

    def _assert_suppressed_index(self, conn):
        """H 的断言：索引存在、建在 suppressed 上、且是部分索引。"""
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' "
                           "AND name='ix_links_suppressed'").fetchone()
        self.assertIsNotNone(row, "缺索引 ix_links_suppressed（P1 的候选取数口）")
        norm = " ".join((row[0] or "").split()).lower()
        self.assertIn("on links(suppressed)", norm, f"索引建在别的列上：{norm}")
        self.assertIn("where removed_at is null", norm, f"索引不是部分索引：{norm}")
        # 不只信 SQL 文本：直接问"这条索引到底覆盖哪些列"
        self.assertEqual(
            [r[2] for r in conn.execute("PRAGMA index_info(ix_links_suppressed)")],
            ["suppressed"], "PRAGMA index_info 说 ix_links_suppressed 的列不是 suppressed")

    def test_F_legacy_db_gains_reinforce_columns(self):
        """判据 F：没有这 3 列的旧 links 表 → connect + SCHEMA + migrate 后三列都在。

        为什么单靠 `_LINKS_DDL` 不够：旧库的 links 已经存在，`CREATE TABLE IF NOT
        EXISTS` 对它**什么都不做**，唯一能把列补上的是 migrate 里的幂等 ALTER。
        """
        root = self._root("legacy-reinforce")
        self._make_legacy_db(root, LEGACY_SCHEMA + LEGACY_LINKS_DDL)
        conn = fish.connect(root)
        try:
            # 前提（夹具自检）：确认这真的是"没有那 3 列"的旧库
            before = _link_columns(conn)
            for name, _, _ in REINFORCE_COLS:
                self.assertNotIn(name, before, f"夹具不合格：旧库不该有 {name}")

            conn.executescript(fish.SCHEMA)     # IF NOT EXISTS：对旧表无操作
            fish.migrate(conn)                  # 必须在这里补列

            self._assert_reinforce_columns(conn, where="旧库升级后")

            # 旧数据必须原样都在，新列落到列声明的默认值上（不是 NULL、也不是改写旧行）
            rows = conn.execute(
                "SELECT src,dst,rel,weight,last_reinforced_at,reinforce_count,suppressed "
                "FROM links").fetchall()
            self.assertEqual(
                rows, [("TEST-261022-01", "TEST-261022-02", "related", 0.8, None, 0, 0)],
                f"升级动了旧数据，或新列的默认值没落上：{rows}")

            # 幂等：真实升级会反复跑，第二遍不许报错也不许多出列
            conn.executescript(fish.SCHEMA)
            fish.migrate(conn)
            self.assertEqual(sorted(_link_columns(conn)), sorted(before.keys() | {
                n for n, _, _ in REINFORCE_COLS}),
                "重复 migrate 改变了 links 的列集合（幂等性破了）")
        finally:
            conn.close()

    def test_F_mutation_dropping_alter_goes_red(self):
        """变异自证（code 级）：把三列的 ALTER 从迁移清单里摘掉 → 判据 F 必须红。

        等价于"改真源码删掉 migrate 里的补列语句"，但可重跑、不用改仓库文件。
        两个变体都要红，红的形态不同，各自证明一件不同的事：
          变体 1（只摘 ALTER，索引还在）→ migrate 自己就撞 `no such column: suppressed`，
                   这正是 07 的 is_owner 事故形状：索引先于列 → 升级路径当场崩。
          变体 2（ALTER 与依赖它的索引一起摘）→ migrate 能跑完，但三列补不出来，
                   **共享断言**必须报错——这一条才证明了断言本身有判别力。
        """
        root = self._root("mut-f-reinforce")
        self._make_legacy_db(root, LEGACY_SCHEMA + LEGACY_LINKS_DDL)
        drop = {n for n, _, _ in REINFORCE_COLS}
        old_cols = tuple(c for c in fish._LINKS_COLS if c[0] not in drop)
        old_idx = tuple(s for s in fish._LINKS_INDEXES if "ix_links_suppressed" not in s)
        self.assertEqual(len(old_cols), 8, "夹具前提：摘掉三列后补列清单应剩 8 项")
        self.assertEqual(len(old_idx), 3, "夹具前提：摘掉一条后索引清单应剩 3 条")
        conn = fish.connect(root)
        try:
            conn.executescript(fish.SCHEMA)

            # 变体 1：列没了、索引还在 → 建索引当场失败
            with mock.patch.object(fish, "_LINKS_COLS", old_cols), \
                    self.assertRaises(sqlite3.OperationalError):
                fish.migrate(conn)

            # 变体 2：列与索引一起摘 → 跑得完，但断言必须红
            with mock.patch.object(fish, "_LINKS_COLS", old_cols), \
                    mock.patch.object(fish, "_LINKS_INDEXES", old_idx):
                fish.migrate(conn)
            with self.assertRaises(AssertionError):
                self._assert_reinforce_columns(conn, where="变异：摘掉 ALTER 后")
        finally:
            conn.close()

    def test_G_fresh_db_has_reinforce_columns(self):
        """判据 G：新库**建表那一刻**就带这 3 列，两个计数列默认 0。

        刻意先**不跑 migrate**：判据要证明的是"建表就带"。若让 migrate 兜底，
        这条判据对"DDL 少了列"就失去判别力（补列会把它悄悄修好）。
        """
        root = self._root()
        conn = fish.connect(root)
        try:
            conn.executescript(fish.SCHEMA)          # 只建表，不补列
            self._assert_reinforce_columns(conn, where="新库建表后")

            # 默认值必须真在列上：P0 不写这三列，若默认值只是 Python 侧兜底，
            # 直接走 SQL 写入的行就是 NULL，P1 的 `WHERE suppressed=0` 会漏掉全部边。
            fish.add_link(conn, "TEST-261022-01", "TEST-261022-02", "related")
            got = conn.execute("SELECT last_reinforced_at, reinforce_count, suppressed "
                               "FROM links").fetchone()
            self.assertEqual(got, (None, 0, 0),
                             f"新写入的边三列不是默认值：{got}（P0 只加列，必须走 DEFAULT）")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM links WHERE suppressed=0 "
                             "AND removed_at IS NULL").fetchone()[0], 1,
                "suppressed=0（正常）的边必须能被查出来——P1 的候选池就靠这一句")

            # 生产装配路径（connect + SCHEMA + migrate）同样要成立
            fish.migrate(conn)
            self._assert_reinforce_columns(conn, where="新库 migrate 后")
        finally:
            conn.close()

    def test_G_mutation_ddl_missing_column_goes_red(self):
        """变异自证（code 级）：建表 DDL 删掉 suppressed → 判据 G 必须红。

        只改 DDL、**不动** migrate 的补列清单：这样"红"只能来自建表本身缺列，
        而不是被补列逻辑掩盖过去。
        """
        root = self._root("mut-g-reinforce")
        marker = "  suppressed         INTEGER DEFAULT 0,\n"
        broken = fish.SCHEMA.replace(marker, "")
        self.assertNotEqual(broken, fish.SCHEMA, "变异没生效：SCHEMA 里没匹配到 suppressed 那行")
        conn = fish.connect(root)
        try:
            conn.executescript(broken)               # 只建表（变异版），不迁移
            with self.assertRaises(AssertionError):
                self._assert_reinforce_columns(conn, where="变异：DDL 少一列")
        finally:
            conn.close()

    def test_H_suppressed_index_exists(self):
        """判据 H：ix_links_suppressed 在库里，建在 suppressed 上，且是部分索引。

        为什么必须是部分索引：候选池的查询永远带 `removed_at IS NULL`，把已删除的边
        一起索引进去只是白占空间与写入——这是查询形态决定的，不是审美。
        """
        root = self._root()
        self._seed(root)
        conn = fish.connect(root)
        try:
            self._assert_suppressed_index(conn)
        finally:
            conn.close()

        # 索引只放 migrate（沿用 P0 的决策）：旧库升级那条路径也必须建出来
        root2 = self._root("legacy-index")
        self._make_legacy_db(root2, LEGACY_SCHEMA + LEGACY_LINKS_DDL)
        conn = fish.connect(root2)
        try:
            conn.executescript(fish.SCHEMA)
            fish.migrate(conn)
            self._assert_suppressed_index(conn)
        finally:
            conn.close()

    def test_H_mutation_dropping_index_goes_red(self):
        """变异自证（code 级）：把 suppressed 索引从 migrate 的索引清单里删掉 → 判据 H 必须红。"""
        root = self._root("mut-h-index")
        keep = tuple(s for s in fish._LINKS_INDEXES if "ix_links_suppressed" not in s)
        self.assertEqual(len(keep), len(fish._LINKS_INDEXES) - 1,
                         "夹具前提：索引清单里应恰好有一条 suppressed 索引")
        conn = fish.connect(root)
        try:
            conn.executescript(fish.SCHEMA)
            with mock.patch.object(fish, "_LINKS_INDEXES", keep):
                fish.migrate(conn)
            with self.assertRaises(AssertionError):
                self._assert_suppressed_index(conn)
        finally:
            conn.close()

    # ---------------------------------- 判据 I/J/K/L（push_log，16 §5.6 / 论文 §6.3）

    def _assert_pending_push_rows(self, conn, pack):
        """判据 I 的断言：注入包展示的每条编码都有一行待判推送（变异用例会重跑它）。"""
        rows = fish.push_log_rows(conn, channel="inject", limit=500)
        self.assertTrue(rows, "push_log 一行都没写：inject 的写入点丢了，推送效果从此没有数据")
        codes = {r[0] for r in rows}
        # 真值取自**返回给调用方的注入包文本**，不是被测实现的内部变量：
        # 包里出现过的编码 = 真正展示过的编码，一条都不能漏；没出现的一条都不许编。
        displayed = {c for (c,) in conn.execute("SELECT code FROM code_index").fetchall()
                     if c in pack}
        self.assertTrue(displayed, "夹具不合格：注入包一个编码都没展示")
        self.assertEqual(codes, displayed,
                         f"push_log 的编码集合与注入包展示不一致："
                         f"多 {sorted(codes - displayed)} / 少 {sorted(displayed - codes)}")
        m = re.search(r"<!-- (\d+)/(\d+) token -->", pack)
        self.assertIsNotNone(m, "注入包没有 token 尾注，拿不到本次推送预算")
        tokens = int(m.group(1))
        self.assertGreater(tokens, 0, "注入包 token 数必须为正")
        for (code, sess, cit, proj, channel, budget, pushed,
             reaction, reacted, latency, cand) in rows:
            self.assertEqual(channel, "inject", f"channel 必须是 inject，实际 {channel!r}")
            self.assertEqual(sess, SESSION,
                             f"{code} 的 session_key 应是当前会话，实际 {sess!r}")
            self.assertEqual(cit, ACTOR_ID, f"{code} 的 citizen_id 应是当前行动者，实际 {cit!r}")
            self.assertEqual(proj, "TEST", f"{code} 的 project 应从编码反查，实际 {proj!r}")
            self.assertEqual(budget, tokens,
                             f"budget_tokens 应记本次注入包 token 数 {tokens}，实际 {budget!r}")
            self.assertIsNotNone(fish.parse_utc(pushed),
                                 f"pushed_at 必须是 ISO8601 UTC，实际 {pushed!r}")
            self.assertIsNone(reaction, f"刚推出去的行必须待判（NULL），实际 {reaction!r}")
            self.assertIsNone(reacted, "待判行不该有 reacted_at")
            self.assertIsNone(latency, "待判行不该有 latency_sec")
            self.assertIsNone(cand, "候选池未实现，candidate_id 必须留空（不许编）")

    def test_I_inject_writes_push_log(self):
        """判据 I：调 inject → push_log 覆盖**包里出现过的每一条编码**。

        2026-10-25 · P1-a：删掉了原来写死的 `len(...) == 5`。原因不是"放宽"，是**变严**——
        `_assert_pending_push_rows` 做的是**集合对账**（包里出现的编码 == push_log 的编码集），
        它比"数字等于 5"严得多：数字对得上但记错了条目，旧断言是绿的。
        而写死的 5 依赖「L1 三条 + L2 今日编码两条」这个段结构，L2 那段已删除
        （研判见 `31` §2），数字必然过时。

        夹具自检留一条下限断言：不然夹具退化成 0 条时，集合对账会"两个空集相等"而空转。
        """
        root = self._root()
        self._seed(root)
        srv = self._server(root)
        try:
            for i in range(5):
                self._remember(srv, title=f"推送条目{i}", body=f"正文 {i}")
            pack = srv.call_tool("inject", {})
        finally:
            srv.conn.close()
        conn = fish.connect(root)          # 独立连接读 = 真值不在被测实现手里
        try:
            self._assert_pending_push_rows(conn, pack)
            self.assertGreaterEqual(len(fish.push_log_rows(conn, limit=500)), 3,
                                    "夹具应至少展示 3 条（L1 最近三件事），否则判据会空转")
        finally:
            conn.close()

    def test_I_mutation_dropping_write_goes_red(self):
        """变异自证：写入点丢了 / 漏记一条 → 判据 I 都必须红。

        2026-10-25 · P1-a：原第二个变体是 `l1_only`（只记 L1 三条）。L2 的「今日编码」段
        删除后，"只记 L1" 变成了**正确行为**（包里也确实只有 L1 那几条），变异随之失去
        意义——一个永远不会红的变异等于没有变异。改为 `drop_one`：漏记任意一条，
        这才是「展示过 = 必须记进 push_log」这条契约的真实失败模式。
        """
        for variant in ("drop_write", "drop_one"):
            with self.subTest(variant=variant):
                root = self._root(f"mut-i-{variant}")
                self._seed(root)
                srv = self._server(root)
                try:
                    for i in range(5):
                        self._remember(srv, title=f"推送条目{i}", body=f"正文 {i}")
                    if variant == "drop_write":
                        with mock.patch.object(fish, "append_push_log", lambda *a, **k: 0):
                            pack = srv.call_tool("inject", {})
                    else:
                        real = fish.append_push_log

                        def drop_one(codes, **kw):
                            return real(list(codes)[:-1], **kw)

                        with mock.patch.object(fish, "append_push_log", drop_one):
                            pack = srv.call_tool("inject", {})
                finally:
                    srv.conn.close()
                conn = fish.connect(root)
                try:
                    with self.assertRaises(AssertionError):
                        self._assert_pending_push_rows(conn, pack)
                finally:
                    conn.close()

    def _assert_accepted_row(self, row, where):
        """判据 J 的断言：这一行已被回填成 accepted，且延迟自洽（变异用例会重跑它）。"""
        code, _sess, _cit, _proj, _channel, _budget, pushed, reaction, reacted, latency, _cand = row
        self.assertEqual(reaction, "accepted",
                         f"{where}：reaction 应为 accepted，实际 {reaction!r}")
        self.assertIsNotNone(fish.parse_utc(reacted),
                             f"{where}：reacted_at 必须是 ISO8601 UTC，实际 {reacted!r}")
        self.assertIsInstance(latency, int,
                              f"{where}：latency_sec 应是整数秒，实际 {latency!r}")
        self.assertGreaterEqual(latency, 0, f"{where}：延迟不能为负")
        # 跨列不变量：latency_sec 必须真的等于 reacted_at − pushed_at（不是写死的 0）
        delta = int((fish.parse_utc(reacted) - fish.parse_utc(pushed)).total_seconds())
        self.assertEqual(latency, delta,
                         f"{where}：latency_sec={latency} 与 reacted_at−pushed_at={delta} 不一致")

    def test_J_read_body_backfills_accepted(self):
        """判据 J：先 inject（拿到推送）→ 再 read_body / resolve 同一条 → 回填 accepted。

        同时钉住两条过滤（不是可选优化，是契约）：
          · 会话 —— 别的会话被推过、本会话读了，不能算那个会话的推送被接受
          · 窗口 —— 25 小时前推的、现在才读，中间隔了一天，算不到这次头上
        """
        root = self._root()
        self._seed(root)
        srv = self._server(root)
        old_25h = (datetime.datetime.now(datetime.timezone.utc)
                   - datetime.timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            a = self._remember(srv, title="甲", body="甲的正文")
            b = self._remember(srv, title="乙", body="乙的正文")
            c = self._remember(srv, title="丙", body="丙的正文")
            srv.call_tool("inject", {})
            # 两条对照行：专供"过滤失效"的变异去撞
            fish.append_push_log(srv.conn, a, channel="inject", session_key="sess-other",
                                 citizen_id=ACTOR_ID, budget_tokens=10)
            fish.append_push_log(srv.conn, b, channel="inject", session_key=SESSION,
                                 citizen_id=ACTOR_ID, budget_tokens=10, pushed_at=old_25h)
            self.assertEqual(len(fish.push_log_rows(srv.conn, code=a, limit=9)), 2,
                             "夹具前提：a 应有 2 条待判行（本会话 + 别的会话）")
            srv.call_tool("read_body", {"code": a})
            srv.call_tool("resolve_code", {"code": b})
        finally:
            srv.conn.close()
        conn = fish.connect(root)
        try:
            for code in (a, b):
                rows = fish.push_log_rows(conn, code=code, limit=50)
                accepted = [r for r in rows if r[7] == "accepted"]
                self.assertEqual(len(accepted), 1,
                                 f"{code} 应恰好有 1 行被回填（本会话那行），实际 {rows}")
                self._assert_accepted_row(accepted[0], f"{code} 本会话推送")
            # 过滤 1：别的会话那行不许被本会话的读取回填
            other = fish.push_log_rows(conn, code=a, session_key="sess-other", limit=9)
            self.assertEqual(len(other), 1, f"夹具前提：应有一行别的会话的推送，实际 {other}")
            self.assertIsNone(other[0][7], "别的会话的推送被本会话的读取回填了（会话过滤失效）")
            # 过滤 2：25 小时前那行不许被回填
            old = [r for r in fish.push_log_rows(conn, code=b, limit=50) if r[6] == old_25h]
            self.assertEqual(len(old), 1, f"夹具前提：b 应有一条 25h 前的推送行，实际 {old}")
            self.assertIsNone(old[0][7], "超过 24h 的推送被回填了（窗口过滤失效）")
            # 对照 3：没读过的 c 必须一直待判（否则"回填"变成了"整表接受"）
            pending_c = fish.push_log_rows(conn, code=c, limit=50)
            self.assertTrue(pending_c, "夹具前提：c 应被推过")
            for r in pending_c:
                self.assertIsNone(r[7], f"没读过的 {c} 被回填了：{r}")
        finally:
            conn.close()

    def test_J_mutation_dropping_backfill_goes_red(self):
        """变异自证：把回填摘掉 → 判据 J 必须红（"推送后读了"没被记下来）。"""
        root = self._root("mut-j")
        self._seed(root)
        srv = self._server(root)
        try:
            a = self._remember(srv, title="甲", body="甲的正文")
            srv.call_tool("inject", {})
            with mock.patch.object(fish, "mark_push_accepted", lambda *ar, **kw: 0):
                srv.call_tool("read_body", {"code": a})
            rows = fish.push_log_rows(srv.conn, code=a, limit=9)
        finally:
            srv.conn.close()
        self.assertTrue(rows, "夹具前提：a 应被推过")
        with self.assertRaises(AssertionError):
            self._assert_accepted_row(rows[0], "变异：回填被摘掉后")

    def test_K_stale_push_marked_ignored(self):
        """判据 K：pushed_at 超过 24h 且未反应 → daily_report 的判定函数置 ignored。"""
        # 配置项已登记且默认 24 小时（判定窗口的唯一来源）
        self.assertIn("push.outcome_window_hours", fish.CONFIG_DEFAULTS,
                      "配置键未登记：push.outcome_window_hours")
        self.assertEqual(fish.CONFIG_DEFAULTS["push.outcome_window_hours"], (24, "int", None),
                         "push.outcome_window_hours 的默认值/类型不对")
        root = self._root()
        self._seed(root)
        srv = self._server(root)
        try:
            a = self._remember(srv, title="甲", body="甲的正文")
            b = self._remember(srv, title="乙", body="乙的正文")
            srv.call_tool("inject", {})
        finally:
            srv.conn.close()
        db = Path(root) / "index.sqlite"
        conn = fish.connect(root)
        try:
            aged = _age_push_rows(conn, a, hours=25)
        finally:
            conn.close()
        self.assertGreaterEqual(aged, 1, "夹具前提：a 应有待判推送行可改成 25 小时前")
        st = daily_report.mark_stale_pushes(db_path=db)
        self.assertEqual(st["window_hours"], 24,
                         "默认窗口应是 24 小时（push.outcome_window_hours）")
        self.assertEqual(st["ignored"], aged,
                         f"应有 {aged} 行被判 ignored，实际 {st['ignored']}")
        conn = fish.connect(root)
        try:
            for r in fish.push_log_rows(conn, code=a, limit=20):
                self.assertEqual(r[7], "ignored", f"超时未反应应判 ignored：{r}")
                self.assertIsNone(r[8], "ignored 是「判定」不是「反应」→ reacted_at 必须为空")
                self.assertIsNone(r[9], "没有反应就没有 latency_sec")
            # 窗口内（刚推的）不许被误判 —— 否则"判忽略"就变成"全表判死"
            for r in fish.push_log_rows(conn, code=b, limit=20):
                self.assertIsNone(r[7], f"还在窗口内的推送被误判成 ignored：{r}")
            # 幂等：真实场景每天跑一次，已经判过的行不该被重复统计
            again = daily_report.mark_stale_pushes(db_path=db)
            self.assertEqual(again["ignored"], 0, "已判过的行被重复统计了")
        finally:
            conn.close()

    def test_K_mutation_window_off_or_sweep_off_goes_red(self):
        """变异自证：窗口设 0（关闭判定）/ 摘掉判定函数 → 判据 K 都必须红。

        窗口 0 = 关闭，沿用 decay.*=0「永不」的既有约定（不是"0 小时立刻判死"）。
        """
        for variant in ("window_0", "sweep_off"):
            with self.subTest(variant=variant):
                root = self._root(f"mut-k-{variant}")
                self._seed(root)
                srv = self._server(root)
                try:
                    a = self._remember(srv, title="甲", body="甲的正文")
                    srv.call_tool("inject", {})
                finally:
                    srv.conn.close()
                db = Path(root) / "index.sqlite"
                conn = fish.connect(root)
                try:
                    aged = _age_push_rows(conn, a, hours=25)
                    if variant == "window_0":
                        fish.set_config(conn, "push.outcome_window_hours", 0)
                finally:
                    conn.close()
                self.assertGreaterEqual(aged, 1, "夹具前提：a 应有待判推送行")
                if variant == "window_0":
                    st = daily_report.mark_stale_pushes(db_path=db)
                else:
                    with mock.patch.object(fish, "close_stale_pushes", lambda *a, **k: 0):
                        st = daily_report.mark_stale_pushes(db_path=db)
                self.assertEqual(st["ignored"], 0, f"{variant}：变异下不该判出 ignored")
                conn = fish.connect(root)
                try:
                    for r in fish.push_log_rows(conn, code=a, limit=20):
                        self.assertIsNone(
                            r[7], f"{variant}：变异下推送行必须仍是待判（判据 K 失去判别力）")
                finally:
                    conn.close()

    def _assert_push_log_schema(self, conn, where=""):
        """判据 L 的断言：表在、列集合精确、三条索引在且形态正确（变异用例会重跑它）。"""
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("push_log", tables, f"{where} 缺表 push_log")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(push_log)")}
        self.assertEqual(cols, self.PUSH_LOG_COLUMNS,
                         f"{where} push_log 列不对：{sorted(cols)}")
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        for name in ("ix_push_session", "ix_push_code", "ix_push_reaction"):
            self.assertIn(name, idx, f"{where} 缺索引 {name}")
        # 不只信 SQL 文本：直接问"这条索引到底覆盖哪些列"
        self.assertEqual([r[2] for r in conn.execute("PRAGMA index_info(ix_push_session)")],
                         ["session_key", "pushed_at"], f"{where} ix_push_session 的列不对")
        self.assertEqual([r[2] for r in conn.execute("PRAGMA index_info(ix_push_code)")],
                         ["code", "pushed_at"], f"{where} ix_push_code 的列不对")
        # 待判索引必须是 `reaction IS NULL` 上的部分索引（它是收口与回填的唯一取数口）
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' "
                           "AND name='ix_push_reaction'").fetchone()
        norm = " ".join((row[0] or "").split()).lower()
        self.assertIn("on push_log(reaction)", norm,
                      f"{where} ix_push_reaction 建在别的列上：{norm}")
        self.assertIn("where reaction is null", norm,
                      f"{where} ix_push_reaction 不是部分索引：{norm}")
        self.assertEqual([r[2] for r in conn.execute("PRAGMA index_info(ix_push_reaction)")],
                         ["reaction"], f"{where} ix_push_reaction 的列不对")

    def test_L_legacy_db_gets_push_log(self):
        """判据 L：没有 push_log 的旧库 → connect + SCHEMA + migrate → 表在、索引在。"""
        # 形态 1：旧库根本没有这张表（生产升级的真实形状）
        root = self._root("legacy-pushlog")
        self._make_legacy_db(root, LEGACY_SCHEMA)
        conn = fish.connect(root)
        try:
            before = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("push_log", before, "夹具不合格：旧库不该有 push_log")
            conn.executescript(fish.SCHEMA)
            fish.migrate(conn)
            self._assert_push_log_schema(conn, "旧库升级后")
            # 幂等：真实升级会反复跑（SCHEMA 与 migrate 都可能被调用多次）
            conn.executescript(fish.SCHEMA)
            fish.migrate(conn)
            self._assert_push_log_schema(conn, "重复升级后")
            # 新表真能用（不是建了张空壳）
            n = fish.append_push_log(conn, "TEST-261022-01", channel="inject",
                                     session_key="s", citizen_id="C1", budget_tokens=42)
            self.assertEqual(n, 1)
            got = fish.push_log_rows(conn, code="TEST-261022-01")
            self.assertEqual(len(got), 1, f"升级后的表写不进去：{got}")
            self.assertEqual(got[0][3], "TEST", "project 应从旧库的编码反查出来")
            self.assertEqual(got[0][5], 42, "budget_tokens 没落上")
            self.assertIsNone(got[0][7], "新写入的行必须待判")
        finally:
            conn.close()

        # 形态 2：**同名表存在但列不全** —— citizens.is_owner 那次事故的形状
        root2 = self._root("legacy-pushlog-crippled")
        self._make_legacy_db(root2, LEGACY_SCHEMA + """
CREATE TABLE push_log (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT);
""")
        conn2 = fish.connect(root2)
        try:
            conn2.executescript(fish.SCHEMA)   # IF NOT EXISTS：旧表不会被改动
            fish.migrate(conn2)                # 必须在这里补列 + 建索引
            self._assert_push_log_schema(conn2, "同名缺列旧表升级后")
        finally:
            conn2.close()

        # 形态 3（纪律）：只跑 migrate（不跑 SCHEMA）也必须建出表与索引 ——
        # 升级路径不许隐含"调用方两件事都要做"这种书上才有的约定。
        root3 = self._root("legacy-pushlog-migrate-only")
        self._make_legacy_db(root3, LEGACY_SCHEMA)
        conn3 = fish.connect(root3)
        try:
            fish.migrate(conn3)
            self._assert_push_log_schema(conn3, "只跑 migrate")
        finally:
            conn3.close()

    def test_L_mutation_create_after_index_goes_red(self):
        """变异自证（fixture 级）：同名旧表缺列时把补列摘掉 → 建索引当场 no such column。

        这正是 07 的 is_owner 事故形状：SCHEMA 先于 migrate 执行，旧表列不齐时
        CREATE INDEX 报 no such column，整个升级路径在启动时崩掉。
        等价于"把 CREATE/ALTER 放到 migrate 之后"——顺序错了，升级就崩在启动。
        """
        root = self._root("mut-l")
        self._make_legacy_db(root, LEGACY_SCHEMA + """
CREATE TABLE push_log (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT);
""")
        conn = fish.connect(root)
        try:
            conn.executescript(fish.SCHEMA)    # IF NOT EXISTS：旧表不会被改动
            with mock.patch.object(fish, "_PUSH_LOG_COLS", ()), \
                    self.assertRaises(sqlite3.OperationalError):
                fish.migrate(conn)             # 补列没了 → 建索引撞 no such column
        finally:
            conn.close()
        # 真实现（先补列、后建索引）必须能走完
        conn = fish.connect(root)
        try:
            fish.migrate(conn)
            self._assert_push_log_schema(conn, "变异恢复后")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
