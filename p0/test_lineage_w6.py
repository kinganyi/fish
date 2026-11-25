#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话树 W6 · 历史回填与对账的判据（113 §10.2，2026-11-25）。

## 这一波验收什么

W6 把规则灌进历史数据，所以判据分四层，**每层都能红**：

  ① **回填本身**（`scan`）：4 层链 → depth 0/1/2/3；`dry_run=True` **生产库零写**、
     `dry_run=False` 才写且**先备份**；第二次 apply 幂等；父闭合把"扫描面之外的父"补进来
     （WB 的扫描面只有 `agent-*`，不补父就整棵树全是孤儿）。
  ② **三份清单**（`detect_cycles`）：A→B→A → 两行 `relation='cycle'` + `basis='conflict'`
     且**不参与继承**（`parent_of` 在环上返回 None）；孤儿 → `unknown/missing`、**不许不是
     main 却被改写成父**；`depth_host` 与重算不等 → `conflict` **逐条列名**。
  ③ **可观测性**（J-W6-C）：`format_report` 对**空清单**也必须打印"本次检测到 0 行"——
     "没打印"与"没检测"不可分，这是判据的判别力问题，不是样式。
  ④ **只读面**：`report()` 在 `mode=ro` 的连接上必须能跑完且**不写一个字节**
     （对账工具就是这么用的）。

## 为什么用桩适配器而不是扫真机

扫真机（810 份 DSH 会话头 / 2,100 份 WB transcript）读数是**活的**，而且慢（20s+）。
本文件的判据全部是"给定输入必须给出确定的树"，所以 `ADAPTERS` 在这里被换成**桩**：
输入完全可控，红/绿只取决于实现。真机读数由 `tools/reconcile_lineage.py` 负责
（它打印、不锚数），两者分工不重叠。

## 两个模块的分工（W6 落地时的一刀）

`detect_cycles` 在 `fish_lineage`（投影层本体），`scan` / `report` / 报告格式化在
`fish_lineage_backfill`（批量层）——后者要装影子库、`_backups` 快照与渲染，塞进
投影层会顶破 J4 的 700 行窗口，而仓库规矩是「先抽层再抬窗口」（先例 W1b）。
所以下面的判据直接打**两个模块**：回填/报告打批量层，检测/标环/继承打投影层。

## 每条判据都配了变异（`mutation_lineage_w6.py`）

纪律同 W1/W2/W5：判据要先在**坏代码**下红一次、还原后再绿一次。没有对照组红的变异
不算证据——那可能只是变异脚本自己坏了。
"""
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish                        # noqa: E402
import fish_lineage                # noqa: E402
import fish_lineage_backfill        # noqa: E402  —— W6 的批量/报告层（detect 在 fish_lineage）

#: 固定时刻：`observed_at` 全部钉死，读数才可复现
NOW = "2026-11-25T00:00:00Z"


def node(session_id, parent=None, *, host="dsh", depth_host=None, origin=None,
         relation=None, basis="header", source="dsh:header", cwd="I:\\w",
         extra=None, observed_at=NOW):
    """造一个适配器会吐出来的节点（字段与 `NODE_FIELDS` 对齐）。"""
    if origin is None:
        origin = "main" if parent is None else "subagent"
    return {
        "host": host, "session_id": session_id, "parent_session": parent,
        "depth_host": depth_host, "origin": origin,
        "relation": relation if relation is not None else ("spawn" if parent else None),
        "agent_id": None, "agent_role": "dafish-team" if parent else None,
        "agent_label": None, "agent_path": None, "cwd": cwd,
        "agent_preset": "dafish-team" if parent else None,
        "basis": basis, "source": source, "extra": extra, "observed_at": observed_at,
    }


class StubAdapter:
    """桩适配器：`scan` 吐给定节点，`resolve` 只认显式给的表（**判不到就 None**）。"""

    host = "dsh"

    def __init__(self, nodes=(), parents=None):
        self._nodes = list(nodes)
        self._parents = dict(parents or {})
        self.last_report = None
        self.resolved = []

    def scan(self, *, since=None, limit=None):
        nodes = [dict(n) for n in self._nodes]
        if since:
            nodes = [n for n in nodes if n["observed_at"] >= since]
        if limit is not None:
            nodes = nodes[:limit]
        return nodes

    def resolve(self, session_id, *, hint=None):
        self.resolved.append(session_id)
        found = self._parents.get(session_id)
        return dict(found) if found else None


class _TempDb(unittest.TestCase):
    """每个判据一个临时库（**绝不碰生产 `p0/data`**，conftest 的护栏也盯着这条）。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="fish-w6-")
        self.root = Path(self._tmp) / "data"
        self.root.mkdir(parents=True)
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        self._saved = dict(fish_lineage.ADAPTERS)

    def tearDown(self):
        fish_lineage.ADAPTERS.clear()
        fish_lineage.ADAPTERS.update(self._saved)
        try:
            self.conn.close()
        except Exception:          # noqa: BLE001
            pass
        shutil.rmtree(self._tmp, ignore_errors=True)

    # -- 装配
    def use_adapter(self, nodes=(), parents=None, host="dsh") -> StubAdapter:
        stub = StubAdapter(nodes, parents)
        stub.host = host
        fish_lineage.ADAPTERS[host] = stub
        return stub

    def insert(self, session_id, parent=None, *, host="dsh", depth_host=None,
               origin=None, relation="spawn", basis="header", cwd="I:\\w",
               extra=None):
        """直接经 `upsert_node` 插一行（不走适配器：判据要能单独测检测面）。"""
        fish_lineage.upsert_node(self.conn, node(
            session_id, parent, host=host, depth_host=depth_host, origin=origin,
            relation=relation if parent is not None else None, basis=basis,
            cwd=cwd, extra=extra))
        self.conn.commit()

    def rows(self, host="dsh") -> dict:
        """`session_id → (origin, relation, basis, depth, lineage_path, depth_host, extra)`。"""
        return {r[0]: r[1:] for r in self.conn.execute(
            "SELECT session_id, origin, relation, basis, depth, lineage_path, depth_host, extra "
            "  FROM session_lineage WHERE host=?", (host,))}

    def count(self, host="dsh") -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM session_lineage WHERE host=?", (host,)).fetchone()[0]


