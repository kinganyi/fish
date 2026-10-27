#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-2 判据：公开包导出脚本（排除规则 / 逐行一致 / 幂等 / 校验失败必红）。

## 为什么判据要跑"真导出"

导出脚本最危险的错误是**静默漏排除**（把 `data/` 或身份文件带进公开包）。
所以判据不用 mock：在临时目录造一棵带"雷"的源树，真跑脚本，再断言雷没被搬走。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "scripts" / "export_public.py"


def _child_env() -> dict:
    """子进程环境：**显式**把标准流定成 UTF-8（评审 🟡-2）。

    为什么必须是这里给而不是改脚本：导出脚本要印 `✗`（校验失败行）——那是它的语义，
    不该为了控制台编码改掉。而 cp936 控制台（本机默认 gb2312）下子进程的 stdout 是
    cp936，`print("✗ …")` 直接 `UnicodeEncodeError` 崩掉，EP3 于是变成"全量唯一红"。
    父进程哪怕自己是 UTF-8（`-X utf8` / `PYTHONIOENCODING=utf-8`）也不会传给子进程，
    所以环境要**显式**传（设 `PYTHONIOENCODING=utf-8` 后实测 5/5 OK）。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


class ExportPublicTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-export-"))
        self.src = self.tmp / "src"
        self.dest = self.tmp / "dest"
        # 源树：正常的代码/文档 + 5 颗"雷"
        (self.src / "tools").mkdir(parents=True)
        (self.src / "a.py").write_text("print('a')\n", encoding="utf-8")
        (self.src / "test_a.py").write_text("x = 1\n", encoding="utf-8")
        (self.src / "note.md").write_text("# 说明\n", encoding="utf-8")
        (self.src / "tools" / "t.py").write_text("y = 2\n", encoding="utf-8")
        (self.src / "data").mkdir()
        (self.src / "data" / "index.sqlite").write_text("secret\n", encoding="utf-8")
        (self.src / "_audit").mkdir()
        (self.src / "_audit" / "a.jsonl").write_text("{}\n", encoding="utf-8")
        (self.src / "历史版本").mkdir()
        (self.src / "历史版本" / "old.py").write_text("old\n", encoding="utf-8")
        (self.src / "_golden").mkdir()
        (self.src / "_golden" / "f.txt").write_text("g\n", encoding="utf-8")
        (self.src / "fish_identity.json").write_text("{}\n", encoding="utf-8")
        (self.src / "__pycache__").mkdir()
        (self.src / "__pycache__" / "a.cpython.pyc").write_text("x", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *extra):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--src", str(self.src),
             "--dest", str(self.dest), *extra],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=_child_env())

    def test_EP1_excludes_are_not_copied(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for must in ("a.py", "test_a.py", "note.md", "tools/t.py"):
            self.assertTrue((self.dest / must).exists(), f"应导出：{must}")
        for forbidden in ("data", "_audit", "历史版本", "_golden",
                          "fish_identity.json", "__pycache__"):
            self.assertFalse((self.dest / forbidden).exists(),
                             f"必须排除：{forbidden}")

    def test_EP2_second_export_is_idempotent(self):
        self.assertEqual(self._run().returncode, 0)
        first = sorted(p.relative_to(self.dest).as_posix() for p in self.dest.rglob("*")
                       if p.is_file())
        self.assertEqual(self._run().returncode, 0, "二次导出必须通过")
        second = sorted(p.relative_to(self.dest).as_posix() for p in self.dest.rglob("*")
                        if p.is_file())
        self.assertEqual(first, second, "二次导出不应改变文件集合")

    def test_EP3_check_only_detects_excluded_present(self):
        self.assertEqual(self._run().returncode, 0)
        # 人为在目标树塞一颗雷 → --check-only 必须红（exit 2）
        (self.dest / "data").mkdir()
        (self.dest / "data" / "leak.sqlite").write_text("leak\n", encoding="utf-8")
        proc = self._run("--check-only")
        self.assertEqual(proc.returncode, 2, "目标树出现 data/ 必须校验失败")
        self.assertIn("excluded-present", proc.stdout)
        # 编码判据（评审 🟡-2）：失败行是 `✗ …`，cp936 控制台下子进程会直接
        # `UnicodeEncodeError` 崩掉（退出码变 1、一行都不印）。这两条把它钉住：
        # 既要真的印出来，又不许以 traceback 结尾。
        self.assertIn("✗", proc.stdout, "校验失败行没印出来（子进程编码崩了）")
        self.assertNotIn("UnicodeEncodeError", proc.stderr)

    def test_EP4_report_has_counts_and_no_errors(self):
        report = self.tmp / "report.json"
        self.assertEqual(self._run("--report", str(report)).returncode, 0)
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(data["expected_files"], 4)
        self.assertEqual(data["errors"], [])
        self.assertEqual(data["recorded_files"], 4)

    def test_EP5_source_tree_missing_fails(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--src", str(self.tmp / "nope"),
             "--dest", str(self.dest)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 1, "源目录不存在应 exit 1")


if __name__ == "__main__":
    unittest.main()
