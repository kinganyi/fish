#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话树 W1 · schema + 适配器骨架验收（113 §2.6 / §2.2，2026-11-25）。

## 这一波验收什么

W1 的承诺是"系统行为与改动前**逐字节相同**，唯一差别是库里多了两张空表和一个
没人调用的模块"（113 §2.1）。所以判据全在**结构**与**零写入**上，不测任何业务行为：

  J-W1-1 两表存在且 **0 行**（migrate 前后都是 0）
  J-W1-2 既有读数迁移前后**逐条不变**（`tool_activity` / `citizens` / `session_tokens`）
  J-W1-3 `origin='main' + 有父` 被库级 CHECK 拒（J-D4 的库级兜底）；其余闭集同测
  J-W1-4 `migrate` 连跑两次幂等（第二次不报错、表仍 0 行、库结构快照不变）
  J-W1-5 旧库升级：`migrate` 会建出两张表 + 5 条索引（SCHEMA 缺席时也成立）
  J-W1-6 列契约与索引：`NODE_FIELDS` 与 DDL 逐列对齐、主键列序正确、索引在册

## 每条判据都配了变异（见 `mutation_lineage_w1.py`）

纪律（03 §G / 09 RX-2，与 `test_identity_p1a` / `test_push_p0` 同一套）：**判据先证明
它在坏输入下会红**，否则"全绿"可能只是判据读空。本文件里两种对照是内联的：

  · J-W1-3 的对照：在**没有这条 CHECK** 的同名表上，同一 INSERT **必须成功**
    ——证明红是约束在挡，不是缺列/类型错误碰巧报错；
  · J-W1-2 的对照：种子行在 migrate 前后各数一遍，两边都 >0（否则"相等"是 0==0 假绿）；
  · J-W1-1 的数据级变异在 `mutation_lineage_w1.py`：直接 INSERT 一行（绕过 `upsert_node`
    与任何开关）→ 同一个 `assert_lineage_empty` 必须红，删掉后复绿。
"""
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish          # noqa: E402
import fish_db       # noqa: E402  —— 建表函数族住这里（会话树的 ensure 已搬到 fish_lineage）
import fish_lineage  # noqa: E402
import fish_lineage_schema   # noqa: E402  —— W1b 抽出的纯 DDL 叶子
import fish_schema   # noqa: E402

#: 本波新建的两张表（**只建表、不写行**）
LINEAGE_TABLES = ("session_lineage", "session_identity")

#: 本波新建的 5 条索引（3 条树取数口 + 2 条身份取数口）
LINEAGE_INDEXES = ("idx_lineage_parent", "idx_lineage_depth", "idx_lineage_basis",
                   "ix_identity_citizen", "ix_identity_inherited")

#: §0.3 基线里最容易被 W1 误伤的三张老表（判据 J-W1-2 的对拍对象）
LEGACY_TABLES = ("tool_activity", "citizens", "session_tokens")

_NOW = "2026-11-25T00:00:00Z"


# ---------------------------------------------------------------- 可复用判据（变异脚本共用）
def table_names(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def index_names(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def row_counts(conn, tables=LINEAGE_TABLES) -> dict:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def assert_lineage_empty(case: unittest.TestCase, conn, where: str = "") -> None:
    """J-W1-1 的**唯一实现**：测试与变异脚本共用它，避免两份判据各自漂开。

    刻意先查"表在不在"再查行数：表不存在时 `COUNT(*)` 抛的是 `no such table`，
    那会让"0 行"看起来像"读数失败"，而失败原因完全不同（缺结构 vs 偷偷写行）。
    """
    missing = [t for t in LINEAGE_TABLES if t not in table_names(conn)]
    case.assertEqual(missing, [], f"{where}缺表：{missing}")
    counts = row_counts(conn)
    case.assertEqual(counts, {t: 0 for t in LINEAGE_TABLES},
                     f"{where}W1 承诺一行都不写，实际行数：{counts}")


def legacy_counts(conn) -> dict:
    return row_counts(conn, LEGACY_TABLES)


def _structure_snapshot(conn) -> dict:
    """库结构快照（表/索引/视图的 DDL 文本）：幂等判据比对它，比数行数更能发现重建。"""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")}


def node(**over):
    """一个合法节点（`extra` 用 dict 传，按契约应被转成 JSON 文本）。"""
    base = dict(host="dsh", session_id="s-1", parent_session="s-0",
                lineage_path="/s-0/s-1", depth=1, depth_host=1,
                origin="subagent", relation="spawn", agent_id=None,
                agent_role="dafish-team", agent_label=None, agent_path=None,
                cwd="/w", agent_preset="dafish-team", basis="header",
                source="dsh:header", extra={"infer_rule": "test"},
                observed_at=_NOW)
    base.update(over)
    return fish_lineage.SessionNode(**base)


class _TempDb(unittest.TestCase):
    """每个判据一个临时库（**绝不碰生产 `p0/data`**，conftest 的护栏也盯着这条）。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="fish-w1-")
        self.root = Path(self._tmp) / "data"
        self.root.mkdir(parents=True)
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:      # noqa: BLE001
            pass
        shutil.rmtree(self._tmp, ignore_errors=True)


