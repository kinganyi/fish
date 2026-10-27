#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""⑥ `citizen merge` 判据（07 号报告 P0-2：身份可分裂且不可合并）。

M-1 dry-run 只数不改
M-2 apply 后归属迁移、行数守恒（不删行）
M-3 src 标 `merged_into:<dst>`（可追溯）
M-4 幂等：第二次 apply 迁移 0 行
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
        for cid, name in (("A", "甲"), ("B", "乙")):
            self.conn.execute(
                "INSERT INTO citizens (citizen_id, display_name, status) "
                "VALUES (?,?,?)", (cid, name, "active"))
        for code in ("c1", "c2"):
            self.conn.execute(
                "INSERT INTO code_index (code,title,owner,status,file_path) "
                "VALUES (?,?,?,?,?)", (code, code, "A", "active", f"demo/{code}.md"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _count(self, owner):
        return self.conn.execute(
            "SELECT COUNT(*) FROM code_index WHERE owner=?", (owner,)).fetchone()[0]

    # M-1
    def test_M1_dry_run_does_not_change(self):
        res = fish.citizen_merge(self.conn, src="A", dst="B")
        self.assertEqual(res["plan"]["code_index.owner"], 2)
        self.assertEqual(self._count("A"), 2)
        self.assertEqual(self._count("B"), 0)

    # M-2
    def test_M2_apply_moves_ownership(self):
        fish.citizen_merge(self.conn, src="A", dst="B", apply=True)
        self.assertEqual(self._count("A"), 0)
        self.assertEqual(self._count("B"), 2)
        # 行数守恒（不删行）
        total = self.conn.execute("SELECT COUNT(*) FROM code_index").fetchone()[0]
        self.assertEqual(total, 2)

    # M-3
    def test_M3_src_marked_merged(self):
        fish.citizen_merge(self.conn, src="A", dst="B", apply=True)
        row = self.conn.execute(
            "SELECT status FROM citizens WHERE citizen_id='A'").fetchone()
        self.assertEqual(row[0], "merged_into:B")

    # M-4
    def test_M4_idempotent(self):
        fish.citizen_merge(self.conn, src="A", dst="B", apply=True)
        res = fish.citizen_merge(self.conn, src="A", dst="B", apply=True)
        self.assertEqual(res["moved"]["code_index.owner"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
