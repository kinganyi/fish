#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W5 判据：注入包的**可逆性**（不变量 I3）。

## 守什么

包尾原本只说"砍了什么、砍了几条"。读者知道自己缺了东西，却**不知道去哪拿**——
于是那段内容等于白白消失。

这条不是锦上添花：`76-方案` 里对比 headroom 时得出的结论就是"**fish 比它差的地方
正是缺这一半**"——它有 CCR 取回通道，fish 只有一句"裁剪=['候选','L2']"。

## 为什么这条判据必须能失败

"报告了裁剪"和"报告了怎么取回"**看起来都像"有输出"**。如果判据只断言"包尾非空"，
把取回句柄删掉它也照样绿。所以这里逐条钉**映射关系**，不是只钉"有这行字"。
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish  # noqa: E402


class RetrievalHintTest(unittest.TestCase):

    def test_no_trim_means_no_hint_line(self):
        """没裁任何段时不印"要回：…"——包尾已经说了「本次无截断」，再补一句是噪音。"""
        self.assertEqual(fish.retrieval_hint({}), "")
        self.assertEqual(fish.retrieval_hint({"rules": 0, "push": 0, "l2": 0}), "")

    def test_every_trimmable_section_has_a_hint(self):
        """**完整性**：凡是能被裁的段，都必须有取回入口。

        这条防的是"以后新增一个可裁段，忘了给它配取回路径"——
        那种遗漏不会让任何东西变红，只会让那段内容静默地变成不可取回。
        """
        # `truncation_counts` 能产出的键就是这些（见 announce_truncation 的取值域）
        trimmable = {"rules", "push", "board", "l2", "attention", "lessons"}
        hinted = {k for k, _n, _h in fish._RETRIEVAL_HINTS}
        self.assertEqual(trimmable - hinted, set(),
                         f"这些段可裁却没有取回入口：{trimmable - hinted}")

    def test_hint_maps_each_section_to_a_command(self):
        """逐段钉映射——不是只钉"有这行字"。"""
        counts = {"push": 3, "l2": 1, "attention": 1, "lessons": 1,
                  "rules": 4, "board": 2}
        hint = fish.retrieval_hint(counts)
        self.assertTrue(hint.startswith("要回："), f"格式不对：{hint!r}")
        for token in ("候选", "L2索引", "注意事项", "错题本", "规矩", "看板"):
            self.assertIn(token, hint, f"{token} 没有取回入口：{hint}")
        # 每段都要带一个可执行入口（反引号或 MCP 名）
        for seg in hint.split("；"):
            self.assertTrue("`" in seg or "MCP" in seg,
                            f"这一段没给可执行入口：{seg!r}")

    def test_only_trimmed_sections_appear(self):
        """只印被裁的段。全印会让"裁了什么"这个问题失焦。"""
        hint = fish.retrieval_hint({"push": 2})
        self.assertIn("候选", hint)
        for absent in ("L2索引", "错题本", "规矩", "看板"):
            self.assertNotIn(absent, hint, f"没裁 {absent} 却印了取回入口")

    def test_tail_contains_hint_when_trimmed(self):
        """包尾**真的**带上了它（不是只有 `retrieval_hint` 单独能算出来）。"""
        tail = fish.injection_tail_text({"push": 3, "l2": 1}, "2026-11-18T00:00:00Z",
                                        sections=["status"])
        self.assertIn("要回：", tail)
        self.assertIn("候选", tail)
        self.assertIn("as-of", tail, "as-of 行不能被挤掉")

    def test_worst_case_tail_covers_the_hint(self):
        """预算预留必须把取回行算进去。

        这个容易漏：`injection_tail_worst` 是包尾长度的上界，如果它不含取回行，
        "裁到 N token"之后追加取回行会**静默超预算**——而超预算正是这段代码
        最该响亮报告的东西。
        """
        worst = fish.injection_tail_worst()
        real = fish.injection_tail_text(
            {"rules": 9999, "push": 9999, "board": 9999, "l2": 1,
             "attention": 1, "lessons": 1},
            fish._INJECTION_TAIL_WORST_AS_OF, sections=None)
        self.assertGreaterEqual(len(worst), len(real),
                                f"最坏预留({len(worst)}) 短于真实({len(real)})")
        self.assertGreaterEqual(fish.count_tokens(worst), fish.count_tokens(real),
                                "最坏预留的 token 数短于真实")
        self.assertIn("要回：", worst, "最坏预留里根本没有取回行")


if __name__ == "__main__":
    unittest.main(verbosity=2)