class LineageSchemaTest(_TempDb):
    """J-W1-1 / J-W1-2：两张空表 + 既有读数不被动。"""

    def test_j_w1_1_tables_exist_and_are_empty(self):
        """**核心**：`SCHEMA` 建出两张表；它们 0 行（W1 一行都不写）。"""
        assert_lineage_empty(self, self.conn, "【SCHEMA 之后】")

    def test_j_w1_1b_migrate_writes_no_rows(self):
        """`migrate()` 也不许写行：它是每条命令都会跑的那一步（113 §2.5）。"""
        fish.migrate(self.conn)
        assert_lineage_empty(self, self.conn, "【migrate 之后】")

    def test_j_w1_2_legacy_counts_unchanged_by_migrate(self):
        """J-W1-2：既有读数逐条对拍（对拍对象是**本库实测**，不锚 110 的历史绝对数）。

        对照内联：种子行在迁移前后都 > 0——否则 `{"x":0}=={"x":0}` 是假绿。
        """
        for i in range(2):
            self.conn.execute(
                "INSERT INTO tool_activity (host, session_key, tool_name, action, seen_at) "
                "VALUES ('dsh', ?, 'write', 'write', ?)", (f"dsh:s-{i}", _NOW))
            self.conn.execute(
                "INSERT INTO citizens (citizen_id, display_name, host) VALUES (?,?,?)",
                (f"cit-c{i}", "未命名·dsh#t", "dsh"))
            self.conn.execute(
                "INSERT INTO session_tokens (token_hash, citizen_id, session_key, evidence, "
                "channel, issued_at) VALUES (?,?,?,?,?,?)",
                (f"hash{i}", f"cit-c{i}", f"dsh:s-{i}", "machine", "cli", _NOW))
        self.conn.commit()

        before = legacy_counts(self.conn)
        self.assertEqual(before, {t: 2 for t in LEGACY_TABLES}, "夹具不合格：种子行没写进去")
        structure_before = _structure_snapshot(self.conn)

        fish.migrate(self.conn)

        self.assertEqual(legacy_counts(self.conn), before,
                         "migrate 改动了既有表的行数——W1 只许加空表")
        after = _structure_snapshot(self.conn)
        # 迁移会补 `ux_citizens_owner` 这类索引（既有行为），但**不许删除/重建老对象**
        lost = sorted(set(structure_before) - set(after))
        self.assertEqual(lost, [], f"migrate 弄丢了既有表/索引：{lost}")
        for name in ("tool_activity", "citizens", "session_tokens"):
            self.assertEqual(after[name], structure_before[name],
                             f"{name} 的 DDL 被改写了——这是结构迁移，不属 W1")
        assert_lineage_empty(self, self.conn, "【migrate 之后】")

    def test_j_w1_5_old_library_upgrade_creates_tables_and_indexes(self):
        """J-W1-5：**旧库**（SCHEMA 里没有会话树）跑一次 migrate 就补齐结构与索引。

        这条同时是 `migrate()` 里那句调用的判据：把调用摘掉，本测试立刻红
        （变异 M4），因为旧库靠 `CREATE TABLE IF NOT EXISTS` 是永远补不出来的。
        """
        old_conn = fish.connect(Path(self._tmp) / "old")
        try:
            old_schema = fish.SCHEMA.replace(fish_schema._LINEAGE_SCHEMA, "")
            old_conn.executescript(old_schema)
            self.assertNotIn("session_lineage", table_names(old_conn),
                             "夹具不合格：旧库不该已经有会话树表")
            for i in range(2):        # 旧库带着数据升级：读数必须一条不变
                old_conn.execute(
                    "INSERT INTO tool_activity (host, session_key, tool_name, action, seen_at) "
                    "VALUES ('dsh', ?, 'write', 'write', ?)", (f"dsh:old-{i}", _NOW))
            old_conn.commit()
            before = legacy_counts(old_conn)
            self.assertGreater(before["tool_activity"], 0, "夹具不合格：旧库没有数据")

            fish.migrate(old_conn)

            self.assertEqual(legacy_counts(old_conn), before, "升级改动了旧库的行数")
            assert_lineage_empty(self, old_conn, "【旧库升级后】")
            missing = [i for i in LINEAGE_INDEXES if i not in index_names(old_conn)]
            self.assertEqual(missing, [], f"旧库升级后缺索引：{missing}")
        finally:
            old_conn.close()


