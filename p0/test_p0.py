#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fish P0 验收测试
================
每条测试都对应 §G 可测试性判据表里的一行。
纪律：每条判据都必须能红——所以除了正例，还包含"故意破坏"的反例。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fish  # noqa: E402


class FishP0Test(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fish-p0-")
        self.root = os.path.join(self.tmp, "data")
        os.makedirs(self.root, exist_ok=True)
        conn = fish.connect(fish.Path(self.root))
        conn.executescript(fish.SCHEMA)
        conn.execute(
            "INSERT INTO code_registry (prefix, namespace, last_date, last_seq, status) VALUES (?,?,?,?,?)",
            ("TEST", "test", "", 0, "active"),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------- 判据 1

    def test_event_immutable(self):
        """事件不可改、不可删——UPDATE / DELETE 必须报错。"""
        conn = fish.connect(fish.Path(self.root))
        fish.append_event(conn, "config.changed", {"citizen_id": "system"},
                          {"kind": "config", "id": "x"}, {"k": "v"})
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("UPDATE events SET type='hacked' WHERE seq=1")
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM events WHERE seq=1")
        # 反例自证：把触发器删掉后必须能改（证明"红"来自触发器而非偶然）
        conn.execute("DROP TRIGGER ev_no_update")
        conn.execute("UPDATE events SET type='hacked' WHERE seq=1")
        row = conn.execute("SELECT type FROM events WHERE seq=1").fetchone()
        self.assertEqual(row[0], "hacked")
        conn.close()

    # ---------------------------------------------------------- 判据 2

    def test_concurrent_codes_no_duplicate(self):
        """50 个并发分配，编码必须无重号、无跳号。"""
        n = 50

        def worker(_):
            conn = fish.connect(fish.Path(self.root))
            try:
                return fish.next_code(conn, "TEST")
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=12) as ex:
            codes = list(ex.map(worker, range(n)))

        self.assertEqual(len(set(codes)), n, f"出现重号：{sorted(codes)}")
        # 序号必须连续 01..n
        seqs = sorted(int(c.rsplit("-", 1)[1]) for c in codes)
        self.assertEqual(seqs, list(range(1, n + 1)), "序号不连续")
        # 反例自证：把 last_seq 改成错误值，下一次分配应跳号（证明判据敏感）
        conn = fish.connect(fish.Path(self.root))
        conn.execute("UPDATE code_registry SET last_seq=999 WHERE prefix='TEST'")
        conn.commit()
        low = fish.next_code(conn, "TEST")
        self.assertTrue(low.endswith("-1000"), f"未按 last_seq 推进：{low}")
        conn.close()

    # ---------------------------------------------------------- 判据 3

    def test_rebuild_projection(self):
        """投影可重建且**保真**：调真实的 rebuild_projection，逐字段核对（不只条数）。

        背景（2026-10-22 修 D-10）：旧版这条测试在手抄一份重建逻辑、只断言条数，
        所以生产代码把 owner 清空、时间重置成重建时刻、superseded 复活成 active，
        它全都发现不了。现在改为「调真函数 + 与重建前投影逐字段比对」。
        """
        conn = fish.connect(fish.Path(self.root))
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        codes = []
        for i in range(3):
            code, _ = fish.record_memory(
                conn, fish.Path(self.root), project="TEST", title=f"t{i}",
                body=f"正文{i}", mem_type="lesson", tags=["x"],
                owner="u-actor", session_actor="sess-1")
            codes.append(code)
        # 一组替代关系：验证 status / superseded_by 能重建
        fish.supersede_memory(conn, codes[0], codes[1], reason="test")
        # 一条访问记录：没有事件来源，必须靠快照保留
        conn.execute("UPDATE code_index SET access_count=7, last_accessed_at=?, dormant=1 "
                     "WHERE code=?", (fish.now_utc(), codes[2]))
        conn.commit()

        fields = [("title", 1), ("project", 2), ("owner", 3), ("mem_type", 4),
                  ("visibility", 5), ("status", 6), ("tags", 7), ("file_path", 8),
                  ("occurred_at", 9), ("session_actor", 11), ("superseded_by", 12),
                  ("access_count", 13), ("dormant", 14)]

        def dump():
            return {r[0]: r for r in conn.execute(
                "SELECT code,title,project,owner,mem_type,visibility,status,tags,file_path,"
                "occurred_at,recorded_at,session_actor,superseded_by,access_count,dormant "
                "FROM code_index")}

        before = dump()
        rep = fish.rebuild_projection(conn, fish.Path(self.root))
        after = dump()

        self.assertEqual(rep["mismatch"], [], f"重建对账有不一致：{rep['mismatch']}")
        self.assertEqual(set(after), set(before), "重建后编码集合必须一致")
        for code in before:
            for name, idx in fields:
                self.assertEqual(
                    after[code][idx], before[code][idx],
                    f"{code} 的 {name} 重建后变了：{before[code][idx]!r} → {after[code][idx]!r}")

        # 几个关键性质单独断言，让失败信息一眼看懂
        self.assertEqual(after[codes[0]][6], "superseded", "superseded 状态丢了")
        self.assertEqual(after[codes[0]][12], codes[1], "superseded_by 丢了")
        self.assertEqual(after[codes[0]][3], "u-actor", "owner 必须来自事件 actor，不能清空")
        self.assertEqual(after[codes[2]][13], 7, "access_count 必须保留")
        self.assertEqual(after[codes[2]][14], 1, "dormant 必须保留")
        conn.close()

    # ---------------------------------------------------------- 判据 3b

    def test_recorded_event_carries_occurred_at(self):
        """D-11 回归：memory.recorded 的 payload 必须带 occurred_at。

        没有它，忠实重建在事件层面就不可能——只能靠投影兜底（见 07 D-11）。
        """
        conn = fish.connect(fish.Path(self.root))
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        code, _ = fish.record_memory(conn, fish.Path(self.root), project="TEST", title="带时间")
        row = conn.execute("SELECT payload FROM events WHERE type='memory.recorded'").fetchone()
        p = json.loads(row[0])
        self.assertIn("occurred_at", p, "payload 缺 occurred_at（07 D-11 回归）")
        self.assertTrue(p["occurred_at"], "occurred_at 不能为空")
        col = conn.execute("SELECT occurred_at FROM code_index WHERE code=?", (code,)).fetchone()[0]
        self.assertEqual(p["occurred_at"], col, "事件与投影必须是同一份时间事实")
        conn.close()

    # ---------------------------------------------------------- 判据 3c

    def test_rebuild_normalizes_legacy_owner(self):
        """重建时 owner 必须是有效公民：历史遗留的非公民值归一到系统主人并计数（07 D-13）。"""
        conn = fish.connect(fish.Path(self.root))
        conn.executescript(fish.SCHEMA)
        conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,status,created_at,is_owner) "
            "VALUES ('OWNER1','主人','resident','active',?,1)", (fish.now_utc(),))
        conn.commit()
        # 一条历史遗留归属：'human-anyi' 从来不是公民 ID
        code, _ = fish.record_memory(conn, fish.Path(self.root), project="TEST",
                                     title="旧归属", owner="human-anyi")
        rep = fish.rebuild_projection(conn, fish.Path(self.root))
        owner = conn.execute("SELECT owner FROM code_index WHERE code=?", (code,)).fetchone()[0]
        self.assertEqual(owner, "OWNER1", "无效 owner 必须归一到系统主人，不能写成幽灵 ID")
        self.assertEqual(rep["owner_normalized"], 1, "归一必须计数，不能静默")
        self.assertEqual(rep["mismatch"], [], "有意归一不应算作对账不一致")
        conn.close()

    # ---------------------------------------------------------- 判据 3d

    def test_rebuild_corrects_impossible_occurred_at(self):
        """occurred_at 晚于记录时间 = 逻辑上不可能，重建必须用事件时间纠正（07 D-11 遗留）。

        注意：这里必须造**历史形态**的事件（payload 里没有 occurred_at）——
        2026-10-22 之后的新事件都自带 occurred_at，走不到这条兜底路径。
        """
        conn = fish.connect(fish.Path(self.root))
        conn.executescript(fish.SCHEMA)
        code = fish.next_code(conn, "TEST")
        # 历史形态：memory.recorded 的 payload 不含 occurred_at
        fish.append_event(conn, "memory.recorded", {"citizen_id": "u"},
                          {"kind": "memory", "id": code},
                          {"code": code, "title": "旧事件",
                           "body_ref": f"projects/TEST/{code}.md"})
        # 投影里是被历史坏重建压平的时间：晚于记录时间
        conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,tags,"
            "file_path,occurred_at,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (code, "旧事件", "TEST", "u", "fact", "shared", "active", "[]",
             f"projects/TEST/{code}.md", "2099-01-01T00:00:00Z", fish.now_utc()))
        conn.commit()

        rep = fish.rebuild_projection(conn, fish.Path(self.root))
        row = conn.execute("SELECT occurred_at,recorded_at FROM code_index WHERE code=?",
                           (code,)).fetchone()
        self.assertNotEqual(row[0], "2099-01-01T00:00:00Z", "被污染的时间必须纠正")
        self.assertLessEqual(row[0], row[1], "occurred_at 不得晚于 recorded_at")
        self.assertEqual(rep["occurred_corrected"], 1, "纠正必须计数，不能静默")
        self.assertEqual(rep["mismatch"], [], "有意纠正不算对账不一致")
        conn.close()

    # ---------------------------------------------------------- 判据 4

    def test_neighbors_no_cross_project(self):
        """邻近查询不能跨项目。"""
        conn = fish.connect(fish.Path(self.root))
        conn.execute(
            "INSERT INTO code_registry (prefix, namespace, last_date, last_seq, status) VALUES (?,?,?,?,?)",
            ("OTHER", "other", "", 0, "active"),
        )
        conn.commit()
        a = fish.next_code(conn, "TEST")
        b = fish.next_code(conn, "OTHER")   # 异项目，时间相邻
        c = fish.next_code(conn, "TEST")
        for code in (a, b, c):
            conn.execute(
                "INSERT INTO code_index (code,title,project,mem_type,status,file_path) VALUES (?,?,?,?,?,?)",
                (code, code, code.split("-")[0], "fact", "active", ""),
            )
        conn.commit()

        prev = conn.execute(
            "SELECT code FROM code_index WHERE code<? AND project=? ORDER BY code DESC LIMIT 3",
            (c, "TEST"),
        ).fetchall()
        prev_codes = [r[0] for r in prev]
        self.assertIn(a, prev_codes, "同项目前一条应出现")
        self.assertNotIn(b, prev_codes, "异项目不应出现")
        conn.close()

    # ---------------------------------------------------------- 判据 5

    def test_healthcheck_detects_broken_link(self):
        """断链必须被 healthcheck 检出（反例：文件不存在）。"""
        conn = fish.connect(fish.Path(self.root))
        code = fish.next_code(conn, "TEST")
        conn.execute(
            "INSERT INTO code_index (code,title,project,mem_type,status,file_path) VALUES (?,?,?,?,?,?)",
            (code, "x", "TEST", "fact", "active", "projects/TEST/not-exists.md"),
        )
        conn.commit()
        broken = []
        root = fish.Path(self.root)
        for c, fp in conn.execute("SELECT code,file_path FROM code_index").fetchall():
            if fp and not (root / fp).exists():
                broken.append(c)
        self.assertIn(code, broken, "断链未被检出")
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
