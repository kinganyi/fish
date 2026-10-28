#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""⑥ citizen merge 判据（07 号报告 P0-2 + 评审 R-02 + 安全 S-03）。

M-1 dry-run 只数不改
M-2 apply 迁移归属、行数守恒
M-3 src 标 `merged_into:<dst>`
M-4 幂等（第二次 0 行）
M-5 拒绝合并系统主人（is_owner=1）
M-6 拒绝非 active 的 dst
M-7 成功写 `citizen.merged` 事件（不可抵赖）
M-8 ULID only（显示名被拒——同名活跃公民存在时按名字会合并错人）
（单事务回滚：构造一个必失败的表在测试里成本高，登记为 T04c 的补充项）
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fish  # noqa: E402


class CitizenMergeTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-merge-"))
        self.conn = fish.connect(self.tmp / "data")
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.a, self.b = fish.ulid(), fish.ulid()
        for cid, name in ((self.a, "甲"), (self.b, "乙")):
            self.conn.execute(
                "INSERT INTO citizens (citizen_id, display_name, status) VALUES (?,?,?)",
                (cid, name, "active"))
        for code in ("c1", "c2"):
            self.conn.execute(
                "INSERT INTO code_index (code,title,owner,status,file_path) "
                "VALUES (?,?,?,?,?)", (code, code, self.a, "active", f"demo/{code}.md"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _owner_count(self, owner):
        return self.conn.execute(
            "SELECT COUNT(*) FROM code_index WHERE owner=?", (owner,)).fetchone()[0]

    def _events(self, etype):
        return self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE type=?", (etype,)).fetchone()[0]

    # M-1
    def test_M1_dry_run_does_not_change(self):
        res = fish.citizen_merge(self.conn, src=self.a, dst=self.b)
        self.assertEqual(res["plan"]["code_index.owner"], 2)
        self.assertEqual(self._owner_count(self.a), 2)
        self.assertEqual(self._owner_count(self.b), 0)

    # M-2
    def test_M2_apply_moves_ownership(self):
        fish.citizen_merge(self.conn, src=self.a, dst=self.b, apply=True)
        self.assertEqual(self._owner_count(self.a), 0)
        self.assertEqual(self._owner_count(self.b), 2)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM code_index").fetchone()[0], 2)

    # M-3
    def test_M3_src_marked_merged(self):
        fish.citizen_merge(self.conn, src=self.a, dst=self.b, apply=True)
        row = self.conn.execute(
            "SELECT status FROM citizens WHERE citizen_id=?", (self.a,)).fetchone()
        self.assertEqual(row[0], f"merged_into:{self.b}")

    # M-4
    def test_M4_idempotent(self):
        fish.citizen_merge(self.conn, src=self.a, dst=self.b, apply=True)
        res = fish.citizen_merge(self.conn, src=self.a, dst=self.b, apply=True)
        self.assertEqual(res["moved"]["code_index.owner"], 0)

    # M-5
    def test_M5_refuses_owner_merge(self):
        self.conn.execute("UPDATE citizens SET is_owner=1 WHERE citizen_id=?",
                          (self.a,))
        self.conn.commit()
        with self.assertRaises(ValueError) as ctx:
            fish.citizen_merge(self.conn, src=self.a, dst=self.b, apply=True)
        self.assertIn("主人", str(ctx.exception))
        self.assertEqual(self._owner_count(self.a), 2, "拒绝时不许动任何行")

    # M-6
    def test_M6_refuses_inactive_dst(self):
        self.conn.execute("UPDATE citizens SET status='retired' WHERE citizen_id=?",
                          (self.b,))
        self.conn.commit()
        with self.assertRaises(ValueError):
            fish.citizen_merge(self.conn, src=self.a, dst=self.b, apply=True)

    # M-7
    def test_M7_writes_merged_event(self):
        before = self._events("citizen.merged")
        fish.citizen_merge(self.conn, src=self.a, dst=self.b, apply=True)
        self.assertEqual(self._events("citizen.merged"), before + 1)

    # M-8
    def test_M8_rejects_display_name(self):
        with self.assertRaises(ValueError) as ctx:
            fish.citizen_merge(self.conn, src="甲", dst=self.b)
        self.assertIn("ULID", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