class LineageConstraintTest(_TempDb):
    """J-W1-3：约束真的在挡（且**是约束在挡**，不是别的原因）。"""

    @staticmethod
    def _ddl_without_main_check() -> str:
        """从**真实 DDL** 里剥掉 J-D4 那条 CHECK（连带注释与 `depth>=0` 后面的逗号）。

        为什么不手写一张"同样列但没有 CHECK"的表：手写的那张会与真实 DDL 漂开，
        这张对照就证明不了"是**这一条**约束在挡"。剥的时候必须把前一条 CHECK 的尾逗号
        收掉——否则 SQL 会报 `near ")"`，那是语法错不是约束缺席（实测过：第一次写
        就是这么红的）。
        """
        kept = [ln for ln in fish_lineage_schema._SESSION_LINEAGE_DDL.split("\n")
                if "origin <> 'main'" not in ln and "J-D4 做成" not in ln]
        return "\n".join(kept).replace("CHECK (depth >= 0),", "CHECK (depth >= 0)")

    def test_j_w1_3_main_with_parent_is_rejected(self):
        """J-D4 的库级兜底：主 + 有父 在库里不可能存在。"""
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO session_lineage (host, session_id, parent_session, origin, basis, "
                "source, observed_at) VALUES ('dsh','bad','p','main','header','dsh:header',?)",
                (_NOW,))

    def test_j_w1_3b_without_the_check_the_same_insert_succeeds(self):
        """**对照**：把那条 CHECK 从 DDL 里摘掉，同一 INSERT **必须成功**。

        没有这条对照，上面那条红可能只是"少了一列/类型不对"碰巧报错——
        那就证明不了"是 CHECK 在挡"。
        """
        ddl = self._ddl_without_main_check()
        self.assertNotIn("origin <> 'main'", ddl, "靶点未命中：DDL 里没有那条 CHECK")
        mem = sqlite3.connect(":memory:")
        try:
            mem.execute(ddl)
            mem.execute(
                "INSERT INTO session_lineage (host, session_id, parent_session, origin, basis, "
                "source, observed_at) VALUES ('dsh','bad','p','main','header','dsh:header',?)",
                (_NOW,))
            mem.commit()
            self.assertEqual(mem.execute(
                "SELECT COUNT(*) FROM session_lineage").fetchone()[0], 1,
                "无 CHECK 的表也插不进去——说明上一条红不是约束造成的")
        finally:
            mem.close()
        # 反证：本库（带 CHECK）同一 INSERT 确实被拒
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO session_lineage (host, session_id, parent_session, origin, basis, "
                "source, observed_at) VALUES ('dsh','bad2','p','main','header','dsh:header',?)",
                (_NOW,))

    def test_j_w1_3c_closed_sets_and_depth(self):
        """闭集不加词：`origin` / `relation` / `basis` / `depth` 四条 CHECK 逐条生效。"""
        base = ("host","session_id","parent_session","lineage_path","depth","depth_host",
                "origin","relation","agent_id","agent_role","agent_label","agent_path",
                "cwd","agent_preset","basis","source","extra","observed_at")
        good = ("dsh", "ok-1", None, "", 0, None, "main", None, None, None, None, None,
                None, None, "header", "dsh:header", None, _NOW)
        self.conn.execute(
            f"INSERT INTO session_lineage ({','.join(base)}) VALUES "
            f"({','.join('?' * len(base))})", good)
        # 合法：relation 为 NULL（根），关系闭集允许 NULL
        self.assertEqual(self.conn.execute(
            "SELECT relation FROM session_lineage WHERE session_id='ok-1'").fetchone()[0], None)
        bad_rows = {
            "origin": ("dsh", "bad-origin", None, "", 0, None, "weird", None, None, None,
                       None, None, None, None, "header", "s", None, _NOW),
            "relation": ("dsh", "bad-rel", "p", "/p/x", 1, None, "subagent", "wat", None,
                         None, None, None, None, None, "header", "s", None, _NOW),
            "basis": ("dsh", "bad-basis", None, "", 0, None, "main", None, None, None, None,
                      None, None, None, "wat", "s", None, _NOW),
            "depth": ("dsh", "bad-depth", "p", "/p/x", -1, None, "subagent", "spawn", None,
                      None, None, None, None, None, "header", "s", None, _NOW),
        }
        for column, row in bad_rows.items():
            with self.assertRaises(sqlite3.IntegrityError,
                                   msg=f"{column} 的 CHECK 没生效：{row}"):
                self.conn.execute(
                    f"INSERT INTO session_lineage ({','.join(base)}) VALUES "
                    f"({','.join('?' * len(base))})", row)

    def test_j_w1_3d_identity_checks(self):
        """`session_identity` 的三条 CHECK + 主键唯一（N 个会话键 → 1 个公民）。"""
        ok = ("dsh:s-1", "cit-1", None, "direct", "machine", _NOW, _NOW)
        self.conn.execute(
            "INSERT INTO session_identity (session_key, citizen_id, inherited_from, origin, "
            "evidence, created_at, observed_at) VALUES (?,?,?,?,?,?,?)", ok)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO session_identity (session_key, citizen_id, inherited_from, origin, "
                "evidence, created_at, observed_at) VALUES ('dsh:s-1','cit-2',NULL,'direct',"
                "'machine',?,?)", (_NOW, _NOW))
        bad = {
            "inherited 缺祖先": ("dsh:s-2", "cit-1", None, "inherited", "inherited", _NOW, _NOW),
            "direct 带祖先": ("dsh:s-3", "cit-1", "dsh:s-1", "direct", "machine", _NOW, _NOW),
            "provisional 空公民": ("dsh:s-4", "", None, "provisional", "provisional", _NOW, _NOW),
        }
        for why, row in bad.items():
            with self.assertRaises(sqlite3.IntegrityError, msg=f"CHECK 没生效：{why}"):
                self.conn.execute(
                    "INSERT INTO session_identity (session_key, citizen_id, inherited_from, "
                    "origin, evidence, created_at, observed_at) VALUES (?,?,?,?,?,?,?)", row)


