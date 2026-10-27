#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T05 · `kb toc` 判据（目录快照：分类概览 + 前 3 标题 + 分页）。

被测：`fish_kb.kb_toc`（fish.py 侧 re-export，同一对象）。
  T1  分类概览：每个目录给「文件数 + 前 3 个标题」（information scent，36 号 §2.4）
  T2  token 经济：同 depth 下 toc 的 tokens ≤ tree 的 40%（36 号"token 少 95%"的保守下限）
  T3  offset 越界：空页 + 明说"不是空库"（三态分明，不许静默空）
  T4  limit 截断：必须给翻页提示（offset/limit）
变异（见 `_audit/mutation_toc.py`）：M-toc1 去掉标题 → T1 红；M-toc2 去掉越界分支 → T3 红。
"""
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

import fish_kb  # noqa: E402 —— 与 fish.py 同目录（cwd 导入）


class KbTocTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-toc-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.vault_dir = self.tmp / "vault"
        for sub, titles in (("10-漫剧", ["西游逆局", "sss级机娘", "偏偏难哄"]),
                            ("40-教程", ["AI宝藏指南", "即梦 Seedance 2.0"]),
                            ("30-自我", ["写作风格蒸馏分析报告"])):
            d = self.vault_dir / sub
            d.mkdir(parents=True)
            for i, t in enumerate(titles):
                (d / f"{i:02d}-{t}.md").write_text(f"# {t}\n\n正文……\n", encoding="utf-8")
        (self.vault_dir / "根文件.md").write_text("# 根文件\n", encoding="utf-8")
        self.vault = fish.kb_vault_add(self.conn, name="toc测试库",
                                       path=str(self.vault_dir), mode="rw")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _toc(self, **kw):
        return fish.kb_toc(self.conn, self.vault, **kw)

    # ------------------------------------------------------------- T1
    def test_T1_categories_with_counts_and_scent_titles(self):
        """每个目录 = 文件数 + 前 3 个标题（判断"要不要下钻"的信息量）。"""
        res = self._toc(depth=2)
        text = res["text"]
        self.assertIn("10-漫剧/", text)
        self.assertIn("3 篇", text)
        for title in ("西游逆局", "sss级机娘", "偏偏难哄"):
            self.assertIn(title, text, f"缺 information scent 标题：{title}")
        # 目录项 = 3 个一级目录 + 根下 1 个文件
        self.assertEqual(res["total"], 4)
        self.assertEqual(res["shown"], 4)

    # ------------------------------------------------------------- T2
    def test_T2_token_economy_vs_tree(self):
        """同 depth 下 toc 的 tokens ≤ tree 的 40%（保守下限；真实库差距更大）。"""
        tree = fish.kb_tree(self.conn, self.vault, depth=2)
        toc = self._toc(depth=2)
        self.assertGreater(tree["tokens"], 0)
        self.assertLessEqual(
            toc["tokens"], max(1, int(tree["tokens"] * 0.4)),
            f"toc tokens={toc['tokens']} 应 ≤ tree 的 40%（={tree['tokens']}）")

    # ------------------------------------------------------------- T3
    def test_T3_offset_past_end_is_empty_but_explained(self):
        """offset 越界 → 空页 + 明说"不是空库"（三态分明）。"""
        res = self._toc(depth=2, offset=999)
        self.assertEqual(res["shown"], 0)
        self.assertIn("不是空库", res["text"])

    # ------------------------------------------------------------- T4
    def test_T4_limit_truncation_has_paging_hint(self):
        """limit 截断必须给翻页提示（offset/limit 的显式口径）。"""
        res = self._toc(depth=2, limit=1)
        self.assertEqual(res["shown"], 1)
        self.assertIn("--offset 1", res["text"])

    # ------------------------------------------------------- M-toc1
    def test_M_toc1_mutation_no_titles_goes_red(self):
        """变异自证：标题提取被摘掉 → T1 的 information scent 断言必红。"""
        orig = fish_kb._kb_toc_title
        try:
            fish_kb._kb_toc_title = lambda p: ""
            res = self._toc(depth=2)
            with self.assertRaises(AssertionError):
                self.assertIn("西游逆局", res["text"])
        finally:
            fish_kb._kb_toc_title = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
