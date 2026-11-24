#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话树 W2 · DSH 适配器 + depth 重算验收（113 §3.4，2026-11-25）。

## 这一波验收什么

W2 的承诺是三件事（113 §3）：把 DSH 会话头**只读地**翻译成投影节点、六条判据定
origin/relation/basis、`recompute_depth` 从链上重算深度。所以判据分三层：

  ① **真实数据**：本机 789 个真实会话头扫描 → 建临时库 → 跑 J2 / J8 / J-D4 / J-W2-X，
     再对深度做一次**独立实现**的对拍（Python 里另写一遍向上走，不看 SQL）；
  ② **构造数据**：J-D1 的四层链（宿主 `delegationDepth` 全写 0 也要算出 0/1/2/3）、
     `depth_host` 不一致 → `conflict`、孤儿、环（守卫）；
  ③ **契约**：`read_header` 只解首帧、失败要列名、适配器**没有** sqlite3（不写库）、
     顶层 import 不许碰业务模块。

## 判据锚什么、不锚什么

锚 **"例外为 0 + 失败集合为空"**（113 §3.4 的 J-W2-X）：

  · `origin='subagent' 且无父` = 0；
  · `relation='fork' 且 origin='subagent'` = 0；
  · `origin='main' 且 parent_session 非空` = 0（库级 CHECK 的兜底）；
  · `basis` 闭集外的行 = 0；扫描失败清单 = `[]`。

**不锚 782 / 763 / 16 这类绝对数**：会话树是活数据，跑一个子代理就 +1（113 §0.3 记着
"同一小时内二次扫描读到 770 个头"）。数字只**打印**，断言一律走"与独立统计相等"或
"例外为 0"。fork = 16 是 2026-11-25 的读数，本文件把它当**输出**而不是判据。

## 每条判据都配了变异（`mutation_lineage_dsh.py`）

纪律同 W1：判据要先证明它在坏输入下会红。本文件把"共用判据"抽成模块级函数
（`assert_projection_invariants` / `expected_depth`），变异脚本调**同一份**——两份拷贝
迟早各自漂开，那时"变异红了"证明的是变异脚本坏了，不是判据有牙。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish                        # noqa: E402
import fish_corpus_dsh             # noqa: E402  —— FRAME_NAME 的唯一来源（对拍用）
import fish_lineage                # noqa: E402
import fish_lineage_dsh as dsh     # noqa: E402  —— 本波主角（叶子适配器）

try:
    import zstandard
except ImportError:                # pragma: no cover —— 缺依赖时下面会显式跳过
    zstandard = None

#: 固定时间戳：`observed_at` / `created_at` 全部钉死，读数才可复现
NOW = "2026-11-25T00:00:00Z"
#: 真实 DSH 会话头的 createdAt（毫秒）取一个固定值，`since` 判据要用
CREATED_MS = 1791167529348
CREATED_ISO = "2026-10-05T02:32:09Z"


# ---------------------------------------------------------------- 共用判据（变异脚本会 import 它们）
def assert_projection_invariants(case: unittest.TestCase, conn,
                                 where: str = "", allowed_basis=("header", "inferred")) -> None:
    """J2 / J8 / J-D4 / J-W2-X：**全部锚"例外为 0"**，一条都不锚绝对数。

    为什么值得抽成函数：变异脚本（`mutation_lineage_dsh.py` 的 D0）要在"改坏一个会话头"
    之后**用同一份判据**验红。两份拷贝各改各的，就会出现"变异红了但其实判据已经换成另一条"。
    """
    checks = (
        ("J2：`origin='subagent'` 的行必须有父",
         "SELECT COUNT(*) FROM session_lineage WHERE host='dsh' "
         "AND origin='subagent' AND parent_session IS NULL"),
        ("J8：`relation='fork'` 与 `origin='subagent'` 不许同时成立",
         "SELECT COUNT(*) FROM session_lineage WHERE host='dsh' "
         "AND relation='fork' AND origin='subagent'"),
        ("J-D4 库级兜底：`origin='main'` 的行不许有父",
         "SELECT COUNT(*) FROM session_lineage WHERE host='dsh' "
         "AND origin='main' AND parent_session IS NOT NULL"),
        ("J-W2-X：`basis` 落在约定的集合之外",
         "SELECT COUNT(*) FROM session_lineage WHERE host='dsh' AND basis NOT IN (%s)"
         % ",".join("'%s'" % b for b in allowed_basis)),
    )
    for why, sql in checks:
        got = conn.execute(sql).fetchone()[0]
        case.assertEqual(got, 0, "%s%s = %d，要求 0" % (where, why, got))


def parents_of(conn) -> dict:
    """`session_id → parent_session`（深度独立实现对拍用的最小输入）。"""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT session_id, parent_session FROM session_lineage WHERE host='dsh'")}


def relations_of(conn) -> dict:
    """`session_id → relation`：深度对拍必须知道**哪条边是 fork 种子边**（不是嵌套）。"""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT session_id, relation FROM session_lineage WHERE host='dsh'")}


def expected_depth(session_id: str, parents: dict, relations: dict):
    """**独立实现**的深度：在 Python 里向上走父链，返回 `(depth, kind)`。

    kind ∈ `root`（走到没父）/ `seed`（撞上 fork 种子边，fork 自己是新的根）/ `orphan`
    （父指针指向不存在的会话）/ `cycle`（`instr` 守卫挡住）。

    与 `fish_lineage.recompute_depth` 的 SQL 是两套代码、同一个语义——SQL 写错（`u.d+1`
    写成 `u.d`、`instr` 参数写反、漏掉 fork 那条停止条件）时这层对拍才会红。孤儿也把父
    指针算一层：宿主写了 `parentSession` 这个事实，就该在深度里体现出来，缺失的是**父那
    一行**，不是这条边。
    """
    depth, cur, seen = 0, session_id, {session_id}
    while True:
        parent = parents.get(cur)
        if parent is None:
            return depth, "root"
        if relations.get(cur) == "fork":
            return depth, "seed"
        if parent in seen:
            return depth, "cycle"
        depth += 1
        if parent not in parents:
            return depth, "orphan"
        cur = parent
        seen.add(parent)


