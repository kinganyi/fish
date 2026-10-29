#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-3 判据：公开包文档里的 `fish <cmd>` 必须能被 CLI 解析（防"文档说能跑、实际没这个命令"）。

## 为什么单独一个判据

历史文档（规格/设计）里提到过大量**规划中**的命令（`fish export` / `fish persona`…），
它们不是现状承诺，不能当成漂移。真正要守的是**对外文档**（公开包 README 的快速开始）
——用户照着敲，必须每一条都能解析。

## 判不到 vs 跳过

公开包目录不存在（如只拿到 p0/ 的机器）→ **skip**（判不到，不是通过）；
存在但命令解析失败 → **红**（这是真漂移）。
"""
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 公开包目录：环境变量优先；默认放家目录下（不写本机绝对路径）
PUBLIC = Path(os.environ.get("FISH_PUBLIC", Path.home() / "fish-public"))
CMD_RE = re.compile(r"python\s+p0/fish\.py\s+([a-z][a-z0-9\-]*)")


def _cli_commands():
    proc = subprocess.run([sys.executable, "fish.py", "--help"], cwd=str(HERE),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    m = re.search(r"\{([a-z0-9,\-]+)\}", proc.stdout or "")
    return set(m.group(1).split(",")) if m else set()


@unittest.skipUnless(PUBLIC.exists(), "公开包目录不存在（判不到，不是通过）")
class DocCommandsTest(unittest.TestCase):

    def test_DC1_readme_commands_are_parseable(self):
        readme = PUBLIC / "README.md"
        self.assertTrue(readme.exists(), f"公开包 README 缺失：{readme}")
        text = readme.read_text(encoding="utf-8")
        mentioned = set(CMD_RE.findall(text))
        self.assertTrue(mentioned, "README 里应至少有可解析的 fish 命令示例")
        cli = _cli_commands()
        self.assertTrue(cli, "--help 解析不出命令列表")
        missing = sorted(c for c in mentioned if c not in cli)
        self.assertEqual(missing, [], f"README 提到但 CLI 没有的命令：{missing}")

    def test_DC2_readme_hooks_doc_exists(self):
        self.assertTrue((PUBLIC / "p0" / "hooks-运维说明.md").exists(),
                        "README 引用的 hooks 运维说明必须存在")


if __name__ == "__main__":
    unittest.main()