# ================================================================ ① 回填本身
class ScanBackfillTest(_TempDb):
    """`scan` 的承诺：批量 upsert → 重算深度 → 检测三清单；缺省 dry-run **零写**。"""

    def test_w6_four_level_chain_backfills_depths(self):
        """J-D1 的数据面：A→B→C→D 四层，宿主值全 0，回填后 depth 必须 0/1/2/3。

        这条是 W6 的核心：省掉 `recompute_depth`（或改成直接读宿主值），
        回填会把错的深度灌进 798 行历史，而"错的"与"对的"在库里长得一样。
        """
        self.use_adapter([node("A", None, depth_host=0), node("B", "A", depth_host=0),
                          node("C", "B", depth_host=0), node("D", "C", depth_host=0)])
        rep = fish_lineage_backfill.scan("dsh", conn=self.conn, dry_run=False)
        rows = self.rows()
        self.assertEqual([rows[sid][3] for sid in "ABCD"], [0, 1, 2, 3])
        self.assertEqual([rows[sid][4] for sid in "ABCD"], ["", "/A", "/A/B", "/A/B/C"])
        self.assertEqual(rep["rows_before"], 0)
        self.assertEqual(rep["rows_after"], 4)
        self.assertEqual(rep["delta"], 4)
        self.assertEqual(sorted(rep["conflicts"]), ["B", "C", "D"])
        self.assertEqual(rep["max_depth"], 3)
        self.assertEqual(sum(rep["depth_dist"].values()), 4)
        # 对不上的三行被标 conflict，A 宿主值本来就对，不许被顺手标
        self.assertEqual([rows[sid][2] for sid in "ABCD"],
                         ["header", "conflict", "conflict", "conflict"])

    def test_w6_dry_run_writes_nothing_and_apply_writes(self):
        """`scan(dry_run=True)`（缺省）生产库**零写**；`dry_run=False` 才写。

        判据写两半：① dry-run 前后行数不变（且报告里的 rows_after 是**影子库**的读数，
        说明它真的算过而不是没跑）；② apply 之后行数变化等于报告里的 delta。
        """
        self.use_adapter([node("A", None, depth_host=0), node("B", "A", depth_host=0)])
        dry = fish_lineage_backfill.scan("dsh", conn=self.conn)           # 缺省 dry-run
        self.assertTrue(dry["dry_run"])
        self.assertEqual(self.count(), 0, "dry-run 写了库：它只许在影子库里跑")
        self.assertEqual((dry["rows_before"], dry["rows_after"]), (0, 2),
                         "dry-run 也必须真的算出『会变成什么样』，否则它只是在数文件")
        self.assertIsNone(dry["backup"], "dry-run 没有写库，不该造备份文件")

        applied = fish_lineage_backfill.scan("dsh", conn=self.conn, dry_run=False)
        self.assertEqual(self.count(), 2)
        self.assertEqual(applied["rows_after"] - applied["rows_before"], applied["delta"])
        self.assertEqual(sorted(self.rows()), ["A", "B"])

    def test_w6_apply_backs_up_first(self):
        """生产写入必须先备份（113 §10.3 的回退完全依赖这份快照）。

        备份内容是**写之前**的库：拿它打开必须能看到 `session_lineage` 且 **0 行**。
        """
        self.use_adapter([node("A", None)])
        rep = fish_lineage_backfill.scan("dsh", conn=self.conn, dry_run=False)
        self.assertIsNotNone(rep["backup"], "apply 必须留快照（否则没有回滚点）")
        backup = Path(rep["backup"])
        self.assertTrue(backup.exists())
        self.assertEqual(backup.parent.name, "_backups")
        snap = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
        try:
            rows = snap.execute("SELECT COUNT(*) FROM session_lineage").fetchone()[0]
        finally:
            snap.close()
        self.assertEqual(rows, 0, "快照必须是写之前的库：它里面不许已经有回填行")

    def test_w6_second_apply_is_idempotent(self):
        """重复 apply：行数不变、`updated` 归零、三份清单不变（幂等是回填的底线）。"""
        self.use_adapter([node("A", None, depth_host=0), node("B", "A", depth_host=1)])
        first = fish_lineage_backfill.scan("dsh", conn=self.conn, dry_run=False)
        second = fish_lineage_backfill.scan("dsh", conn=self.conn, dry_run=False)
        self.assertEqual(self.count(), 2)
        self.assertEqual(second["delta"], 0)
        self.assertEqual(second["depth"]["updated"], 0, "第二次不许再改深度")
        self.assertEqual((second["cycles"], second["orphans"], second["conflicts"]),
                         (first["cycles"], first["orphans"], first["conflicts"]))
        self.assertEqual(second["marked"], [], "已标过的环不许重复标")

    def test_w6_parent_closure_brings_parents_outside_scan_surface(self):
        """父闭合：扫描面里没有的父，由适配器按 id 补进投影（否则 WB 整棵树全是孤儿）。

        为什么这条是 W6 的必要件：WB 的适配器（W3 契约）只枚举 `agent-*.jsonl`，
        父是**顶层会话**，一个都不在扫描结果里。不补父 → 2,100 行全落 orphan。
        """
        self.use_adapter(
            [node("k1", "p1", host="workbuddy"), node("k2", "p1", host="workbuddy")],
            parents={"p1": node("p1", None, host="workbuddy")}, host="workbuddy")
        rep = fish_lineage_backfill.scan("workbuddy", conn=self.conn, dry_run=False)
        rows = self.rows(host="workbuddy")
        self.assertEqual(sorted(rows), ["k1", "k2", "p1"], "父必须被补进投影")
        self.assertEqual(rep["parents_resolved"], 1)
        self.assertEqual(rep["parents_missing"], [])
        self.assertEqual(rep["orphans"], [], "补上父之后不该再报孤儿")
        self.assertEqual(rows["p1"][3], 0, "补进来的父是根：depth 0")
        self.assertEqual(rows["k1"][3], 1)

    def test_w6_orphan_parent_is_not_guessed_from_cwd(self):
        """回填**不许按 cwd 猜父**：判不到的父原样留着，落进 orphan 逐条列名。

        变异打的就是这一条：把同 cwd 的另一个父顶上去之后，孤儿会"消失"——
        那比报错更糟：树看起来完整了，而那条边是编的。
        """
        self.use_adapter(
            [node("P", None, cwd="I:\\shared"),
             node("B", "P", cwd="I:\\shared"),
             node("X", "gone", cwd="I:\\shared", origin="unknown", basis="missing")])
        rep = fish_lineage_backfill.scan("dsh", conn=self.conn, dry_run=False)
        rows = self.rows()
        self.assertEqual(rows["X"][0], "unknown", "孤儿不许被写成 main")
        self.assertEqual(rows["X"][2], "missing", "孤儿必须是 unknown/missing")
        self.assertEqual(self.conn.execute(
            "SELECT parent_session FROM session_lineage WHERE session_id='X'").fetchone()[0],
            "gone", "父指针被改写了：按 cwd 猜父 = 编一条边")
        self.assertEqual(rep["orphans"], ["X"])
        self.assertEqual(rep["parents_missing"], ["gone"])

    def test_w6_unknown_host_fails_loudly(self):
        """未登记的宿主 → `KeyError`（**不猜、不回退到 dsh**）。"""
        with self.assertRaises(KeyError):
            fish_lineage_backfill.scan("nope", conn=self.conn)


