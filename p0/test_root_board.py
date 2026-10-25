#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""根看板（fish 全局）· 聚合视图（2026-10-25）。

## 用户的三条要求（原话）

> **根板接上，随子板刷新而刷新，且只显示"有效"看板**

## 为什么需要它

看板按日切 `period_key`，而**没有任何东西会关旧板**——`boards` 表里 open 板会
日复一日累积。于是"当前有几块有效看板"这个问题会越来越难回答。
根板就是那个**唯一回答它**的地方（`09` R5-3/R5-8）。

## 判据

  R1  **随子板刷新**：子板签到/渲染后，根板文件自动出现（不需要额外一步）
  R2  **只列 open**：子板被 close 后，根板不再列它
  R3  **只列今天**：昨天（其它 period_key）的板不出现——否则根板会变成历史板列表
  R4  **不递归**：根板自己不参与聚合（否则自己把自己装进去）
  R5  空态要明写（"今天还没有任何项目开工"），不能留白
"""
import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

CID = "01ROOTBOARD00000000000001"


class RootBoardTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (CID, "根板测试", "resident", "owner", None, "active", fish.now_utc()))
        for p in ("RA", "RB"):
            self.conn.execute(
                "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
                "VALUES (?,?,NULL,0,'active')", (p, p))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.root.parent, ignore_errors=True)

    def _root_path(self):
        b = fish.board_ensure(self.conn, "root", "")
        return self.root / b["path"]

    def _root_text(self):
        p = self._root_path()
        return p.read_text(encoding="utf-8") if p.exists() else ""

    # ------------------------------------------------------------------ R1
    def test_R1_root_refreshed_with_child(self):
        """子板签到后，根板**自动**出现（不需要额外一步）。"""
        self.assertFalse(self._root_path().exists(), "开始不该有根板")
        board = fish.board_checkin(self.conn, CID, "做 RA 的事",
                                   scope="project", scope_key="RA")
        fish.board_render(self.conn, self.root, board)
        self.assertTrue(self._root_path().exists(), "子板刷新后根板应自动出现")
        txt = self._root_text()
        self.assertIn("RA", txt, "根板应列出 RA")
        self.assertIn("做 RA 的事", txt, "根板应显示同项目里在做什么")

    # ------------------------------------------------------------------ R2
    def test_R2_closed_child_disappears(self):
        """子板 close 后，根板不再列它（只显示有效看板）。"""
        b1 = fish.board_checkin(self.conn, CID, "做 RA", scope="project", scope_key="RA")
        b2 = fish.board_checkin(self.conn, CID, "做 RB", scope="project", scope_key="RB")
        fish.board_render(self.conn, self.root, b1)
        fish.board_render(self.conn, self.root, b2)
        self.assertIn("RA", self._root_text())
        self.assertIn("RB", self._root_text())

        fish.board_close(self.conn, self.root, "project", "RA")
        # 刷新另一块板 → 根板跟着刷
        fish.board_render(self.conn, self.root, b2)
        txt = self._root_text()
        self.assertNotIn("RA", txt, "已归档的板不该出现在根板上")
        self.assertIn("RB", txt)

    # ------------------------------------------------------------------ R3
    def test_R3_other_period_excluded(self):
        """其它日期（period_key）的板不出现——否则根板会变成历史列表。"""
        old = fish.board_ensure(self.conn, "project", "OLDP", period_key="261024")
        _ = old
        cur = fish.board_checkin(self.conn, CID, "做今天的", scope="project", scope_key="RA")
        fish.board_render(self.conn, self.root, cur)
        txt = self._root_text()
        self.assertIn("RA", txt)
        self.assertNotIn("OLDP", txt, "昨天的板不该出现在今天的根板上")
        # 而且根板自己也只按今天的 period 建
        n = self.conn.execute(
            "SELECT COUNT(*) FROM boards WHERE scope='root'").fetchone()[0]
        self.assertEqual(n, 1, "根板一天一块")

    # ------------------------------------------------------------------ R4
    def test_R4_root_not_self_aggregating(self):
        """根板自己不参与聚合（不递归、不把自己列进去）。"""
        fish.board_render_root(self.conn, self.root)
        txt = self._root_text()
        self.assertNotIn("| `root", txt, "根板不该把自己列进表里")
        self.assertIn("今日有效看板（0 块）", txt)

    # ------------------------------------------------------------------ R5
    def test_R5_empty_state_is_explicit(self):
        """空态要明写，不能留白（留白会让读者以为功能坏了）。"""
        fish.board_render_root(self.conn, self.root)
        txt = self._root_text()
        self.assertIn("还没有任何项目开工", txt)
        self.assertIn("未登记 = 未发生", txt)
        self.assertIn("只列**今天", txt, "必须声明过滤条件，否则读者不知道为何没有旧板")

    # ------------------------------------------------------------------ 幂等
    def test_idempotent(self):
        """连渲两次内容一致（无时间戳漂移）。"""
        b = fish.board_checkin(self.conn, CID, "做 RA", scope="project", scope_key="RA")
        fish.board_render(self.conn, self.root, b)
        first = self._root_text()
        fish.board_render(self.conn, self.root, b)
        self.assertEqual(first, self._root_text(), "根板渲染应幂等")


if __name__ == "__main__":
    unittest.main(verbosity=2)
