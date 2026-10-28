#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-1 判据（推送研究 2026-10-28）：空 session_key 也要去重。

D-1 空 session_key：最近 24h 推过的 code **全部**被挡（含别的 session 推的）
D-2 有 session_key：仍只按本会话过滤（既有行为不变）
D-3 超过窗口的推送不算（24h 前的不挡）
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fish          # noqa: E402
import fish_push     # noqa: E402


class PushDedupeTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-dedupe-"))
        self.conn = fish.connect(self.tmp / "data")
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _push(self, code, session_key, minutes_ago=5):
        self.conn.execute(
            "INSERT INTO push_log (session_key, code, channel, pushed_at) "
            "VALUES (?,?,?, strftime('%Y-%m-%dT%H:%M:%SZ','now', ?))",
            (session_key, code, "tool_after", f"-{minutes_ago} minutes"))
        self.conn.commit()

    # D-1
    def test_D1_empty_session_key_still_dedupes(self):
        self._push("A", "")            # 本会话（空）
        self._push("B", "other-sess")  # 别人的会话
        got = fish_push._recent_pushed_codes(self.conn, "", hours=24)
        self.assertIn("A", got, "空 session 自己的推送要去重")
        self.assertIn("B", got, "缺 session_key 时要按全局去重（否则 tool_after 通道失效）")

    # D-2
    def test_D2_session_scoped_unchanged(self):
        self._push("A", "")
        self._push("B", "s1")
        got = fish_push._recent_pushed_codes(self.conn, "s1", hours=24)
        self.assertIn("B", got)
        self.assertNotIn("A", got, "有 session_key 时仍只按本会话过滤")

    # D-3
    def test_D3_old_pushes_excluded(self):
        self._push("OLD", "", minutes_ago=60 * 30)   # 30 小时前
        got = fish_push._recent_pushed_codes(self.conn, "", hours=24)
        self.assertNotIn("OLD", got)


if __name__ == "__main__":
    unittest.main(verbosity=2)
