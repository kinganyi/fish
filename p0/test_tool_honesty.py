#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 判据（外部审查 🟠-B2/B3）：工具诚实——SKIP/MISS 不许包装成成功。

H-1 `mutation_workspace_normalize.py`：SKIP/MISS 必须让进程非 0
H-2 `mutation_injection_status.py`：同上
H-3 `tools/golden_compare.py`：缺 run2 = 判不到（noise.identical 默认 False）

判据是**静态**读源码（不复用被测实现的运行结果——那等于自己证自己）。
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _src(rel):
    return (HERE / rel).read_text(encoding="utf-8")


class ToolHonestyTest(unittest.TestCase):

    # H-1
    def test_H1_workspace_normalize_skips_are_failures(self):
        src = _src("mutation_workspace_normalize.py")
        self.assertIn("SKIP", src)
        self.assertIn("return 2", src, "SKIP 必须让进程非 0（旧实现无条件 return 0）")
        self.assertIn("变异未真正执行", src, "缺省文案：要告诉人哪里没跑")

    # H-2
    def test_H2_injection_status_skips_are_failures(self):
        src = _src("mutation_injection_status.py")
        self.assertIn("SKIP", src)
        self.assertIn("变异未真正执行", src)

    # H-3
    def test_H3_golden_missing_run2_is_not_pass(self):
        src = _src("tools/golden_compare.py")
        self.assertIn('"identical": old2 is not None', src,
                      "缺 run2 时 noise.identical 必须为 False（旧实现预置 True → fail-open）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
