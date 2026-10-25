#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看板 · 外部评估第二批（13/14/15）的修复判据（2026-10-25）。

## 来源

外部 AI 在 2026-10-25 19:15–19:22（**就在我们第一批修复做完之后**）又写了 4 条，
其中 3 条是**我们刚引入的 bug**：

  EVAL-261025-13  重叠检测未过滤 idle/left 行 → 警告掺假 + 活跃工被挤出注入包
  EVAL-261025-14  软门按「记忆的项目」判定（跨项目必误报）+ 不看活跃度（签到一次顶一天）
  EVAL-261025-15  裁剪顺序与自己的注释矛盾

## 判据

  N1  重叠检测**只算有效行**（left 与超窗 idle 都不算）
  N2  残留行**单独提示**、不叫"冲突"、也不被无痕删除
  N3  注入包排序：**活跃 > 有重叠 > 其余**（否则"看得见更多行"= "看见错的行"）
  S1  软门**跨项目不误报**（在 A 签到、写 B 的记忆 → 不提示）
  S2  软门**超窗提醒**（签到很久没更新 doing → 提示刷新，而不是沉默）
  S3  `status='left'` 有**入口**（CLI `--status` 与 MCP schema 都有）
  T1  裁剪链：**看板排在错题本/规矩之后**（与"可离线重建的让位于不可重建的"一致）