# ================================================================ ② 三份清单
class DetectCyclesTest(_TempDb):
    """`detect_cycles`：walk 求反 → cycle / orphan / conflict，三份清单**永远存在**。"""

    def test_w6_cycle_is_marked_and_not_inherited(self):
        """J-D3：A→B→A → 两行 `relation='cycle'` + `basis='conflict'`，且**不参与继承**。

        三件事必须同时成立，少一件这个标记就没用：
          · 标了（读侧才认得出它是环）；
          · `parent_of` 在环上返回 None（继承/推送/hook 三条路都从这里走）；
          · **对照**：正常 A→B 的父照样判得出来（不把整棵树都堵死）。
        """
        self.insert("A", "B", depth_host=1)
        self.insert("B", "A", depth_host=1)
        self.insert("C", None, depth_host=0)
        self.insert("D", "C", depth_host=1)
        det = fish_lineage.detect_cycles(self.conn, "dsh")
        self.conn.commit()
        self.assertEqual(sorted(det["cycles"]), ["A", "B"])
        self.assertEqual(det["orphans"], [])
        rows = self.rows()
        self.assertEqual((rows["A"][1], rows["A"][2]), ("cycle", "conflict"))
        self.assertEqual((rows["B"][1], rows["B"][2]), ("cycle", "conflict"))
        self.assertIsNone(fish_lineage.parent_of(self.conn, "dsh", "A"),
                          "环成员返回了父：继承会顺着环走")
        self.assertIsNone(fish_lineage.parent_of(self.conn, "dsh", "B"))
        # 对照：正常边不许被误伤
        self.assertEqual(fish_lineage.parent_of(self.conn, "dsh", "D"), "C")
        self.assertEqual(fish_lineage.ancestor_sessions(self.conn, "dsh", "D"), ["C"])
        self.assertEqual(fish_lineage.ancestor_sessions(self.conn, "dsh", "B"), [])
        # 环上的行没有可用深度：不许写一个假深度
        self.assertEqual((rows["A"][3], rows["B"][3]), (0, 0))

    def test_w6_cycle_mark_is_reversible(self):
        """环被拆开之后标记要**还原**：不清账的标记 = 永久噪音（同 `conflict_from`）。"""
        self.insert("A", "B")
        self.insert("B", "A")
        fish_lineage.detect_cycles(self.conn, "dsh")
        self.conn.commit()
        self.assertEqual(self.rows()["A"][1], "cycle")
        self.conn.execute("UPDATE session_lineage SET parent_session=NULL "
                          "  WHERE session_id='A' AND host='dsh'")
        self.conn.commit()
        det = fish_lineage.detect_cycles(self.conn, "dsh")
        self.conn.commit()
        self.assertEqual(det["cycles"], [])
        self.assertEqual(sorted(det["restored"]), ["A", "B"])
        rows = self.rows()
        self.assertEqual(rows["A"][1], "spawn", "relation 必须还原")
        self.assertEqual(rows["A"][2], "header", "basis 必须还原")
        self.assertNotIn("cycle_relation_from", json.loads(rows["A"][6] or "{}"))

    def test_w6_conflict_names_rows_with_host_value_mismatch(self):
        """`depth_host` 与重算深度不等 → `conflict` 清单**逐条列名**（不是只给个数）。"""
        self.insert("A", None, depth_host=0)
        self.insert("B", "A", depth_host=5)
        self.insert("C", "B", depth_host=9)
        det = fish_lineage.detect_cycles(self.conn, "dsh")
        self.assertEqual(det["conflicts"], ["B", "C"])
        self.assertEqual(det["marked"], [], "没有环，不许标环")

    def test_w6_orphan_is_listed_and_stays_unknown_missing(self):
        """孤儿（父不存在）进 orphan 清单，且**不许**被改成 main / 被改成有父。"""
        self.insert("X", "gone", depth_host=1, origin="unknown", basis="missing")
        fish_lineage.recompute_depth(self.conn, "dsh")
        self.conn.commit()
        det = fish_lineage.detect_cycles(self.conn, "dsh")
        self.assertEqual(det["orphans"], ["X"])
        self.assertEqual(det["cycles"], [])
        rows = self.rows()
        self.assertEqual((rows["X"][0], rows["X"][2]), ("unknown", "missing"))
        self.assertEqual(rows["X"][3], 1, "父指针算一层：缺的是父那一行，不是这条边")

    def test_w6_empty_projection_reports_zero_lists(self):
        """0 行也要**有清单**（`None` 与 `[]` 是两件事）。"""
        det = fish_lineage.detect_cycles(self.conn, "dsh")
        self.assertEqual((det["rows"], det["cycles"], det["orphans"], det["conflicts"]),
                         (0, [], [], []))


