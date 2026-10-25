#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看板归档：项目关闭联动 + 按日 sweep（2026-10-25 · 用户要求 C）。

## 背景

看板按日切 `period_key`，而**没有任何东西会关旧板**——`boards` 表里 open 板会
日复一日累积。根板虽然只列今天（过滤掉了），但那是**掩盖**，不是**解决**。

用户 2026-10-25 选 C：两个都做。

  A  `project.closed` 时自动归档该项目所有 open 板（**联动**）
  B  `board sweep`：把 N 天前的 open 板自动归档（**按日**）

## 判据

  C1  项目 close → 该项目的 open 板全部变 closed（联动）
  C2  项目 reopen → 看板**不会**自动 reopen（看板是"那一刻"的快照，不该被复活）
  C3  sweep 语义：`--days N` = **保留最近 N 天（含今天）**
  C4  sweep 边界：`--days 7` 会把"今天-7 天"那块也归档（不是留在边界外）
  C5  sweep 不动已 closed 的（`closed_at` 不被刷新——它是第一次归档的时刻）
  C6  sweep **不删**任何东西（R5-7：不合并、不压缩、不删除）
  C7  sweep 后根板跟着刷新
"""
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

CID = "01SWEEPTEST00000000000001"


class BoardArchiveTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (CID, "归档测试", "resident", "owner", None, "active", fish.now_utc()))
        for p in ("PA", "PB"):
            self.conn.execute(
                "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
                "VALUES (?,?,NULL,0,'active')", (p, p))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.root.parent, ignore_errors=True)

    def _status(self, scope, key, pk=None):
        pk = pk or fish.board_period()
        return self.conn.execute(
            "SELECT status FROM boards WHERE scope=? AND scope_key=? AND period_key=?",
            (scope, key, pk)).fetchone()

    # ------------------------------------------------------------------ C1
    def test_C1_project_close_archives_boards(self):
        """项目 close → 该项目的 open 板全部归档（联动）。"""
        fish.board_checkin(self.conn, CID, "干活", scope="project", scope_key="PA")
        self.assertEqual(self._status("project", "PA")[0], "open")
        res = fish.apply_project_status(self.conn, "PA", "closed", reason="测试")
        self.assertEqual(res["boards_closed"], 1, f"应归档 1 块，实际 {res}")
        self.assertEqual(self._status("project", "PA")[0], "closed")

    def test_C1_close_does_not_touch_other_projects(self):
        """关 PA 不影响 PB 的板。"""
        fish.board_checkin(self.conn, CID, "A 的活", scope="project", scope_key="PA")
        fish.board_checkin(self.conn, CID, "B 的活", scope="project", scope_key="PB")
        fish.apply_project_status(self.conn, "PA", "closed", reason="测试")
        self.assertEqual(self._status("project", "PA")[0], "closed")
        self.assertEqual(self._status("project", "PB")[0], "open", "PB 的板不该被动")

    # ------------------------------------------------------------------ C2
    def test_C2_reopen_does_not_revive_boards(self):
        """reopen 项目**不会**把看板一起复活——看板是"那一刻"的快照。"""
        fish.board_checkin(self.conn, CID, "干活", scope="project", scope_key="PA")
        fish.apply_project_status(self.conn, "PA", "closed", reason="测试")
        fish.apply_project_status(self.conn, "PA", "active", reason="测试")
        self.assertEqual(self._status("project", "PA")[0], "closed",
                         "看板不该被 reopen 自动复活（它记录的是已归档的那一天）")

    # ------------------------------------------------------------------ C3/C4
    def test_C3_C4_sweep_semantics(self):
        """`--days N` = 保留最近 N 天（含今天）；第 N 天前那块要归档。"""
        today = fish.board_period()
        ymd = today
        # 造：今天 / 3 天前 / 7 天前 / 8 天前
        import datetime
        d0 = datetime.datetime.strptime("20" + ymd, "%Y%m%d").date()
        pks = {}
        for offset in (0, 3, 7, 8):
            pk = (d0 - datetime.timedelta(days=offset)).strftime("%y%m%d")
            pks[offset] = pk
            fish.board_ensure(self.conn, "project", "PA", period_key=pk)

        r7 = fish.board_sweep(self.conn, self.root, days=7)
        self.assertEqual(r7["cutoff"], pks[7], "截止日应是今天-7")
        got = {b["period_key"] for b in r7["closed"]}
        self.assertIn(pks[8], got, "8 天前应归档")
        self.assertIn(pks[7], got, "**7 天前（边界）也应归档**——`--days 7` 是保留 7 天")
        self.assertNotIn(pks[3], got, "3 天前不该归档")
        self.assertNotIn(pks[0], got, "今天不该归档")

    # ------------------------------------------------------------------ C5
    def test_C5_closed_at_not_refreshed(self):
        """重复 sweep 不该刷新已 closed 板的 `closed_at`（它是第一次归档的时刻）。"""
        import datetime
        d0 = datetime.datetime.now().date() - datetime.timedelta(days=30)
        pk = d0.strftime("%y%m%d")
        fish.board_ensure(self.conn, "project", "PA", period_key=pk)
        fish.board_sweep(self.conn, self.root, days=7)
        first = self.conn.execute(
            "SELECT closed_at FROM boards WHERE period_key=?", (pk,)).fetchone()[0]
        self.assertIsNotNone(first)
        fish.board_sweep(self.conn, self.root, days=7)
        second = self.conn.execute(
            "SELECT closed_at FROM boards WHERE period_key=?", (pk,)).fetchone()[0]
        self.assertEqual(first, second, "closed_at 被重复 sweep 刷新了")

    # ------------------------------------------------------------------ C6
    def test_C6_sweep_deletes_nothing(self):
        """sweep 只改 status，**不删任何行**（R5-7）。"""
        import datetime
        d0 = datetime.datetime.now().date() - datetime.timedelta(days=30)
        pk = d0.strftime("%y%m%d")
        b = fish.board_ensure(self.conn, "project", "PA", period_key=pk)
        fish.board_checkin(self.conn, CID, "老活", scope="project", scope_key="PA",
                           period_key=pk)
        n_boards = self.conn.execute(
            "SELECT COUNT(*) FROM boards WHERE scope='project'").fetchone()[0]
        n_presence = self.conn.execute("SELECT COUNT(*) FROM presence").fetchone()[0]
        fish.board_sweep(self.conn, self.root, days=7)
        # 只数项目板：sweep 会刷根板，而**建根板是正常行为**（它是聚合视图），
        # 不该被误读成"sweep 删了/加了行"（2026-10-25 判据 C6 初版就是这么红的）。
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM boards WHERE scope='project'").fetchone()[0],
            n_boards, "sweep 不该删看板行")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM presence").fetchone()[0], n_presence,
            "sweep 不该删在场行")
        # 归档后仍然读得到
        info = fish.board_show(self.conn, "project", "PA", pk)
        self.assertEqual(info["board"]["status"], "closed")
        self.assertTrue(info["presence"], "归档后在场记录仍可读")

    # ------------------------------------------------------------------ C7
    def test_C7_sweep_refreshes_root(self):
        """sweep 归档后根板跟着刷新（有效集合变了）。"""
        import datetime
        d0 = datetime.datetime.now().date() - datetime.timedelta(days=30)
        pk = d0.strftime("%y%m%d")
        fish.board_ensure(self.conn, "project", "PA", period_key=pk)
        # 今天也开一块，让根板有内容。
        # **必须显式 render**：`board_checkin` 只写表、不渲染（渲染是独立一步，
        # 见 `board_render` 的 docstring）——所以光签到不会产出根板文件。
        b = fish.board_checkin(self.conn, CID, "今天的", scope="project", scope_key="PB")
        fish.board_render(self.conn, self.root, b)
        root_b = fish.board_ensure(self.conn, "root", "")
        root_p = self.root / root_b["path"]
        self.assertIn("今日有效看板（1 块）", root_p.read_text(encoding="utf-8"))
        fish.board_sweep(self.conn, self.root, days=7)
        txt = root_p.read_text(encoding="utf-8")
        self.assertIn("今日有效看板（1 块）", txt, "今天的板仍在")
        self.assertNotIn(pk, txt, "被 sweep 的旧板不该突然出现在根板上")


if __name__ == "__main__":
    unittest.main(verbosity=2)