"""
import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

import mcp_server  # noqa: E402


class BoardFeedback2Test(unittest.TestCase):

    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.root = self.base / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        for p in ("AA", "BB"):
            self.conn.execute(
                "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
                "VALUES (?,?,NULL,0,'active')", (p, p))
        self.conn.commit()
        self.srv = mcp_server.FishServer(self.root)
        self.anon = self.srv._resolve_identity(None)
        self._drop = {v for _h, v in fish.HOST_SESSION_ENVS} | set(fish.SESSION_ALIAS_ENVS)
        self._clean = {k: v for k, v in os.environ.items() if k not in self._drop}

    def tearDown(self):
        try:
            self.srv.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.base, ignore_errors=True)

    def _ident(self, name):
        import re
        with mock.patch.dict(os.environ, self._clean, clear=True):
            o = self.srv.tool_session_start({"session_name": name}, self.anon)
        tok = re.search(r"token:\s*(fsh_\S+)", o).group(1)
        return self.srv._resolve_identity(tok)

    def _checkin(self, ident, doing, surface, project="AA", status="active"):
        return self.srv.tool_board({"action": "checkin", "doing": doing,
                                    "surface": surface, "project": project,
                                    "status": status}, ident)

    def _age(self, ident, project, minutes):
        """把某人的 last_seen_at 往前拨 N 分钟（模拟"很久没动"）。"""
        import datetime
        board = fish.board_ensure(self.conn, "project", project)
        t = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.conn.execute(
            "UPDATE presence SET last_seen_at=? WHERE board_id=? AND citizen_id=?",
            (t, board["id"], ident.citizen_id))
        self.conn.commit()

    # ------------------------------------------------------------------ N1
    def test_N1_left_rows_excluded_from_overlap(self):
        """**left（已下板）不算重叠**——这是 `EVAL-261025-13` 的主诉。"""
        a, b = self._ident("A"), self._ident("B")
        self._checkin(a, "A 的活", "p0/fish.py")
        self._checkin(b, "B 的活", "p0/fish.py")
        board = fish.board_ensure(self.conn, "project", "AA")
        self.assertEqual(len(fish.board_overlaps(self.conn, board)), 1, "先确认真有重叠")
        # B 下板
        self._checkin(b, "下板了", "p0/fish.py", status="left")
        self.assertEqual(fish.board_overlaps(self.conn, board), [],
                         "已下板的人不该再进重叠检测")

    def test_N1_stale_rows_excluded_from_overlap(self):
        """**超窗（>90 分钟）的行不算重叠**——"开工前先协调"对走开三小时的人是错话。"""
        a, b = self._ident("A2"), self._ident("B2")
        self._checkin(a, "A 的活", "p0/fish.py")
        self._checkin(b, "B 的活", "p0/fish.py")
        board = fish.board_ensure(self.conn, "project", "AA")
        self.assertEqual(len(fish.board_overlaps(self.conn, board)), 1)
        self._age(b, "AA", 120)                 # B 已 2 小时没动
        self.assertEqual(fish.board_overlaps(self.conn, board), [],
                         "超窗的行不该再报重叠")

    def test_N1_within_window_still_counts(self):
        """**窗口内（60 分钟）仍算重叠**——不能因为过滤把真冲突也漏掉。"""
        a, b = self._ident("A3"), self._ident("B3")
        self._checkin(a, "A 的活", "p0/fish.py")
        self._checkin(b, "B 的活", "p0/fish.py")
        self._age(b, "AA", 60)                  # 60 < 90，仍有效
        board = fish.board_ensure(self.conn, "project", "AA")
        self.assertEqual(len(fish.board_overlaps(self.conn, board)), 1,
                         "60 分钟仍在窗口内（窗口是 90）")

    # ------------------------------------------------------------------ N2
    def test_N2_stale_rows_reported_separately(self):
        """残留行**单独提示**、不叫"冲突"（既不误报也不无痕删除）。"""
        a, b = self._ident("A4"), self._ident("B4")
        self._checkin(a, "A 的活", "p0/fish.py")
        self._checkin(b, "B 的活", "p0/fish.py")
        self._age(b, "AA", 120)
        board = fish.board_ensure(self.conn, "project", "AA")
        stale = fish.board_stale_rows(self.conn, board)
        self.assertEqual(len(stale), 1, f"应报 1 行残留：{stale}")
        self.assertEqual(stale[0]["citizen_id"], b.citizen_id)
        self.assertGreater(stale[0]["minutes"], 90)
        # 注入包里：重叠段没有，但有残留提示
        block = fish.board_block(self.conn, "AA")
        self.assertNotIn("占用面重叠", block, "超窗行不该进重叠段")
        self.assertIn("不算", block, f"应有一句轻提示说明它们不算重叠：{block[:400]}")

    # ------------------------------------------------------------------ N3
    def test_N3_ordering_active_first(self):
        """排序：**活跃 > 有重叠 > 其余**——否则残留行会把活跃工挤出前 5。"""
        # 3 个残留行 + 2 个活跃工，只有活跃工之间才真重叠
        stale_ids = []
        for i in range(3):
            who = self._ident(f"stale-{i}")
            self._checkin(who, f"早就走了 {i}", "p0/fish.py")
            self._age(who, "AA", 180)
            stale_ids.append(who.citizen_id)
        a, b = self._ident("live-A"), self._ident("live-B")
        self._checkin(a, "真在干活 A", "p0/other.py")
        self._checkin(b, "真在干活 B", "p0/other.py")
        block = fish.board_block(self.conn, "AA")
        # 两个活跃工必须都在注入包里（旧实现会被 3 个残留行挤出前 5）
        self.assertIn(a.citizen_id, block, f"活跃工 A 被挤出去了：{block[:500]}")
        self.assertIn(b.citizen_id, block, f"活跃工 B 被挤出去了：{block[:500]}")

    # ------------------------------------------------------------------ S1
    def test_S1_soft_gate_no_cross_project_false_alarm(self):
        """**跨项目不误报**：在 AA 签到、写 BB 的记忆 → 不该提示"还没签到"。

        这是 `EVAL-261025-14` 的现场：19:16 在 anyi-studio 签到，
        19:17 写 EVAL 就报"你还没签到"。
        """
        who = self._ident("cross")
        self._checkin(who, "在 AA 干活", "p0/x.py", project="AA")
        out = self.srv.tool_remember({"project": "BB", "title": "跨项目写一条"}, who)
        self.assertNotIn("你还没签到", out, f"跨项目写不该误报：{out[-300:]}")

    def test_S1_soft_gate_still_warns_when_truly_absent(self):
        """软门功能不能"修误报修没了"——现在的形态是**超时提醒**。

        2026-10-25 改（`EVAL-261025-16` 建议 3 落地）：`remember` 会自动兜底登记，
        所以"完全没行"这个分支不再出现；软门的价值转到"有行但 doing 过期"上。
        这条判据钉的就是：**兜底之后它仍然在工作**。
        """
        who = self._ident("absent")
        out = self.srv.tool_remember({"project": "BB", "title": "没签到就写"}, who)
        board = fish.board_ensure(self.conn, "project", "BB")
        row = self.conn.execute(
            "SELECT doing FROM presence WHERE board_id=? AND citizen_id=?",
            (board["id"], who.citizen_id)).fetchone()
        self.assertIsNotNone(row, "应已自动登记")
        self.assertIn("自动登记", row[0])
        # 把它拨到 2 小时前 → 软门应提醒"没更新"
        self._age(who, "BB", 120)
        ok, msg = fish.board_checkin_or_remind(self.conn, who.citizen_id, "BB")
        self.assertFalse(ok)
        self.assertIn("分钟没更新", msg)

    # ------------------------------------------------------------------ S2
    def test_S2_soft_gate_warns_when_stale(self):
        """**超窗提醒**：签到很久没更新 doing → 提示刷新（不是沉默）。"""
        who = self._ident("stale-gate")
        self._checkin(who, "一件早就做完的事", "p0/x.py", project="AA")
        self._age(who, "AA", 120)
        ok, msg = fish.board_checkin_or_remind(self.conn, who.citizen_id, "AA")
        self.assertFalse(ok, "超窗时不该算'已签到'")
        self.assertIsNotNone(msg)
        self.assertIn("分钟没更新", msg, f"提示应说清是'没更新'而非'没签到'：{msg}")

    def test_S2_soft_gate_ok_when_fresh(self):
        who = self._ident("fresh-gate")
        self._checkin(who, "正在干", "p0/x.py", project="AA")
        ok, msg = fish.board_checkin_or_remind(self.conn, who.citizen_id, "AA")
        self.assertTrue(ok)
        self.assertIsNone(msg)

    # ------------------------------------------------------------------ S3
    def test_S3_left_has_entry_points(self):
        """`status='left'` 必须有**入口**（CLI 与 MCP 都要有），否则它只是 DDL 里的词。"""
        # MCP schema
        tool = next(t for t in mcp_server.TOOLS if t["name"] == "board")
        props = tool["inputSchema"]["properties"]
        self.assertIn("status", props, "MCP board 缺 status 入参")
        self.assertEqual(set(props["status"]["enum"]), {"active", "left"})
        # 实际可用
        who = self._ident("leaver")
        out = self._checkin(who, "收工", "p0/x.py", status="left")
        self.assertIn("已下板", out)
        board = fish.board_ensure(self.conn, "project", "AA")
        st = self.conn.execute(
            "SELECT status FROM presence WHERE board_id=? AND citizen_id=?",
            (board["id"], who.citizen_id)).fetchone()[0]
        self.assertEqual(st, "left")

    def test_S3_cli_has_status_flag(self):
        """CLI 的 `board checkin` 也要有 `--status`。"""
        import subprocess
        r = subprocess.run(["python", "fish.py", "board", "checkin", "--help"],
                           capture_output=True, text=True, encoding="utf-8",
                           cwd=str(Path(__file__).parent))
        self.assertIn("--status", r.stdout, f"CLI 缺 --status：{r.stdout[-300:]}")
        self.assertIn("left", r.stdout)

    # ------------------------------------------------------------------ T1
    def test_T1_board_trimmed_after_lesson_and_rules(self):
        """裁剪链：**看板排在错题本/规矩之后**（与注释的理由一致）。

        `EVAL-261025-15`：上一版注释说"看板该比这三者都活得久"，
        代码却只比注意事项久——同一份文件两种依据。
        """
        who = self._ident("trim")
        self._checkin(who, "干活", "p0/x.py", project="AA")
        _pack, meta = fish.build_injection_pack(
            self.conn, self.root, who.citizen_id, budget=40, project="AA")
        t = meta["trimmed"]
        self.assertIn("看板", t, f"极小预算时看板终归会被裁：{t}")
        for other in ("错题本", "规矩"):
            if other in t:
                self.assertLess(t.index(other), t.index("看板"),
                                f"{other} 应先于看板被裁，实际顺序={t}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