# ================================================================ ③ 可观测性（J-W6-C）
class ReportFormatTest(_TempDb):
    """J-W6-C：三份清单**逐条列名**，0 行也打印"本次检测到 0 行"。"""

    def test_w6_zero_lists_are_printed_not_silent(self):
        """空清单必须打印一句话——"没打印"与"没检测"不可分。

        变异：把 `if not ids:` 改成 `if ids:` → 空清单什么都不打 → 这条红。
        """
        rep = fish_lineage_backfill.report(self.conn, "dsh")
        text = fish_lineage_backfill.format_report([rep])
        for label in ("cycle", "orphan", "conflict"):
            self.assertIn(f"{label}：本次检测到 0 行", text,
                          f"{label} 的空清单没有打印：读报告的人分不出『没检测』与『0 行』")
        self.assertIn("投影行数：0", text)

    def test_w6_non_empty_lists_name_every_row(self):
        """非空清单：每一行都要**列名**（只给个数等于让人自己去查库）。"""
        self.insert("A", "B")
        self.insert("B", "A")
        fish_lineage.detect_cycles(self.conn, "dsh")
        self.insert("X", "gone", origin="unknown", basis="missing")
        self.insert("P", None, depth_host=0)
        self.insert("Q", "P", depth_host=9)
        self.conn.commit()
        rep = fish_lineage_backfill.report(self.conn, "dsh")
        text = fish_lineage_backfill.format_report([rep])
        for sid in ("A", "B", "X", "Q"):
            self.assertIn(f"- {sid}", text, f"{sid} 没有被列名")
        self.assertNotIn("cycle：本次检测到 0 行", text)
        self.assertIn("cycle：2 行", text)
        self.assertIn("conflict：1 行", text)

    def test_w6_scan_report_zero_lists_are_printed(self):
        """`scan` 的报告走**同一个格式化**：空清单同样打印（不许有第二份实现）。"""
        self.use_adapter([node("A", None)])
        rep = fish_lineage_backfill.scan("dsh", conn=self.conn)
        text = fish_lineage_backfill.format_report([rep])
        for label in ("cycle", "orphan", "conflict"):
            self.assertIn(f"{label}：本次检测到 0 行", text)
        self.assertIn("dry-run", text)


