#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 面的看板工具 `board`（2026-10-25）。

背景
----
看板原来**看得见、动不了**：注入包会说「你还没签到」，但 MCP 面 15 个工具里
**没有一个是能签到的**——只能去跑 CLI。签到闭环断在这里，所以
`09` R5-2「未登记时提示」只是半条链路：提示有了，动作没有。

用户 2026-10-25 裁定补上（工具数 15 → 16）。

判据
----
  P1  `show` **不需要凭证**（看是只读的；要求凭证等于逼 AI 在"先看"和"先签"之间二选一，
      而正确顺序是**先看有没有人占着**，再签）
  P2  `checkin` **必须凭证**（看板上要记'是谁'；记成 unattributed 会造出"有人在动但
      不知道是谁"的幽灵行，别人照样无法判断会不会撞车）
  P3  `doing` 必填（没有 doing 的在场行等于没签到）
  P4  checkin 之后 show 能看到自己（端到端闭环）
  P5  同一人重复 checkin 只有一行（只写自己行，不覆盖别人）
"""
import importlib.util
import re
import shutil
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

import mcp_server  # noqa: E402


class BoardToolTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "data"
        self.srv = mcp_server.FishServer(self.root)
        self.anon = self.srv._resolve_identity(None)
        self.valid = self._make_ident("board-dev")

    def tearDown(self):
        try:
            self.srv.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.root.parent, ignore_errors=True)

    def _make_ident(self, session_name):
        """在**模拟真实 MCP 进程的环境**下签发一个身份。

        真实 MCP 进程里，宿主会把 `DSH_SESSION_ID` 等宿主会话 env **scrub 掉**，
        所以 `build_session_identity` 走的是 `alias`（self-reported）档。
        测试进程里这些 env 是有的——不清掉的话两次调用会因为同一个 `DSH_SESSION_ID`
        落到同一个公民上，把"不同 session_name = 不同公民"这条夹具做废
        （2026-10-25 判据 P5 就是这么红的：两个人只写了一行）。
        """
        import os
        from unittest import mock
        drop = {v for _h, v in fish.HOST_SESSION_ENVS} | set(fish.SESSION_ALIAS_ENVS)
        clean = {k: v for k, v in os.environ.items() if k not in drop}
        with mock.patch.dict(os.environ, clean, clear=True):
            out = self.srv.tool_session_start({"session_name": session_name}, self.anon)
        tok = re.search(r"token:\s*(fsh_\S+)", out).group(1)
        return self.srv._resolve_identity(tok)

    # ------------------------------------------------------------------ P1
    def test_P1_show_needs_no_token(self):
        """无凭证也能看板——看是只读的。"""
        out = self.srv.tool_board({"action": "show", "project": "P1"}, self.anon)
        self.assertIn("看板", out)
        self.assertIn("在场", out)
        # 空看板要明写，不能留白
        self.assertIn("还没有人签到", out)
        self.assertIn("未登记 = 未发生", out)

    # ------------------------------------------------------------------ P2
    def test_P2_checkin_requires_token(self):
        """签到必须带凭证——看板上要记'是谁'。"""
        with self.assertRaises(ValueError) as ctx:
            self.srv.tool_board(
                {"action": "checkin", "doing": "干活", "project": "P1"}, self.anon)
        msg = str(ctx.exception)
        self.assertIn("凭证", msg)
        self.assertIn("session_start", msg, "错误信息要给出拿到凭证的路径")

    # ------------------------------------------------------------------ P3
    def test_P3_doing_required(self):
        """doing 必填——没有 doing 的在场行等于没签到。"""
        with self.assertRaises(ValueError) as ctx:
            self.srv.tool_board(
                {"action": "checkin", "doing": "  ", "project": "P1"}, self.valid)
        self.assertIn("doing", str(ctx.exception))

    # ------------------------------------------------------------------ P4
    def test_P4_checkin_then_show(self):
        """端到端：签到之后看板能看到自己（**可读名**，不是 ULID）。"""
        out = self.srv.tool_board(
            {"action": "checkin", "doing": "做看板工具",
             "surface": "p0/mcp_server.py", "project": "P1"}, self.valid)
        self.assertIn("已签到", out)
        self.assertIn("做看板工具", out)
        shown = self.srv.tool_board({"action": "show", "project": "P1"}, self.valid)
        # 2026-10-26（T03 契约变更）：人类可读面用可读名，**不许**出现 26 位公民 ID。
        self.assertNotIn(self.valid.citizen_id, shown,
                         "看板渲染不许出现公民 ULID（用户拍板：不要机器码）")
        self.assertIn("board-dev", shown, "看板上应出现可读名")
        self.assertIn("做看板工具", shown, "看板上应出现 doing")
        self.assertIn("p0/mcp_server.py", shown, "看板上应出现占用面")

    # ------------------------------------------------------------------ P5
    def test_P5_checkin_idempotent_per_citizen(self):
        """同一人重复签到只有一行；**不覆盖别人**（只写自己行）。

        ⚠️ 2026-10-26（T03）：`tool_session_start` 现在会**自动签到**（自动行算登记），
        所以"总行数 == 2"这个断言不再成立——它测的其实是"有没有自动行"，
        而不是"幂等"。改成按公民数行（每个公民在每块板上恰好 1 行），
        判据更贴近它想证明的性质，也没有放宽。
        """
        other = self._make_ident("board-dev-2")

        board = fish.board_ensure(self.srv.conn, "project", "P1")

        def rows_of(cid):
            """**这块板**上该公民有几行（别的板上的自动行不算）。"""
            return self.srv.conn.execute(
                "SELECT COUNT(*) FROM presence WHERE board_id=? AND citizen_id=?",
                (board["id"], cid)).fetchone()[0]

        self.srv.tool_board({"action": "checkin", "doing": "A 的活",
                             "project": "P1"}, self.valid)
        self.srv.tool_board({"action": "checkin", "doing": "B 的活",
                             "project": "P1"}, other)
        self.assertEqual(rows_of(self.valid.citizen_id), 1, "一个公民在每块板上只能有一行")
        self.assertEqual(rows_of(other.citizen_id), 1, "一个公民在每块板上只能有一行")
        # 重复签到只更新自己那一行
        self.srv.tool_board({"action": "checkin", "doing": "A 改主意了",
                             "project": "P1"}, self.valid)
        self.assertEqual(rows_of(self.valid.citizen_id), 1, "重复签到不该多出一行")
        # ⚠️ 按**这块板**取 doing：自动签到会在根板另建一行，
        # 全表 `{citizen_id: doing}` 的字典会把两行折叠成一行（取到哪行取决于顺序）。
        rows = {r[0]: r[1] for r in self.srv.conn.execute(
            "SELECT citizen_id, doing FROM presence WHERE board_id=?", (board["id"],))}
        self.assertEqual(rows[self.valid.citizen_id], "A 改主意了")
        self.assertEqual(rows[other.citizen_id], "B 的活", "A 的签到覆盖了 B 的行")


if __name__ == "__main__":
    unittest.main(verbosity=2)