def tally_headers(headers) -> dict:
    """**独立于 `classify`** 的桶统计：照着 113 §3.2 的表格条件直接数一遍。

    它和被测代码是两份表达式、同一张表：`and` 写成 `or`、`== 0` 写成 `>= 0` 这类改动
    会让两边对不上（这就是变异 M2 的靶子）。**不锚具体数字**——数出来的数只用于对拍。
    """
    out = {"fork": 0, "subagent": 0, "conflict": 0, "seeded_root": 0, "root": 0, "other": 0}
    for header in headers:
        parent = header.get("parent_session") or None
        depth = header.get("delegation_depth")
        seeded = header.get("is_seeded") is True
        origin = header.get("origin") or ""
        if seeded and parent and depth == 0:
            out["fork"] += 1
        elif origin == "subagent" and parent:
            out["subagent"] += 1
        elif parent and not origin and not seeded:
            out["conflict"] += 1
        elif not parent and seeded:
            out["seeded_root"] += 1
        elif not parent and not origin and depth == 0:
            out["root"] += 1
        else:
            out["other"] += 1
    return out


# ---------------------------------------------------------------- 夹具：造**真实形态**的会话文件
def frame_bytes(events) -> bytes:
    """把若干事件压成**一个 zstd 帧**（真实 DSH 文件就是"多帧串联"，首帧多行 JSONL）。"""
    body = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
    return zstandard.ZstdCompressor().compress(body.encode("utf-8"))


def session_event(session_id: str, **over) -> dict:
    """一个 `session` 事件（字段名照真实样本：`id` / `parentSession` / `isSeeded` …）。"""
    event = {"type": "session", "version": 4, "id": session_id, "createdAt": CREATED_MS,
             "cwd": "I:\\w", "agentPreset": "dafish-team"}
    event.update(over)
    return event


