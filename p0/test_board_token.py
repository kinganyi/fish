#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B1 判据（体验复测报告 v1.1）：`board` 子命令的 `--token`。

B1 原始现象：`board show --token x` → `unrecognized arguments: --token x`。
用户照着别处习惯敲就踩，而且报错没告诉人"该去哪"。

T1 `--token` 必须被接受（不再 unrecognized）
T2 无效 token 必须**明确报错**（不能静默当没给）
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
FISH = HERE / "fish.py"


class BoardTokenTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-btoken-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(FISH), "--root", str(self.tmp)] + list(args),
            capture_output=True, text=True, encoding="utf-8", errors="replace")

    def test_T1_token_accepted_by_show(self):
        r = self._run("board", "show", "--token", "x")
        self.assertNotIn("unrecognized", (r.stderr or "").lower(),
                         f"board show --token 仍被拒：{(r.stderr or '')[:200]}")

    def test_T2_invalid_token_fails_loudly(self):
        self._run("init")
        r = self._run("board", "checkin", "--token", "fsh_not_a_real_token",
                      "--doing", "探针")
        self.assertIn("无对应公民", r.stderr or "",
                      f"无效 token 必须明确报错（不能静默当没给）：{(r.stderr or '')[:300]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