# ================================================================ ④ 只读面
class ReadOnlyReportTest(_TempDb):
    """`report()` 必须能在 `mode=ro` 的连接上跑完：对账工具就是这么用它的。"""

    def test_w6_report_on_read_only_connection_writes_nothing(self):
        self.insert("A", "B")
        self.insert("B", "A")
        self.conn.commit()
        self.conn.close()
        ro = sqlite3.connect(f"file:{self.root / 'index.sqlite'}?mode=ro", uri=True)
        try:
            rep = fish_lineage_backfill.report(ro, "dsh")
            row = ro.execute("SELECT relation, basis FROM session_lineage "
                             " WHERE session_id='A'").fetchone()
        finally:
            ro.close()
        self.assertEqual(sorted(rep["cycles"]), ["A", "B"])
        self.assertEqual(rep["marked"], [], "report 是只读的：它不许标环")
        self.assertEqual(tuple(row), ("spawn", "header"), "只读连接上出现了写入")
        self.conn = sqlite3.connect(str(self.root / "index.sqlite"))   # tearDown 用

    def test_w6_report_without_tables_says_unknown_not_zero(self):
        """投影表不存在 → `missing_table=True` 的**判不到**，不是"0 行通过"。"""
        conn = sqlite3.connect(":memory:")
        try:
            rep = fish_lineage_backfill.report(conn, "dsh")
        finally:
            conn.close()
        self.assertTrue(rep["missing_table"])
        text = fish_lineage_backfill.format_report([rep])
        self.assertIn("判不到", text)


# ================================================================ ⑤ CLI 装配
class CliTest(_TempDb):
    """`fish lineage report` 的装配：argparse → `fish.cmd_lineage` → 同一份格式化。"""

    def test_w6_cli_report_smoke(self):
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", str(HERE / "fish.py"), "--root", str(self.root),
             "lineage", "report", "--host", "dsh"],
            cwd=str(HERE), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("cycle：本次检测到 0 行", proc.stdout)
        self.assertIn("== dsh ==", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
