#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""⑥ `--by relevance` 判据（BUG-5 收口）：默认 priority 零回归，relevance 走 bm25。

B-1 `--by relevance` 的输出匹配来源标 `bm25`（不是"标题/正文"）
B-2 默认（priority）保持原形状（`匹配:标题` / `匹配:正文`），不出现 `bm25`
B-3 两种排序都返回同一批候选的超集（同一库、同一词，条数一致）
"""
import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fish  # noqa: E402


class _Args:
    root = None
    query = None
    project = None
    code = None
    tags = None
    limit = 10
    scope = "all"
    by = "priority"


class ByRelevanceTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-byrel-"))
        self.data = self.tmp / "data"
        conn = fish.connect(self.data)
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        for idx, word in (("01", "甲"), ("02", "乙")):
            code = f"demo-261028-{idx}"
            conn.execute(
                "INSERT INTO code_index (code,title,project,status,file_path) "
                "VALUES (?,?,?,?,?)",
                (code, f"{word}条", "demo", "active", f"demo/{code}.md"))
            conn.execute(
                "INSERT INTO memory_fts (code, title_idx, body_idx) VALUES (?,?,?)",
                (code, fish.fts_text(f"{word}条"),
                 fish.fts_text(f"独特词{word}在这里")))
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _recall(self, query, by):
        args = _Args()
        args.root = str(self.data)
        args.query = query
        args.by = by
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fish.cmd_recall(args)
        return buf.getvalue()

    # B-1
    def test_B1_relevance_marks_bm25(self):
        out = self._recall("独特词", "relevance")
        self.assertIn("匹配:bm25", out)

    # B-2
    def test_B2_priority_keeps_old_shape(self):
        out = self._recall("独特词", "priority")
        self.assertNotIn("匹配:bm25", out)
        self.assertTrue("匹配:标题" in out or "匹配:正文" in out)

    # B-3
    def test_B3_same_hit_count(self):
        a = self._recall("独特词", "priority")
        b = self._recall("独特词", "relevance")
        self.assertEqual(a.count("\t匹配:"), b.count("\t匹配:"),
                         "两种排序的命中条数应一致（同一库同一词）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
