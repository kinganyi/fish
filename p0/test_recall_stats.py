#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""⑥ recall 统计行判据（BUG-5 收口）：区分"没有"与"没搜到"。

R-1 命中时：输出含「扫描 N 条 ｜ 编码 a ｜ 标题 b ｜ 正文 c ｜ 返回 d」
R-2 未命中时：统计行**仍然打印**（说明扫描了多少），不是只给"未找到"
R-3 计数自洽：标题 + 正文 + 编码 == 返回数（每行只可能落一个来源桶）
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


class RecallStatsTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-stats-"))
        self.data = self.tmp / "data"
        conn = fish.connect(self.data)
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        # 直接写库（不依赖 remember 的 CLI/MCP 包装；测试只关心 recall 的输出形状）
        for idx, word in (("01", "甲"), ("02", "乙")):
            code = f"demo-261028-{idx}"
            conn.execute(
                "INSERT INTO code_index (code,title,project,status,file_path) "
                "VALUES (?,?,?,?,?)",
                (code, f"{word}条", "demo", "active", f"demo/{code}.md"))
            conn.execute(
                "INSERT INTO memory_fts (code, title_idx, body_idx) VALUES (?,?,?)",
                (code, f"{word}条", f"独特词{word}在这里"))
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _recall(self, query):
        args = _Args()
        args.root = str(self.data)
        args.query = query
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fish.cmd_recall(args)
        return buf.getvalue()

    # R-1
    def test_R1_stats_line_on_hit(self):
        out = self._recall("独特词甲")
        self.assertIn("扫描", out)
        self.assertIn("标题", out)
        self.assertIn("正文", out)
        self.assertIn("返回", out)

    # R-2
    def test_R2_stats_line_on_miss(self):
        out = self._recall("根本不存在的词ZZZ")
        self.assertIn("扫描", out, "未命中也要给统计（否则分不清没有 vs 没搜到）")

    # R-3
    def test_R3_counts_are_self_consistent(self):
        out = self._recall("独特词")
        line = [ln for ln in out.splitlines() if ln.startswith("> 扫描")][0]
        nums = {}
        for part in line.lstrip("> ").split("｜"):
            key, _, value = part.strip().partition(" ")
            nums[key] = int(value.split()[0])
        total_buckets = nums["编码"] + nums["标题"] + nums["正文"]
        self.assertEqual(total_buckets, nums["返回"], f"桶计数不自洽：{nums}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