class LineageIdempotencyTest(_TempDb):
    """J-W1-4：`migrate` 连跑两次幂等。"""

    def test_j_w1_4_migrate_is_idempotent(self):
        """第二次不报错、表仍 0 行、库结构快照一字不变（变异 M3 去掉 IF NOT EXISTS → 红）。"""
        fish.migrate(self.conn)
        snapshot_1 = _structure_snapshot(self.conn)
        fish.migrate(self.conn)              # 不许抛
        self.assertEqual(_structure_snapshot(self.conn), snapshot_1,
                         "第二次 migrate 改动了库结构（重建/删除）")
        assert_lineage_empty(self, self.conn, "【两次 migrate 之后】")


class LineageContractTest(_TempDb):
    """J-W1-6：`NODE_FIELDS` / DDL / 索引 三处契约对齐。"""

    def test_j_w1_6_indexes_and_column_contract(self):
        missing = [i for i in LINEAGE_INDEXES if i not in index_names(self.conn)]
        self.assertEqual(missing, [], f"缺索引：{missing}")
        # W1b：`fish_schema` 里的名字必须与叶子**同一对象**（copy 一份就各走各的）
        self.assertIs(fish_schema._LINEAGE_SCHEMA, fish_lineage_schema._LINEAGE_SCHEMA,
                      "fish_schema 的别名与叶子不是同一对象——两份拷贝迟早漂开")
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(session_lineage)")}
        self.assertEqual(cols, set(fish_lineage.NODE_FIELDS) | {"retracted_at"},
                         "`NODE_FIELDS` 与 DDL 列集合不一致（两侧必须逐列对应）")
        pk = [r[1] for r in sorted(
            (r for r in self.conn.execute("PRAGMA table_info(session_lineage)") if r[5]),
            key=lambda r: r[5])]
        self.assertEqual(tuple(pk), ("host", "session_id"),
                         "主键必须是 (host, session_id)——换序会让 UPSERT 命中错行")
        info = {r[1]: r for r in self.conn.execute("PRAGMA table_info(session_lineage)")}
        self.assertEqual((info["lineage_path"][3], info["lineage_path"][4]), (1, "''"),
                         "lineage_path 必须 NOT NULL DEFAULT ''")
        self.assertEqual((info["depth"][3], info["depth"][4]), (1, "0"),
                         "depth 必须 NOT NULL DEFAULT 0")
        pk_identity = [r[1] for r in sorted(
            (r for r in self.conn.execute("PRAGMA table_info(session_identity)") if r[5]),
            key=lambda r: r[5])]
        self.assertEqual(tuple(pk_identity), ("session_key",))
        parent_idx = [r[2] for r in self.conn.execute("PRAGMA index_info(idx_lineage_parent)")]
        self.assertEqual(tuple(parent_idx), ("host", "parent_session"),
                         "idx_lineage_parent 的列序必须与查询 `WHERE host=? AND parent_session=?` 一致")


