#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workspace_map 路径归一化的判据（2026-10-24 自查补）。

背景
----
S4 交付的 `normalize_workspace_path` 只做了「正斜杠 / 无尾斜杠 / 全小写」，
**漏了 `.` 与 `..` 归一化**，于是：

    resolve_project_by_cwd('/a/b/../bc')  →  'PROJ-AB'   ← 错
    实际 /a/b/../bc == /a/bc，不是 /a/b 的子路径，应该 None

修复过程中还踩了两个顺序/大小写坑，都在这份判据里钉住：
  · `posixpath.normpath('C:/')` == 'C:' → 会丢掉盘符根的斜杠；
  · 补回 `C:/` 的 if 若在去尾斜杠的 while **之前**，补的 `/` 会被立刻砍掉；
  · 盘符正则若只写 `[a-z]`，大写 `C:` 匹配不上（此时还没 lower）。

判据
----
N1  .. 归一化：`/a/b/../bc` 归一成 `/a/bc`，且 `resolve` 返回 None
N2  . 归一化：`/a/./b` 归一成 `/a/b`
N3  盘符根：`C:\` / `C:/` / `c:` 都归一成 `c:/`（不是 `c:`）
N4  POSIX 根：`/` 与 `//` 归一成 `/`
N5  真实路径：反斜杠 + 尾斜杠的 Windows 绝对路径
N6  空值：None / 空串 / 纯空白 → 空串
"""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fish


class WorkspaceNormalizeTest(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)

    def tearDown(self):
        self.conn.close()

    # ------------------------------------------------------------------ N1
    def test_N1_dotdot_normalized_and_not_matched(self):
        """`..` 必须归一化，且归一化后**不能**误匹配更短的映射。"""
        self.assertEqual(
            fish.normalize_workspace_path("/a/b/../bc"), "/a/bc",
            "`..` 没归一化——旧的裸字符串前缀匹配会把它当成 /a/b 的子路径")

        fish.set_workspace_mapping(self.conn, "/a/b", "PROJ-AB")
        self.assertIsNone(
            fish.resolve_project_by_cwd(self.conn, "/a/b/../bc"),
            "/a/b/../bc 实际是 /a/bc，不是 /a/b 的子路径，必须返回 None")

    # ------------------------------------------------------------------ N2
    def test_N2_dot_normalized(self):
        """`.` 必须移除。"""
        self.assertEqual(fish.normalize_workspace_path("/a/./b"), "/a/b")
        fish.set_workspace_mapping(self.conn, "/a/b", "PROJ-AB")
        self.assertEqual(
            fish.resolve_project_by_cwd(self.conn, "/a/b/./c"),
            "PROJ-AB", "`.` 归一化后仍是 /a/b/c，应命中")

    # ------------------------------------------------------------------ N3
    def test_N3_drive_root_keeps_slash(self):
        """盘符根必须保留 `c:/`，不能塌成 `c:`。"""
        for raw in ("C:\\", "C:/", "c:"):
            self.assertEqual(
                fish.normalize_workspace_path(raw), "c:/",
                f"{raw!r} 归一化丢了盘符根斜杠（posixpath.normpath 会去掉它，"
                f"且补回后不能再被去尾斜杠的循环砍掉）")

    # ------------------------------------------------------------------ N4
    def test_N4_posix_root(self):
        self.assertEqual(fish.normalize_workspace_path("/"), "/")
        self.assertEqual(fish.normalize_workspace_path("//"), "/")

    # ------------------------------------------------------------------ N5
    def test_N5_windows_absolute_path(self):
        self.assertEqual(
            fish.normalize_workspace_path("I:\\app\\DSH-Home\\workspace\\learn\\"),
            "i:/app/dsh-home/workspace/learn")

    # ------------------------------------------------------------------ N6
    def test_N6_empty_values(self):
        for raw in (None, "", "   "):
            self.assertEqual(fish.normalize_workspace_path(raw), "",
                             f"{raw!r} 应归一成空串")


if __name__ == "__main__":
    unittest.main(verbosity=2)
