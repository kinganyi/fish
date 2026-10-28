#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-2 判据（行为谱系 G1）：**结案即不再推**。

U-1 `accepted` 的 code 在 24h **之后**也不再进候选（读过了就够）
U-2 `ignored` 的 code 同样
U-3 仍在窗口内的普通推送（reaction IS NULL）保持旧的 24h 行为
U-4 有 session_key 时行为一致（按会话 + 全局结案）
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


class PushSettledTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-settled-"))
        self.conn = fish.connect(self.tmp)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _push(self, code, reaction=None, hours_ago=1, session_key="s1"):
        label = "" if reaction is None else reaction
        self.conn.execute(
            "INSERT INTO push_log (session_key, code, channel, pushed_at, reaction) "
            "VALUES (?,?,?, strftime('%Y-%m-%dT%H:%M:%SZ','now', ?), ?)",
            (session_key, code, "tool_after", f"-{int(hours_ago)} hours", label))
        self.conn.commit()

    # U-1
    def test_U1_accepted_never_pushed_again(self):
        self._push("MEM-A", "accepted", hours_ago=24 * 30)   # 一个月前读过
        self.assertIn("MEM-A", fish_push._recent_pushed_codes(self.conn, "s1", hours=24),
                      "读过的 code 必须仍然被挡（旧实现 24h 后就放行）")

    # U-2
    def test_U2_ignored_never_pushed_again(self):
        self._push("MEM-B", "ignored", hours_ago=24 * 10)
        self.assertIn("MEM-B", fish_push._recent_pushed_codes(self.conn, "s1", hours=24))

    # U-3
    def test_U3_unreacted_keeps_24h_window(self):
        self._push("MEM-C", None, hours_ago=2)      # 窗口内
        self._push("MEM-D", None, hours_ago=48)     # 窗口外
        got = fish_push._recent_pushed_codes(self.conn, "s1", hours=24)
        self.assertIn("MEM-C", got)
        self.assertNotIn("MEM-D", got, "未反应的旧推送不该被永久挡（还没结案）")

    # U-4
    def test_U4_global_settled_across_sessions(self):
        self._push("MEM-E", "accepted", hours_ago=24 * 5, session_key="other-sess")
        # 空 session（tool_after 通道）必须看到别的会话的 accep 记录
        self.assertIn("MEM-E", fish_push._recent_pushed_codes(self.conn, "", hours=24))


if __name__ == "__main__":
    unittest.main(verbosity=2)