class UpsertNodeTest(_TempDb):
    """`upsert_node`：全仓唯一的投影写口（W2/W3/W6 都只经它写）。"""

    def test_upsert_node_writes_and_overwrites(self):
        fish_lineage.upsert_node(self.conn, node())
        self.assertEqual(row_counts(self.conn), {"session_lineage": 1, "session_identity": 0})
        # 同键再写：**整行覆盖**（不是插第二行），observed_at 跟着更新
        fish_lineage.upsert_node(self.conn, node(basis="conflict", observed_at="2026-11-26T00:00:00Z"))
        self.assertEqual(row_counts(self.conn)["session_lineage"], 1)
        row = self.conn.execute(
            "SELECT basis, observed_at, agent_role FROM session_lineage "
            "WHERE host='dsh' AND session_id='s-1'").fetchone()
        self.assertEqual(row, ("conflict", "2026-11-26T00:00:00Z", "dafish-team"))

    def test_upsert_node_defaults_and_json_extra(self):
        """省略 `lineage_path`/`depth` 取表 DEFAULT；`extra` 传 dict 自动转 JSON 文本。"""
        n = node(lineage_path=None, depth=None, extra={"path_id_mismatch": 1, "b": 2})
        del n["lineage_path"]                # 真·省略，而不是给 None
        del n["depth"]
        fish_lineage.upsert_node(self.conn, n)
        row = self.conn.execute(
            "SELECT lineage_path, depth, extra FROM session_lineage").fetchone()
        self.assertEqual((row[0], row[1]), ("", 0))
        self.assertEqual(row[2], '{"b": 2, "path_id_mismatch": 1}',
                         "extra 必须是规范 JSON（键序固定，便于逐字节对拍）")

    def test_upsert_node_rejects_bad_input(self):
        """拼错的键与缺必填列都要**响亮**失败（静默忽略 = 写一行缺值而没人知道）。"""
        with self.assertRaises(ValueError):
            fish_lineage.upsert_node(self.conn, node(session_key="typo"))   # 未知字段
        n = node()
        del n["origin"]
        with self.assertRaises(ValueError):
            fish_lineage.upsert_node(self.conn, n)                          # 缺必填
        with self.assertRaises(sqlite3.IntegrityError):
            fish_lineage.upsert_node(self.conn, node(origin="main"))        # 撞 J-D4 的 CHECK
        self.assertEqual(row_counts(self.conn)["session_lineage"], 0,
                         "被拒的写不许留下半行")


