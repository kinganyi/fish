#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-2 判据：推送结果闭环（sweep 判 ignored + 前缀回填 accepted）。

## 为什么这组判据存在

现场读数：`push_log` 1,027 行里 920 条 `reaction` 为空——回填函数都写好了
（`mark_push_accepted` / `close_stale_pushes`），但**没人驱动**，而且看板/知识库
候选（`board:` / `kb:`）没有对应的记忆编码，`mark_push_accepted` 永远匹配不上。
本组判据钉住两件事：窗口外必须被 sweep 判掉；看了看板/知识库必须按前缀回填。

## 判据

  S-PO1 `close_stale_pushes` 只判"窗口外且待判"的行（窗口内不动、已判不动）
  S-PO2 `mark_push_accepted_prefix` 只回填"同会话 + 同前缀 + 窗口内 + 待判"
  S-PO3 `kb:` 前缀不会误伤 `board:` 的推送（反之亦然）
"""
import datetime
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish  # noqa: E402

CID = "01PUSHOUTCOME00000000AA1"
SK_A = "dsh:session-outcome-aaaa-0001"
SK_B = "dsh:session-outcome-bbbb-0002"


def _ago(hours):
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


class PushOutcomeBase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-push-outcome-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,status,created_at,host) "
            "VALUES (?,?,'visitor','active',?,'dsh')", (CID, "AI-甲", fish.now_utc()))
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _push(self, code, *, session_key=SK_A, hours_ago=1, channel="tool_after",
              reaction=None):
        self.conn.execute(
            "INSERT INTO push_log (session_key,citizen_id,project,code,candidate_id,"
            "channel,budget_tokens,pushed_at,reaction) VALUES (?,?,?,?,?,?,?,?,?)",
            (session_key, CID, None, code, None, channel, 100, _ago(hours_ago), reaction))
        self.conn.commit()

    def _reaction(self, code, session_key=SK_A):
        row = self.conn.execute(
            "SELECT reaction FROM push_log WHERE code=? AND session_key IS ? "
            "ORDER BY id DESC LIMIT 1", (code, session_key)).fetchone()
        return row[0] if row else None


class SweepTest(PushOutcomeBase):
    """S-PO1：只判窗口外且待判的行。"""

    def test_SPO1_sweep_marks_only_stale_pending(self):
        self._push("OLD-1", hours_ago=30)                 # 窗口外、待判 → ignored
        self._push("FRESH-1", hours_ago=1)                # 窗口内、待判 → 不动
        self._push("OLD-ACCEPTED", hours_ago=30, reaction="accepted")  # 已判 → 不动
        ignored = fish.close_stale_pushes(self.conn, window_hours=24)
        self.assertEqual(ignored, 1, "只该判 1 条（窗口外且待判）")
        self.assertEqual(self._reaction("OLD-1"), "ignored")
        self.assertIsNone(self._reaction("FRESH-1"), "窗口内不许判")
        self.assertEqual(self._reaction("OLD-ACCEPTED"), "accepted", "已判不许覆盖")

    def test_SPO1b_sweep_idempotent(self):
        self._push("OLD-2", hours_ago=30)
        self.assertEqual(fish.close_stale_pushes(self.conn, window_hours=24), 1)
        self.assertEqual(fish.close_stale_pushes(self.conn, window_hours=24), 0,
                         "第二次没有可判的行（幂等）")


class PrefixAcceptedTest(PushOutcomeBase):
    """S-PO2：前缀回填只认同会话/同前缀/窗口内/待判。"""

    def test_SPO2_prefix_accepts_same_session_in_window(self):
        self._push("board:大肥鱼生命系统", session_key=SK_A, hours_ago=1)
        self._push("board:大肥鱼生命系统", session_key=SK_B, hours_ago=1)
        self._push("board:old", session_key=SK_A, hours_ago=30)
        n = fish.mark_push_accepted_prefix(
            self.conn, "board:%", session_key=SK_A, window_hours=24)
        self.assertEqual(n, 1, "只该回填 A 会话、窗口内的那 1 条")
        self.assertEqual(self._reaction("board:大肥鱼生命系统", SK_A), "accepted")
        self.assertIsNone(self._reaction("board:大肥鱼生命系统", SK_B),
                          "别的会话的推送不许被回填")
        self.assertIsNone(self._reaction("board:old", SK_A), "窗口外的不许回填")

    def test_SPO3_prefix_does_not_cross_kind(self):
        self._push("kb:库:设计.md", session_key=SK_A, hours_ago=1)
        self._push("board:板上", session_key=SK_A, hours_ago=1)
        fish.mark_push_accepted_prefix(self.conn, "kb:%", session_key=SK_A,
                                       window_hours=24)
        self.assertEqual(self._reaction("kb:库:设计.md", SK_A), "accepted")
        self.assertIsNone(self._reaction("board:板上", SK_A),
                          "kb 前缀不许误伤 board 的推送")


class McpBoardBackfillTest(PushOutcomeBase):
    """S-PO4：MCP `board show` 真的触发前缀回填（挂点集成判据）。"""

    def test_SPO4_board_show_backfills_accepted(self):
        import types

        import mcp_server

        self._push("board:大肥鱼生命系统", session_key=SK_A, hours_ago=1)
        srv = mcp_server.FishServer(self.root)
        try:
            ident = types.SimpleNamespace(status="valid", citizen_id=CID,
                                          session_key=SK_A)
            out = srv.tool_board({"action": "show", "project": "PJ"}, ident)
            self.assertIn("看板", out)
        finally:
            srv.close() if hasattr(srv, "close") else None
        self.assertEqual(self._reaction("board:大肥鱼生命系统", SK_A), "accepted",
                         "board show 应把同会话窗口内的 board: 推送回填为 accepted")


if __name__ == "__main__":
    unittest.main()
