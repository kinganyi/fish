#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 判据（外部审查 🔴6）：路径穿越。

T-1 `body_relpath` 拒绝路径分隔符 / 点段 / 控制字符（`projects/../../outside` 曾可逃出 root）
T-2 正常项目名（含中文）仍然工作
T-3 `tool_add_project` 拒绝非法 prefix（静态 + 动态双查）
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fish          # noqa: E402
import mcp_server    # noqa: E402


class PathTraversalTest(unittest.TestCase):

    # T-1
    def test_T1_body_relpath_rejects_traversal(self):
        bad = ("../../outside", "a/b", "a\\b", "..", ".", "a\x00b", "a:b", "")
        for project in bad:
            with self.assertRaises(ValueError, msg=f"应拒绝 {project!r}"):
                fish.body_relpath(project, "X-261028-01", "t")

    # T-2
    def test_T2_valid_project_names_still_work(self):
        self.assertEqual(
            fish.body_relpath("大肥鱼生命系统", "X-261028-01", "t"),
            "projects/大肥鱼生命系统/X-261028-01__t.md")
        self.assertEqual(
            fish.body_relpath("anyi-studio", "ANYI-1", "t"),
            "projects/anyi-studio/ANYI-1__t.md")

    # T-3
    def test_T3_add_project_rejects_bad_prefix(self):
        tmp = Path(tempfile.mkdtemp(prefix="fish-traversal-"))
        try:
            conn = fish.connect(tmp)
            conn.executescript(fish.SCHEMA)
            fish.migrate(conn)
            srv = mcp_server.FishServer.__new__(mcp_server.FishServer)
            srv.conn = conn
            from types import SimpleNamespace
            ident = SimpleNamespace(citizen_id="test-citizen",
                                    evidence="self-reported", session_key="self:test")
            for bad in ("../x", "a/b", "a\\b", "..", "a\x00b"):
                with self.assertRaises(ValueError, msg=f"应拒绝 prefix={bad!r}"):
                    srv.tool_add_project({"prefix": bad}, ident)
            # 正常 prefix 仍能登记
            srv.tool_add_project({"prefix": "XY", "namespace": "正常项目"}, ident)
            self.assertTrue(conn.execute(
                "SELECT 1 FROM code_registry WHERE prefix='XY'").fetchone())
            conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
