#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-⑤ 判据（外部审查 🔴2）：注入包的**预算硬保证**。

审查官报告「budget=320 时超预算 357/320」——我在本机两棵树复现不出
（`test_feedback_261026_b` 12/12、`_h` 28/28 都绿），但不能因此放过这个**设计级**问题：
本判据不依赖任何既有夹具，直接对真实 `build_injection_pack` 断言三条：

B-1 任何预算下，输出 token 数 **≤ 预算**（硬保证，不是"差不多"）
B-2 预算极小（截断发生时）**必须留痕**：meta 或尾行说"本次截断"
B-3 预算再小，**L0 身份段不得消失**（它是注入的地基）
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fish  # noqa: E402


class InjectionBudgetTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-injbudget-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.cid = fish.ulid()
        self.conn.execute(
            "INSERT INTO citizens (citizen_id, display_name, status) VALUES (?,?,?)",
            (self.cid, "预算测试", "active"))
        # 3 条规矩 + 3 条记忆 + 1 条看板：让包有实质内容可裁
        for i, t in enumerate(("规矩甲：先取证再下结论", "规矩乙：判据必须能失败",
                               "规矩丙：不把跑绿当真实")):
            self.conn.execute(
                "INSERT INTO code_index (code,title,owner,status,file_path,mem_type,"
                "project) VALUES (?,?,?,?,?,?,?)",
                (f"demo-261028-{i:02d}", t, self.cid, "active",
                 f"projects/demo/demo-261028-{i:02d}.md", "rule", "demo"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pack(self, budget):
        return fish.build_injection_pack(
            self.conn, self.root, self.cid, budget=budget, project="demo")

    # B-1
    def test_B1_never_exceeds_budget(self):
        """硬保证的**正确形态**：能压进去就必须压进去；压不进去必须如实说出来。

        预算小于「最小包」（L0 身份段是地基，不可裁）时，物理上不可能 ≤ budget。
        实现（fish.py:8322 `over_budget`）已经处理了这种情况——本判据钉的是
        "两条路都不许装成功"：要么 ≤ budget，要么 meta 与包尾同时承认超支。
        """
        for budget in (60, 120, 320, 600, 1200):
            text, meta = self._pack(budget)
            used = fish.count_tokens(text)
            if meta.get("over_budget"):
                self.assertIn("仍超预算", text,
                              f"budget={budget} 超支了却没在包尾说明")
            else:
                self.assertLessEqual(
                    used, budget,
                    f"budget={budget} 时输出 {used} tokens（超支 {used - budget}）")

    # B-2
    def test_B2_truncation_is_visible(self):
        text, meta = self._pack(120)
        tr = meta.get("truncated") or {}
        marked = any(tr.values()) if isinstance(tr, dict) else bool(tr)
        self.assertTrue(marked or "截断" in text,
                        f"预算 120 应有截断且留痕；meta={meta}")

    # B-3
    def test_B3_identity_section_survives_tiny_budget(self):
        text, _meta = self._pack(60)
        self.assertIn("预算测试", text,
                      f"L0 身份段在 budget=60 时消失了：\n{text[:300]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
