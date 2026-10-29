#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静默吞错判据（`fish_静默吞错分档清单 v1.0` 的 B 档落地守卫）。

规则：三个核心文件里**不允许** `except X:` 后只有 `pass`（可有注释）。
改回 `pass` → 这条立刻红；这是"出声"的唯一可执行保证。

为什么只扫三文件：清单的 B 档范围就是这三个（fish.py / mcp_server.py /
fish_common.py）。其余模块（fish_push / 各 hook / 语料）是另一轮的范围，
判据不假装覆盖它们（宁可小而不漏，不要大而虚）。
"""
import re
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORE = ("fish.py", "mcp_server.py", "fish_common.py")
# `pass` 后**必须**带 `# A档` 标记才豁免（显式声明"我知道这里静默"）；
# 无标记的静默一律算违规——判据的意图是"静默必须是自觉决定"，不是"静默为零"。
SILENT = re.compile(r"except[^\n]*:\s*\n(?:\s*#.*\n)*\s*pass\b(?!\s*#\s*A档)")


class SilentAuditTest(unittest.TestCase):

    def test_no_silent_except_in_core_files(self):
        offenders = {}
        for name in CORE:
            text = (HERE / name).read_text(encoding="utf-8", errors="replace")
            n = len(SILENT.findall(text))
            if n:
                offenders[name] = n
        self.assertEqual(
            offenders, {},
            f"核心文件里还有静默 except（应改用 warn_ignored / warn_derived 出声）：{offenders}")

    def test_warn_ignored_exists_and_writes_stderr(self):
        """出声出口必须存在，且真的写 stderr（不是空函数）。"""
        import fish
        self.assertTrue(callable(getattr(fish, "warn_ignored", None)),
                        "fish.warn_ignored 不存在（静默点的统一出口被删了？）")
        src = (HERE / "fish.py").read_text(encoding="utf-8")
        m = re.search(r"def warn_ignored\(.*?\):\n(.*?)\ndef ", src, re.S)
        self.assertIsNotNone(m, "找不到 warn_ignored 的实现")
        self.assertIn("stderr", m.group(1), "warn_ignored 必须写 stderr（否则等于没出声）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