class ShellTest(unittest.TestCase):
    """W1 的"空壳"必须是**响的**：返回假值的空壳会把 W2/W6 的判据变成恒绿。

    W2 落地 `recompute_depth`（113 §3.3）之后，**空壳名单只剩 `detect_cycles`（W6）
    与两个宿主适配器**——所以这里把"必须还响"的断言收窄到它们，并新增一条**反向**护栏：
    `recompute_depth` 必须真能跑（在空库上返回 0 行 0 改动）。少了这条反向断言，有人把它
    改回 `raise` 时 J-D1（四层链 0/1/2/3）会整条变绿，而本文件不会有任何反应。
    """

    def test_shells_fail_loudly(self):
        with self.assertRaises(NotImplementedError):
            fish_lineage.detect_cycles(None, "dsh")
        # 反向护栏：W2 已填，它**不许**再是空壳
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(fish.SCHEMA)
            stats = fish_lineage.recompute_depth(conn, "dsh")
        finally:
            conn.close()
        self.assertEqual((stats["rows"], stats["updated"], stats["conflicts"]), (0, 0, 0),
                         "空库上的重算应当报 0 行 / 0 改动 / 0 冲突")
        self.assertEqual(sorted(fish_lineage.ADAPTERS), ["dsh", "workbuddy"])
        for host, adapter in fish_lineage.ADAPTERS.items():
            self.assertEqual(adapter.host, host, "适配器的 host 与注册表键不一致")
            with self.assertRaises(NotImplementedError):
                adapter.resolve("s-1")
            with self.assertRaises(NotImplementedError):
                list(adapter.scan())

    def test_identity_key_of_strips_role_once(self):
        """唯一的 `#role` 剥离点（113 §1-5）：只剥第一段，其余原样。"""
        self.assertEqual(fish_lineage.identity_key_of("dsh:s-1#engineer"), "dsh:s-1")
        self.assertEqual(fish_lineage.identity_key_of("dsh:s-1"), "dsh:s-1")
        self.assertEqual(fish_lineage.identity_key_of(""), "")
        self.assertEqual(fish_lineage.identity_key_of(None), "")
        self.assertEqual(fish_lineage.identity_key_of("a#b#c"), "a")
        self.assertEqual(len(fish_lineage.NODE_FIELDS), 18)
        self.assertIn("session_lineage", fish.SCHEMA, "SCHEMA 没拼上会话树 DDL")
        self.assertIn("session_identity", fish.SCHEMA, "SCHEMA 没拼上身份投影 DDL")


class LineageLayeringTest(unittest.TestCase):
    """§2.4 的分层护栏：`fish_lineage` **只许 stdlib**（最多 `fish_common`）。

    为什么现在就上钉子：W2/W3 会把宿主适配器接进来（`fish_lineage_dsh` /
    `fish_lineage_wb`），它们 import `fish_corpus_*`，而那些模块最终 import `fish`。
    本模块一旦反向依赖任何 fish 上层模块，`import fish_db`/`fish_schema` 就会顺着
    这条链把 `fish` 拖进来 —— 循环导入的症状不是报错，而是"某一侧拿到半初始化模块"，
    在随机位置以 `AttributeError` 出现。这条判据把这个方向钉死在 W1。
    """

    def test_no_fish_business_imports(self):
        import ast
        tree = ast.parse((HERE / "fish_lineage.py").read_text(encoding="utf-8"))
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods.add(node.module.split(".")[0])
        # W1b：白名单里只许多出**更底层**的叶子（DDL 常量，零 import）。
        allowed = {"fish_common", "fish_lineage_schema"}
        upstream = sorted(m for m in mods if m.startswith("fish") and m not in allowed)
        self.assertEqual(upstream, [],
                         f"fish_lineage 依赖了上层模块 {upstream}（W2 起会成环）")
        self.assertTrue(mods & {"sqlite3", "json"}, "模块应当只用 stdlib（实测没看到）")
        # 白名单不是后门：叶子必须**真的**零 import，否则这个例外就站不住
        leaf = ast.parse((HERE / "fish_lineage_schema.py").read_text(encoding="utf-8"))
        leaf_mods = {a.name.split(".")[0] for n in ast.walk(leaf)
                     if isinstance(n, ast.Import) for a in n.names}
        leaf_mods |= {n.module.split(".")[0] for n in ast.walk(leaf)
                      if isinstance(n, ast.ImportFrom) and n.module}
        self.assertEqual(sorted(leaf_mods), [],
                         f"fish_lineage_schema 不是纯常量叶子，import 了 {sorted(leaf_mods)}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