def write_session(root, session_id: str, *, workspace="--ws--", name=None,
                  extra_lines=(), event=None, **event_over):
    """在 `<root>/<workspace>/<session_id>/session.v4.jsonl.zstd` 写一个会话文件。

    `**event_over` 直接并进 `session` 事件（写判据时最顺手：`write_session(root, "f",
    isSeeded=True, parentSession="p", delegationDepth=0)`）。
    """
    path = Path(root) / workspace / session_id / (name or dsh.FRAME_NAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    head = event if event is not None else session_event(session_id, **event_over)
    path.write_bytes(frame_bytes([head, *extra_lines]))
    return path


class _TempDb(unittest.TestCase):
    """每个判据一个临时库（**绝不碰生产 `p0/data`**，conftest 的护栏也盯着这条）。"""

    def setUp(self):
        if zstandard is None:                       # pragma: no cover
            self.skipTest("缺 zstandard 依赖：DSH 适配器无从取证")
        self._tmp = tempfile.mkdtemp(prefix="fish-w2-")
        self.root = Path(self._tmp) / "data"
        self.root.mkdir(parents=True)
        self.frames = Path(self._tmp) / "sessions"
        self.frames.mkdir()
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:      # noqa: BLE001
            pass
        shutil.rmtree(self._tmp, ignore_errors=True)

    # -- 投影（写只走 upsert_node，适配器自己不碰库）
    def project(self, nodes) -> int:
        for node in nodes:
            fish_lineage.upsert_node(self.conn, node)
        self.conn.commit()
        return len(nodes)

    def rows(self) -> dict:
        """`session_id → (origin, relation, basis, depth, lineage_path, depth_host, extra)`。"""
        return {r[0]: r[1:] for r in self.conn.execute(
            "SELECT session_id, origin, relation, basis, depth, lineage_path, depth_host, extra "
            "  FROM session_lineage WHERE host='dsh'")}

    def insert(self, session_id, parent=None, *, depth_host=None, origin=None,
               relation="spawn", basis="header", source="dsh:header", extra=None):
        """直接经 `upsert_node` 插一行（不走适配器：判据要能单独测重算）。"""
        origin = origin or ("main" if parent is None else "subagent")
        fish_lineage.upsert_node(self.conn, {
            "host": "dsh", "session_id": session_id, "parent_session": parent,
            "depth_host": depth_host, "origin": origin,
            "relation": relation if parent is not None else None,
            "agent_id": None, "agent_role": "dafish-team", "agent_label": None,
            "agent_path": None, "cwd": "I:\\w", "agent_preset": "dafish-team",
            "basis": basis, "source": source, "extra": extra, "observed_at": NOW})
        self.conn.commit()


# ---------------------------------------------------------------- ① 读头与 classify


class HeaderTest(_TempDb):
    """`read_header`：只解首帧、字段取宿主原话、失败要响。"""

    def test_read_header_parses_session_event(self):
        path = write_session(self.frames, "s-1", parentSession="p-1", isSeeded=False,
                             delegationDepth=1, agentPreset="dafish-team",
                             origin="subagent", cwd="I:\\w2")
        header = dsh.read_header(path)
        self.assertEqual(header["session_id"], "s-1")
        self.assertEqual(header["parent_session"], "p-1")
        self.assertEqual(header["delegation_depth"], 1)
        self.assertEqual(header["agent_preset"], "dafish-team")
        self.assertEqual(header["origin"], "subagent")
        self.assertIs(header["is_seeded"], False)
        self.assertEqual(header["cwd"], "I:\\w2")
        self.assertEqual(header["created_at"], CREATED_ISO)
        self.assertNotIn("path_id_mismatch", header)

    def test_read_header_only_needs_first_frame(self):
        """首帧之后的事件（哪怕整份文件后面全是别的）不影响读数：只解首帧是 113 §3.2 的明文要求。"""
        path = write_session(self.frames, "s-2",
                             extra_lines=[{"type": "turn/start", "data": {}},
                                          {"type": "tool/call", "data": {}}])
        header = dsh.read_header(path)
        self.assertEqual(header["session_id"], "s-2")

    def test_read_header_rejects_frame_without_session_event(self):
        path = Path(self.frames) / "--ws--" / "s-3" / dsh.FRAME_NAME
        path.parent.mkdir(parents=True)
        path.write_bytes(frame_bytes([{"type": "turn/start", "data": {}}]))
        with self.assertRaises(dsh.HeaderError) as ctx:
            dsh.read_header(path)
        self.assertEqual(ctx.exception.reason_code, "no_session_event")

    def test_read_header_rejects_broken_frame(self):
        path = Path(self.frames) / "--ws--" / "s-4" / dsh.FRAME_NAME
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\x28\xb5\x2f\xfd" + b"\x00" * 64)      # zstd 魔数 + 垃圾
        with self.assertRaises(dsh.HeaderError) as ctx:
            dsh.read_header(path)
        self.assertEqual(ctx.exception.reason_code, "bad_frame")

    def test_read_header_reports_path_id_mismatch(self):
        """目录名 ≠ 头部 id：以**头部**为准并留痕（与 `fish_corpus_dsh._header_of` 同口径）。"""
        path = write_session(self.frames, "dir-name", event=session_event("real-id"))
        header = dsh.read_header(path)
        self.assertEqual(header["session_id"], "real-id")
        self.assertEqual(header["path_session_id"], "dir-name")
        self.assertEqual(header["path_id_mismatch"], 1)

    def test_frame_name_matches_corpus_layer(self):
        """发现逻辑委托语料层：文件名常量必须是**同一个值**（两份必然漂）。"""
        self.assertEqual(dsh.FRAME_NAME, fish_corpus_dsh.FRAME_NAME_V4)
        self.assertEqual(dsh.HOST, fish_corpus_dsh.HOST)


class ClassifyTest(_TempDb):
    """六条判据（113 §3.2）：**顺序即优先级**，逐条钉死产出。"""

    def classify(self, **header):
        base = {"session_id": "s", "parent_session": None, "origin": "", "is_seeded": None,
                "delegation_depth": None, "agent_preset": None, "cwd": None}
        base.update(header)
        return dsh.classify(base)

    def test_rule1_fork_requires_all_three(self):
        got = self.classify(parent_session="p", is_seeded=True, delegation_depth=0)
        self.assertEqual((got["origin"], got["relation"], got["basis"], got["rule"]),
                         ("fork", "fork", "header", "fork"))
        # 三条里缺一条都不是 fork：seed 为真但 depth=1 → 判据 2（spawn）
        got = self.classify(parent_session="p", is_seeded=True, delegation_depth=1,
                            origin="subagent")
        self.assertEqual((got["origin"], got["relation"]), ("subagent", "spawn"),
                         "seed+depth=1 落 spawn：判据 1 要求 depth==0（实测 4 行）")
        # 三条里缺一条都不是 fork：seed 为真但没有父 → 判据 4（root）
        got = self.classify(parent_session=None, is_seeded=True, delegation_depth=0)
        self.assertEqual((got["origin"], got["basis"]), ("main", "header"),
                         "seed 与 parent 脱钩 → 根，不许标 fork（实测 8 行）")

    def test_rule2_subagent_spawn(self):
        got = self.classify(parent_session="p", origin="subagent", delegation_depth=1)
        self.assertEqual((got["origin"], got["relation"], got["basis"]),
                         ("subagent", "spawn", "header"))
        self.assertEqual(got["source"], dsh.SOURCE_HEADER)

    def test_rule3_conflict_is_unknown_inferred_not_main(self):
        """矛盾行（有父 + 无 origin + 非 seed）→ `unknown` / `inferred` / `dsh:depth`。

        三件事必须同时成立，缺一条就出事故：
          · `origin='main'` 会撞库级 CHECK（J-D4），也会让这条会话冒充根；
          · `origin='subagent'` 是替宿主发言（它没说）；
          · `source` 不以 `:depth` 结尾，W6 的 J9 过滤就认不出这是推断出来的行。
        """
        got = self.classify(parent_session="p", origin="", is_seeded=False,
                            delegation_depth=1)
        self.assertEqual((got["origin"], got["relation"], got["basis"], got["rule"]),
                         ("unknown", "spawn", "inferred", "conflict"))
        self.assertEqual(got["source"], dsh.SOURCE_DEPTH)
        self.assertEqual(got["extra"]["infer_rule"], dsh.INFER_SELF_DEPTH)
        self.assertTrue(got["source"].endswith(":depth"))

    def test_rule4_and_rule5_are_roots(self):
        self.assertEqual(self.classify(is_seeded=True, delegation_depth=0)["rule"], "seeded-root")
        self.assertEqual(self.classify(origin="", delegation_depth=0)["rule"], "root")
        for header in ({"is_seeded": True, "delegation_depth": 0},
                       {"origin": "", "delegation_depth": 0}):
            got = self.classify(**header)
            self.assertEqual((got["origin"], got["relation"], got["basis"]),
                             ("main", None, "header"))

    def test_rule6_self_contradictions_are_corrected(self):
        """宿主自述自相矛盾的两种形状：**不许**撞 CHECK，也不许把 fork 写成没父。"""
        got = self.classify(parent_session="p", origin="main", delegation_depth=1)
        self.assertEqual((got["origin"], got["basis"], got["rule"]),
                         ("unknown", "inferred", "main-with-parent"))
        got = self.classify(parent_session=None, origin="fork", delegation_depth=0)
        self.assertEqual((got["origin"], got["basis"], got["rule"]),
                         ("unknown", "inferred", "fork-no-parent"))
        got = self.classify(parent_session="p", origin="weird", delegation_depth=1)
        self.assertEqual((got["origin"], got["basis"]), ("unknown", "inferred"),
                         "闭集外的 origin 不许硬塞进库")
        self.assertEqual(got["extra"]["infer_rule"], "origin:weird")

    def test_every_verdict_is_inside_the_closed_set(self):
        """穷举常见组合：`origin` 恒在闭集内、`basis` 恒在库的取值里、main 恒无父。"""
        combos = []
        for parent in (None, "p"):
            for origin in ("", "main", "subagent", "fork", "unknown", "??"):
                for seeded in (None, True, False):
                    for depth in (None, 0, 1, 2):
                        combos.append({"session_id": "s", "parent_session": parent,
                                       "origin": origin, "is_seeded": seeded,
                                       "delegation_depth": depth})
        for header in combos:
            got = dsh.classify(header)
            self.assertIn(got["origin"], dsh.ORIGINS, header)
            self.assertIn(got["basis"], ("header", "catalog", "db", "path", "runtime",
                                         "depth", "inferred", "missing", "conflict"), header)
            if got["origin"] == "main":
                self.assertIsNone(header["parent_session"],
                                  "main+父 会撞 CHECK（J-D4）：%r" % (header,))

    def test_orphan_parent_becomes_missing(self):
        """`known_ids` 不含父 → `unknown` + `basis='missing'`（110 §3.6.3 的孤儿诚实）。"""
        got = dsh.classify({"session_id": "s", "parent_session": "gone", "origin": "subagent",
                            "is_seeded": False, "delegation_depth": 1},
                           known_ids={"s"})
        self.assertEqual((got["origin"], got["basis"]), ("unknown", "missing"))
        self.assertEqual(got["extra"]["missing_parent"], 1)
        self.assertEqual(got["relation"], "spawn", "关系保留：它本该有父")


class NodeShapeTest(_TempDb):
    """`node_from_header`：适配器**不猜深度**、`agent_id` 恒 NULL。"""

    def test_node_shape(self):
        path = write_session(self.frames, "s-9", parentSession="p-9", isSeeded=False,
                             delegationDepth=2, origin="subagent")
        node = dsh.node_from_header(dsh.read_header(path), observed_at=NOW)
        self.assertEqual(node["host"], "dsh")
        self.assertEqual(node["session_id"], "s-9")
        self.assertEqual(node["parent_session"], "p-9")
        self.assertEqual(node["depth_host"], 2)
        self.assertEqual(node["origin"], "subagent")
        self.assertEqual(node["relation"], "spawn")
        self.assertEqual(node["agent_role"], "dafish-team")
        self.assertIsNone(node["agent_id"], "DSH 的 agent_id 恒 NULL")
        self.assertIsNone(node["agent_label"], "catalog 的 label 本期不填（113 §3.6）")
        self.assertNotIn("depth", node, "适配器不写 depth：那是 recompute_depth 的事")
        self.assertNotIn("lineage_path", node)
        self.assertEqual(node["observed_at"], NOW)

    def test_node_is_accepted_by_upsert_node(self):
        """形状契约：产出的 dict 必须能被唯一的写口接住（多一个键就会 ValueError）。"""
        path = write_session(self.frames, "s-8", delegationDepth=0)
        fish_lineage.upsert_node(self.conn, dsh.node_from_header(dsh.read_header(path)))
        self.conn.commit()
        self.assertEqual(self.rows()["s-8"][0], "main")


# ---------------------------------------------------------------- ② 适配器 scan / resolve


class ScanTest(_TempDb):
    """`scan`：委托语料层发现、失败列名、`since`/`limit` 只砍产出不砍判父。"""

    def build(self):
        write_session(self.frames, "root-1", delegationDepth=0)
        write_session(self.frames, "sub-1", parentSession="root-1", origin="subagent",
                      delegationDepth=1)
        write_session(self.frames, "fork-1", parentSession="root-1", isSeeded=True,
                      delegationDepth=0)

    def test_scan_report_counts_and_no_failures(self):
        self.build()
        adapter = dsh.DshLineageAdapter(roots=self.frames)
        nodes, report = adapter.scan_with_report()
        self.assertEqual(report["files"], 3)
        self.assertEqual(report["parsed"], 3)
        self.assertEqual(report["failed"], [], "失败清单必须为空（113 §3.4 的锚）")
        self.assertEqual(sorted(n["session_id"] for n in nodes), ["fork-1", "root-1", "sub-1"])
        self.assertEqual(report["rules"], {"root": 1, "subagent": 1, "fork": 1})
        # scan 是 scan_with_report 的薄壳：同一份数据、同一份报告
        again = dsh.DshLineageAdapter(roots=self.frames).scan()
        self.assertEqual(sorted(n["session_id"] for n in again),
                         sorted(n["session_id"] for n in nodes))

    def test_scan_names_broken_files(self):
        """坏文件**逐条列名**，且不许拖垮其余会话（"失败集合为空"必须有可读的反面）。"""
        self.build()
        broken = Path(self.frames) / "--ws--" / "broken-1" / dsh.FRAME_NAME
        broken.parent.mkdir(parents=True)
        broken.write_bytes(b"not a zstd frame at all")
        _nodes, report = dsh.DshLineageAdapter(roots=self.frames).scan_with_report()
        self.assertEqual(report["files"], 4)
        self.assertEqual(report["parsed"], 3, "parsed 只数解析成功的文件（不含失败）")
        self.assertEqual(report["unique"], 3)
        self.assertEqual([item["reason"] for item in report["failed"]], ["bad_frame"])
        self.assertIn("broken-1", report["failed"][0]["path"])

    def test_scan_since_filters_output_only(self):
        """`since` 只砍产出：被砍掉的父**仍然**参与"父在不在"的判定（否则误判孤儿）。"""
        write_session(self.frames, "old-root",
                      event=session_event("old-root", createdAt=1_600_000_000_000,
                                          delegationDepth=0))
        write_session(self.frames, "new-sub",
                      event=session_event("new-sub", createdAt=CREATED_MS,
                                          parentSession="old-root", origin="subagent",
                                          delegationDepth=1))
        adapter = dsh.DshLineageAdapter(roots=self.frames)
        nodes, report = adapter.scan_with_report(since="2026-01-01T00:00:00Z")
        self.assertEqual([n["session_id"] for n in nodes], ["new-sub"])
        self.assertEqual(report["skipped_since"], 1)
        self.assertEqual(nodes[0]["basis"], "header",
                         "父虽然被 since 砍掉，仍是「存在」的：since 只许砍产出")

    def test_scan_limit_reports_truncation(self):
        self.build()
        nodes, report = dsh.DshLineageAdapter(roots=self.frames).scan_with_report(limit=2)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(report["projected"], 2)
        self.assertEqual(report["truncated_by_limit"], 1)

    def test_resolve_and_missing(self):
        self.build()
        adapter = dsh.DshLineageAdapter(roots=self.frames)
        node = adapter.resolve("sub-1")
        self.assertIsNotNone(node)
        self.assertEqual(node["parent_session"], "root-1")
        self.assertIsNone(adapter.resolve("nope-1"), "判不到就 None，不许拿 main 冒充")
        self.assertIsNone(adapter.resolve(""))
        self.assertIsNone(adapter.resolve("../etc"), "非会话 id 形状不许拼路径")

    def test_resolve_uses_hint_path(self):
        path = write_session(self.frames, "s-7", delegationDepth=0)
        adapter = dsh.DshLineageAdapter(roots=Path(self._tmp) / "no-such-root")
        node = adapter.resolve("s-7", hint={"path": str(path)})
        self.assertEqual(node["session_id"], "s-7")

    def test_duplicate_files_for_same_session_are_deduped_and_named(self):
        """同一会话留了两份文件（版本迁移残留：实测 11 个目录）→ **只投影最新的一份**，
        其余进 `duplicate_ids` 清单。

        为什么这条是硬要求：主键是 `(host, session_id)`，两份文件都会 upsert 同一行，
        "谁赢"取决于目录遍历顺序——同一份数据两次扫描可能读出不同结果，而表面上完全正常。
        """
        legacy = write_session(self.frames, "dup-1", name="session.jsonl.zstd",
                               agentPreset="legacy-preset", delegationDepth=0)
        newest = write_session(self.frames, "dup-1", name=dsh.FRAME_NAME,
                               agentPreset="new-preset", delegationDepth=0)
        os.utime(legacy, (1, 1))                  # 保证 legacy 更旧（discover 按 mtime 倒序）
        self.assertGreater(newest.stat().st_mtime_ns, legacy.stat().st_mtime_ns)
        nodes, report = dsh.DshLineageAdapter(roots=self.frames).scan_with_report()
        self.assertEqual([n["session_id"] for n in nodes], ["dup-1"])
        self.assertEqual(nodes[0]["agent_preset"], "new-preset", "留下的必须是最新那份")
        self.assertEqual(len(report["duplicate_ids"]), 1)
        self.assertEqual(len(report["duplicate_ids"][0]["paths"]), 2)
        self.assertEqual(report["files"], 2)
        self.assertEqual(report["parsed"], 2)
        self.assertEqual(report["unique"], 1)

    def test_scan_does_not_touch_source_logs(self):
        """**只读证明**：扫描前后源日志的 (size, mtime_ns) 逐字节不变。"""
        self.build()
        snapshot = {p: (p.stat().st_size, p.stat().st_mtime_ns)
                    for p in Path(self.frames).rglob("session*.jsonl.zstd")}
        self.assertEqual(len(snapshot), 3)
        dsh.DshLineageAdapter(roots=self.frames).scan_with_report()
        after = {p: (p.stat().st_size, p.stat().st_mtime_ns)
                 for p in Path(self.frames).rglob("session*.jsonl.zstd")}
        self.assertEqual(after, snapshot, "扫描动了源日志——适配器只许读")


# ---------------------------------------------------------------- ③ depth 重算


class DepthRecomputeTest(_TempDb):
    """J-D1 / J-D4：**宿主值不作数**，深度只能从链上算出来。"""

    def test_j_d1_four_level_chain_ignores_host_depth(self):
        """A→B→C→D：宿主 `delegationDepth` 全写 0，重算仍要 0/1/2/3，且三行 conflict。

        为什么这行是 W2 的核心：省掉重算，J-D1 永远不会红，W6 的回填会把错的深度写进
        1,522 行语料（"错的"与"对的"在库里长得一样）。`conflicts == 3` 而不是 4：A 的宿主值
        本来就是 0，与重算结果相等——113 §3.4 那句"四行 conflict"是笔误，只有 3 行对不上。
        """
        for sid, parent in (("A", None), ("B", "A"), ("C", "B"), ("D", "C")):
            self.insert(sid, parent, depth_host=0)
        stats = fish_lineage.recompute_depth(self.conn, "dsh")
        rows = self.rows()
        self.assertEqual([rows[s][3] for s in "ABCD"], [0, 1, 2, 3])
        self.assertEqual([rows[s][4] for s in "ABCD"], ["", "/A", "/A/B", "/A/B/C"])
        self.assertEqual(stats["updated"], 3, "A 本来就在 0/''，不许虚报改动")
        self.assertEqual(stats["conflicts"], 3)
        self.assertEqual(sorted(stats["conflict_ids"]), ["B", "C", "D"])
        self.assertEqual(stats["max_depth"], 3)
        for sid in "BCD":
            self.assertEqual(rows[sid][2], "conflict", "%s 的 depth_host 与重算不等" % sid)
        self.assertEqual(rows["A"][2], "header", "A 对得上，不许被顺手标 conflict")

    def test_host_depth_too_high_also_conflicts(self):
        """反向：宿主把四层都写成 3，重算仍是 0/1/2/3，对不上的照样标 conflict。"""
        for sid, parent in (("A", None), ("B", "A"), ("C", "B"), ("D", "C")):
            self.insert(sid, parent, depth_host=3)
        stats = fish_lineage.recompute_depth(self.conn, "dsh")
        self.assertEqual([self.rows()[s][3] for s in "ABCD"], [0, 1, 2, 3])
        self.assertEqual(stats["conflicts"], 3)          # D 的 3 与重算的 3 相等
        self.assertEqual(self.rows()["D"][2], "header")

    def test_conflict_is_reversible(self):
        """`conflict` 不是单向标记：宿主值改对之后，`basis` 要**还原**成原来那个。"""
        self.insert("A", None, depth_host=0)
        self.insert("B", "A", depth_host=5)
        fish_lineage.recompute_depth(self.conn, "dsh")
        row = self.rows()["B"]
        self.assertEqual(row[2], "conflict")
        self.assertEqual(json.loads(row[6])["conflict_from"], "header")
        # 把宿主值改对（模拟"宿主日志被修正"）
        self.conn.execute("UPDATE session_lineage SET depth_host=1 WHERE session_id='B'")
        self.conn.commit()
        stats = fish_lineage.recompute_depth(self.conn, "dsh")
        row = self.rows()["B"]
        self.assertEqual(row[2], "header", "两值又对上了，basis 必须还原")
        self.assertNotIn("conflict_from", json.loads(row[6] or "{}"))
        self.assertEqual(stats["conflicts"], 0)

    def test_idempotent_second_run_changes_nothing(self):
        for sid, parent in (("A", None), ("B", "A"), ("C", "B")):
            self.insert(sid, parent, depth_host=None)
        first = fish_lineage.recompute_depth(self.conn, "dsh")
        self.assertGreater(first["updated"], 0, "第一次重算必须真的改动（否则统计是假的）")
        second = fish_lineage.recompute_depth(self.conn, "dsh")
        self.assertEqual(second["updated"], 0, "第二次不许再改：幂等是 upsert/重算的底线")
        self.assertEqual(second["rows"], first["rows"])

    def test_empty_host_reports_zeros_not_fake_success(self):
        stats = fish_lineage.recompute_depth(self.conn, "dsh")
        self.assertEqual((stats["rows"], stats["updated"], stats["conflicts"]), (0, 0, 0))
        self.assertEqual(stats["cycles"], [])
        self.assertEqual(stats["orphans"], [])

    def test_orphan_is_named_and_counted(self):
        """J-D4 的数据面：父指针指向不存在的会话 → 进 `orphans` 清单（不静默当根）。"""
        self.insert("X", "gone", depth_host=1)
        stats = fish_lineage.recompute_depth(self.conn, "dsh")
        self.assertEqual(stats["orphans"], ["X"])
        self.assertEqual(stats["cycles"], [])
        self.assertEqual(self.rows()["X"][3], 1, "父指针算一层：缺失的是父那一行，不是这条边")

    def test_cycle_is_cut_named_and_not_written(self):
        """A↔B：守卫切断递归（**不设超时就会无限递归**），两行**不写深度**、只列名。

        测试侧装了 SQLite 进度回调：万一守卫被删掉，这段代码会**被中断**（红），
        而不是把整台机器挂住——变异 M1 就是靠它才跑得下去。
        """
        self.insert("A", "B", depth_host=1)
        self.insert("B", "A", depth_host=1)
        self.conn.set_progress_handler(lambda: 1, 200000)   # 只给 20 万步：正常几行就收工
        try:
            stats = fish_lineage.recompute_depth(self.conn, "dsh")
        finally:
            self.conn.set_progress_handler(None, 0)
        self.assertEqual(sorted(stats["cycles"]), ["A", "B"])
        self.assertEqual(stats["orphans"], [])
        rows = self.rows()
        self.assertEqual((rows["A"][3], rows["B"][3]), (0, 0), "环上的行没有可用深度，不许编")
        self.assertEqual((rows["A"][4], rows["B"][4]), ("", ""))
        # 下游影响也被钉住：环上的行不许被当成正常树节点（它们的 basis 仍是原值）
        self.assertEqual((rows["A"][2], rows["B"][2]), ("header", "header"))

    def test_self_loop_is_named_as_cycle(self):
        """自环（A 的父是 A）与 A↔B 同一类：`instr` 守卫第一步就挡住，**不写深度**。"""
        self.insert("self-1", "self-1", depth_host=1)
        self.conn.set_progress_handler(lambda: 1, 200000)
        try:
            stats = fish_lineage.recompute_depth(self.conn, "dsh")
        finally:
            self.conn.set_progress_handler(None, 0)
        self.assertEqual(stats["cycles"], ["self-1"])
        self.assertEqual(self.rows()["self-1"][3], 0, "自环没有可用深度，不许写")

    def test_other_host_is_untouched(self):
        fish_lineage.upsert_node(self.conn, {
            "host": "workbuddy", "session_id": "wb-1", "parent_session": None,
            "depth_host": 0, "origin": "main", "relation": None, "agent_id": None,
            "agent_role": None, "agent_label": None, "agent_path": None, "cwd": None,
            "agent_preset": None, "basis": "header", "source": "wb:runtime",
            "extra": None, "observed_at": NOW})
        self.insert("A", None, depth_host=0)
        fish_lineage.recompute_depth(self.conn, "dsh")
        row = self.conn.execute(
            "SELECT depth, basis FROM session_lineage WHERE host='workbuddy'").fetchone()
        self.assertEqual(row, (0, "header"), "重算只动指定宿主：跨宿主串写会把 WB 的树算坏")


# ---------------------------------------------------------------- ④ 真实数据（本机只读）


class RealProjectionTest(_TempDb):
    """把**真实 DSH 会话头**投影到临时库，跑 113 §3.4 的判据。

    这是"例外为 0"那组锚的取证处：构造数据能证明判据在坏输入下会红，但只有真实数据
    能证明它**在今天的现场上**是绿的。数字随活着变化（跑一个子代理就 +1），所以这里
    **只打印数字、断言一律用"与独立统计相等"或"例外为 0"**。
    """

    nodes = None
    report = None
    live_root = None
    _headers = None

    @classmethod
    def setUpClass(cls):
        if zstandard is None:                     # pragma: no cover
            raise unittest.SkipTest("缺 zstandard 依赖：真实投影无从取证")
        from fish_corpus import default_roots     # 根目录的唯一来源（不写第二套）
        roots = default_roots("dsh")
        root = Path(roots[0]) if roots else None
        if root is None or not root.is_dir():
            raise unittest.SkipTest("本机没有 DSH 会话目录（%s）——真实投影判据无从取证" % root)
        cls.live_root = root
        adapter = dsh.DshLineageAdapter(roots=root)
        nodes, report = adapter.scan_with_report()
        if not nodes:
            raise unittest.SkipTest("DSH 会话目录里没有会话文件（%s）" % root)
        cls.nodes, cls.report = nodes, report
        # 会话头留一份（fork/矛盾行要拿**原始字段**独立再数一遍，不能拿节点反推）
        cls._headers = [dsh.read_header(item["path"])
                        for item in fish_corpus_dsh.discover([root])]
        print("\n  [W2] 真实会话头：文件 %d / 解析 %d / 失败 %d / 桶 %s"
              % (report["files"], report["parsed"], len(report["failed"]), report["rules"]))

    @classmethod
    def tearDownClass(cls):
        cls._headers = None

    def setUp(self):
        if self.__class__.report is None:         # pragma: no cover
            self.skipTest("真实投影未取证")
        self._tmp = tempfile.mkdtemp(prefix="fish-w2-real-")
        self.root = Path(self._tmp) / "data"
        self.root.mkdir(parents=True)
        self.frames = self.live_root
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        for node in self.nodes:                   # 写只走 upsert_node（适配器不碰库）
            fish_lineage.upsert_node(self.conn, node)
        self.conn.commit()
        self.stats = fish_lineage.recompute_depth(self.conn, "dsh")
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:      # noqa: BLE001
            pass
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_scan_covers_every_file_and_names_failures(self):
        report = self.report
        self.assertEqual(report["failed"], [],
                         "有会话头读不出来：%s" % [f["path"] for f in report["failed"]])
        self.assertEqual(report["files"], report["parsed"] + len(report["failed"]),
                         "有文件既没进 parsed 也没进 failed（逐份记账）")
        self.assertEqual(sum(report["rules"].values()), report["projected"],
                         "classify 的桶统计与投影个数对不上（有行没进判据）")
        # **多份文件的账要对上**：parsed = unique + 被去重掉的那几份
        dup_extra = sum(len(item["paths"]) - 1 for item in report["duplicate_ids"])
        self.assertEqual(report["parsed"], report["unique"] + dup_extra,
                         "同一会话的多份文件没有被逐份记账（去重不许静默丢）")
        for item in report["duplicate_ids"]:
            self.assertGreater(len(item["paths"]), 1)
            # 重复的一定是**同一个会话目录**里的多份帧文件（v4 / v3 / legacy）
            self.assertTrue(all(Path(p).parent.name == item["session_id"]
                                for p in item["paths"]),
                            "去重清单里的路径与会话 id 对不上：%r" % (item,))

    def test_j2_j8_and_j_w2_x_exceptions_are_zero(self):
        self.assertEqual(len(self.nodes), self.report["projected"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM session_lineage WHERE host='dsh'").fetchone()[0],
            self.report["projected"],
            "投影个数与库里的行数不等（同一会话被写了两遍？）")
        assert_projection_invariants(self, self.conn, "【真实投影】")

    def test_j8_fork_bucket_matches_independent_tally(self):
        """fork 行数 = **独立统计**的 fork 桶。数字只打印（2026-11-25 是 16 行）。"""
        tally = tally_headers(self.headers())
        got = self.conn.execute(
            "SELECT COUNT(*) FROM session_lineage WHERE host='dsh' AND relation='fork'"
        ).fetchone()[0]
        print("  [W2] fork 桶：库 %d / 独立统计 %d" % (got, tally["fork"]))
        self.assertEqual(got, tally["fork"], "fork 判据与 113 §3.2 的表格对不上")
        self.assertGreater(got, 0, "真实数据里一个 fork 都没有——判据可能读空了")
        forks = self.conn.execute(
            "SELECT session_id, depth_host FROM session_lineage WHERE host='dsh' "
            "AND relation='fork'").fetchall()
        self.assertEqual([f[1] for f in forks], [0] * len(forks),
                         "fork 的判据里 delegationDepth 必须是 0")

    def test_conflict_rows_land_inferred_not_main(self):
        """矛盾行（有父 / 无 origin / 非 seed）必须落 `unknown` + `inferred` + `dsh:depth`。"""
        tally = tally_headers(self.headers())
        rows = self.conn.execute(
            "SELECT session_id, origin, basis, source, extra FROM session_lineage "
            " WHERE host='dsh' AND basis='inferred'").fetchall()
        print("  [W2] 矛盾行：独立统计 %d / 库内 inferred %d" % (tally["conflict"], len(rows)))
        self.assertEqual(len(rows), tally["conflict"])
        for sid, origin, basis, source, extra in rows:
            self.assertEqual(origin, "unknown", "%s 落成了 %s（不许 main，也不许替宿主说话）"
                             % (sid, origin))
            self.assertEqual(basis, "inferred")
            self.assertTrue(source.endswith(":depth"), "%s 的 source=%r 不是推断口径" % (sid, source))
            self.assertEqual(json.loads(extra)["infer_rule"], dsh.INFER_SELF_DEPTH)

    def test_depth_matches_independent_implementation(self):
        """深度对拍：SQL 一套、Python 一套，两套必须逐行相等（差异即 bug）。"""
        parents = parents_of(self.conn)
        relations = relations_of(self.conn)
        mismatched, cycles, seeds = [], [], 0
        for sid in parents:
            depth, kind = expected_depth(sid, parents, relations)
            if kind == "cycle":
                cycles.append(sid)
                continue
            if kind == "seed":
                seeds += 1
            got = self.conn.execute(
                "SELECT depth FROM session_lineage WHERE host='dsh' AND session_id=?",
                (sid,)).fetchone()[0]
            if got != depth:
                mismatched.append((sid, got, depth))
        self.assertEqual(cycles, [], "真实数据里出现父链环：先查会话头被谁改过")
        self.assertEqual(mismatched, [], "SQL 重算与独立实现对不上（前 5 条：%r）"
                         % (mismatched[:5],))
        self.assertEqual(sorted(self.stats["orphans"]), [], "真实数据里出现孤儿父指针")
        self.assertGreater(seeds, 0, "真实数据里一条 fork 种子边都没有——fork 规则没被验证")
        print("  [W2] 深度：%d 行，最大深度 %d，冲突 %d（宿主值 ≠ 链上深度），种子边停 %d 行"
              % (self.stats["rows"], self.stats["max_depth"], self.stats["conflicts"], seeds))

    def headers(self):
        """本轮扫描的会话头副本（fork / 矛盾行的独立统计要拿**原始字段**再数一遍）。"""
        return list(self.__class__._headers or [])


# ---------------------------------------------------------------- ⑤ 契约：不写库、不依赖业务层


def _imports_of(tree) -> set:
    """AST 里出现过的**所有** import 顶层名（含函数内 import——那也算依赖）。"""
    import ast
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


class LayeringTest(unittest.TestCase):
    """§3.2 / §5.7 的依赖方向：适配器是**叶子**，不许 import 主库、不许碰 sqlite。"""

    def test_module_level_imports_are_leaf_safe(self):
        import ast
        tree = ast.parse((HERE / "fish_lineage_dsh.py").read_text(encoding="utf-8"))
        module_level = []
        for node in tree.body:                 # 只看**顶层**：函数内 import 是刻意的
            if isinstance(node, ast.Import):
                module_level.extend(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                module_level.append(node.module.split(".")[0])
        upstream = sorted({m for m in module_level
                           if m.startswith("fish") and m != "fish_common"})
        self.assertEqual(upstream, [], "顶层 import 了业务模块 %s（hook 路径会变重）" % upstream)
        self.assertNotIn("sqlite3", module_level, "适配器不许有数据库依赖（它只读日志）")
        self.assertIn("fish_common", module_level)

    def test_session_node_type_comes_from_type_checking(self):
        """`SessionNode` 只在 `TYPE_CHECKING` 里引：运行时它会把主库拖进来。"""
        source = (HERE / "fish_lineage_dsh.py").read_text(encoding="utf-8")
        self.assertIn("if TYPE_CHECKING:", source)
        import fish_lineage
        self.assertIsInstance(fish_lineage.SessionNode(), dict)   # 契约：Node 就是 dict

    def test_module_has_no_write_api(self):
        """**结构性只读**：模块里没有任何写文件的 API（真实日志由运行中的宿主自己在追加，
        拿快照证明"没动它"不可靠，所以这一侧用静态证据）。

        只允许 `open(path, "rb")` 这种只读打开；`Path.write_*` / `write_bytes` /
        `shutil` / `os.remove` 一个都不许出现。
        """
        import ast
        source = (HERE / "fish_lineage_dsh.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        banned_attrs = {"write_text", "write_bytes", "unlink", "rmdir", "mkdir", "rename",
                        "replace", "touch", "chmod", "write"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in banned_attrs:
                # `dobj.decompress` / `dobj.eof` 之类不算；只盯 Path/os 上的写方法
                raise AssertionError("模块里出现了写操作 `.{}`（行 %d）" % (node.attr, node.lineno))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "open":
                mode = node.args[1] if len(node.args) > 1 else None
                self.assertIsInstance(mode, ast.Constant, "open() 的 mode 必须是字面量")
                self.assertIn("b", str(mode.value), "open() 必须是二进制只读打开")
                self.assertNotIn("w", str(mode.value))
                self.assertNotIn("a", str(mode.value))
                self.assertNotIn("+", str(mode.value))
        for name in ("shutil", "tempfile", "sqlite3", "subprocess"):
            self.assertNotIn(name, _imports_of(tree),
                             "%s 不该出现在只读适配器里" % name)

    def test_adapter_api_has_no_connection_parameter(self):
        """**结构性不写库**：适配器的方法签名里根本没有连接（不是靠"我记得别写"）。"""
        import inspect
        for name in ("resolve", "scan", "scan_with_report", "find_path"):
            params = inspect.signature(getattr(dsh.DshLineageAdapter, name)).parameters
            self.assertNotIn("conn", params, "%s 收了连接：适配器写库就再也管不住了" % name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
