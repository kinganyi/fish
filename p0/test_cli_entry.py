#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""CLI 入口判据（FB-13 的 2026-11-11 重构收口）。

## 这组判据守什么

`install_cli.py` 曾是**单文件 .cmd**：把 `fish.py` 的绝对路径直接写进批处理，按
`mbcs`(cp936) 保存。注释写着"`.cmd` 由 Windows cmd 按 ANSI 读取"——**这个假设在本机
是错的**：本机 ANSI=936、OEM=936，但 cmd 实际按 **UTF-8** 读批处理，于是仓库路径里的
`大肥鱼生命系统` 变成 `���������ϵͳ`，python 报
`can't open file '...\p0\fish.py': [Errno 2]`。

那个报错**长得像"文件不存在"**，所以很容易被误判成"项目目录被删了"或"shim 指向旧路径"，
一路查到别处去。真正的根因是**转码**，而且它无法靠"选一个正确的代码页"解决——
不同机器/不同 Windows 版本的 cmd 读 .cmd 的代码页并不一致，任何写死的编码都会在
某一类机器上炸。

## 现在的形态（两条不变式）

    fish.cmd        纯 ASCII，只有 `@python "%~dp0fish_launch.py" %*`
    fish_launch.py  UTF-8，TARGET 逐字等于真实 fish.py

不变式 A：**shim 里一个非 ASCII 字符都没有** → 任何代码页读出来都一样。
不变式 B：**shim 里没有绝对路径**（用 `%~dp0`）→ 项目搬家不会让 shim 失效。

判据分三组：
  · 名字一致（`install_cli` 与 `fish` 各存一份文件名，改名不能一漏一改）
  · 回执状态感知（装了说"已就绪"，没装才说"去装"——装了却不说等于没装）
  · 回执**不许**把撞名的其它 `fish` 程序当成自己的（那比不说更误导）
"""
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish          # noqa: E402
import install_cli   # noqa: E402


class NamesAgreeTest(unittest.TestCase):
    """两个模块各存一份文件名（`install_cli` 不能 import `fish`——那会把整个核心拖进来）。"""

    def test_shim_name_matches(self):
        self.assertEqual(install_cli.SHIM_NAME, fish.CLI_ENTRY_SHIM,
                         "两个模块的 shim 名不一致：改名时漏了一处")

    def test_launcher_name_matches(self):
        self.assertEqual(install_cli.LAUNCHER_NAME, fish.CLI_ENTRY_LAUNCHER,
                         "两个模块的启动器名不一致：改名时漏了一处")

    def test_shim_content_references_launcher_by_name(self):
        """shim 必须按**当前**启动器名转发（名字改了 shim 却还指旧名 = 一敲就崩）。"""
        content = install_cli.shim_content()
        self.assertIn(f"%~dp0{install_cli.LAUNCHER_NAME}", content)


class HintStateAwareTest(unittest.TestCase):
    """`cli_entry_hint()` 必须按实况说话。"""

    def _hint(self, found, beside_exists):
        """把 `shutil.which` 与 `Path.exists` 都换成受控值，只看分支逻辑。"""
        fake = Path(tempfile.mkdtemp(prefix="fish-hint-")) / "fish.cmd"
        with mock.patch.object(shutil, "which", return_value=str(fake) if found else None), \
                mock.patch.object(Path, "exists", return_value=beside_exists):
            return fish.cli_entry_hint()

    def test_not_installed_points_to_installer(self):
        hint = self._hint(found=False, beside_exists=False)
        self.assertIn("install_cli.py --apply", hint, "没装时必须告诉人怎么装")
        self.assertIn("fish.py", hint, "没装时要给出可直接复制粘贴的 python 路径")
        self.assertNotIn("已就绪", hint, "没装却说已就绪 = 撒谎")

    def test_installed_says_ready(self):
        hint = self._hint(found=True, beside_exists=True)
        self.assertIn("已就绪", hint,
                      "**装了却不说**，AI 会以为只能用 MCP 工具、继续绕路")
        self.assertIn("fish --help", hint, "要给出验证命令")

    def test_foreign_fish_program_is_not_ours(self):
        """PATH 上有个同名 `fish.exe`、旁边没有 `fish_launch.py` → 不许报"已就绪"。

        撞名的情形真实存在（别的工具也可能叫 fish）。把别人的程序报成"我们的已就绪"，
        比什么都不说更糟：AI 会去敲它，拿到不相干的输出。
        """
        hint = self._hint(found=True, beside_exists=False)
        self.assertNotIn("已就绪", hint, "撞名程序被误认成 fish 了")
        self.assertIn("install_cli.py --apply", hint)


class RealEnvironmentTest(unittest.TestCase):
    """本机实况：装好了就该报已就绪（这条在没装 shim 的机器上会红——那是**正确**的红）。"""

    def test_hint_reflects_this_machine(self):
        installed = shutil.which("fish")
        beside = None
        if installed:
            beside = Path(installed).resolve().parent / fish.CLI_ENTRY_LAUNCHER
        hint = fish.cli_entry_hint()
        if installed and beside is not None and beside.exists():
            self.assertIn("已就绪", hint, f"本机已装却没报已就绪：{hint}")
        else:
            self.assertNotIn("已就绪", hint, f"本机没装却报已就绪：{hint}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
