#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-a 推送度量的判据（2026-10-25 · E 批）。

守的是 `16` §5.5 那条纪律：**四个数字永不合并**。
论文原话是 "evaluation is the gating layer"，而"不能只报一个综合分"的理由更直接——
**只报综合分，"不说话"看起来最安全**（沉默永远没有误报）。

  M1  采纳率只除以"已判"，待判不算（否则窗口没到就拉低）
  M2  空数据各率是 None 不是 0（"没推过" ≠ "推了没人理"）
  M3  违规率单独一栏，且只统计 tool_after
  M4  窗口过滤生效
  M5  分会话方差：样本 <2 时是 None（算不出 ≠ 0）
  M6  **不给总分**：返回结构里不存在任何"综合分"字段
"""
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)


class PushStatsTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.data = Path(self.root) / "data"
        self.conn = fish.connect(self.data)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.cid = "01CITIZEN0000000000000009"

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _push(self, code, *, reaction=None, channel="inject", session="s1",
              latency=None, pushed_at=None, citizen=None):
        self.conn.execute(
            "INSERT INTO push_log (session_key,citizen_id,project,code,channel,"
            "budget_tokens,pushed_at,reaction,latency_sec) VALUES (?,?,?,?,?,?,?,?,?)",
            (session, citizen or self.cid, "TESTA", code, channel, 1000,
             pushed_at or fish.now_utc(), reaction, latency))
        self.conn.commit()

    def _access(self, code, *, via="read_body", at=None, session="s1"):
        self.conn.execute(
            "INSERT INTO memory_access_log (code,citizen_id,session_key,project,via,accessed_at) "
            "VALUES (?,?,?,?,?,?)",
            (code, self.cid, session, "TESTA", via, at or fish.now_utc()))
        self.conn.commit()

    # ---------------------------------------------------------------- M1
    def test_M1_accept_rate_excludes_pending(self):
        """采纳率只除以"已判"，待判不算。

        变异：把分母改成 total（含待判）→ 50% 会变成 25% → 红。
        """
        self._push("TESTA-261025-01", reaction="accepted", latency=10)
        self._push("TESTA-261025-02", reaction="accepted", latency=20)
        self._push("TESTA-261025-03", reaction="ignored")
        self._push("TESTA-261025-04", reaction="ignored")
        self._push("TESTA-261025-05", reaction=None)          # 待判
        st = fish.push_stats(self.conn, days=30)
        self.assertEqual(st["decided"], 4, f"已判应为 4，实际 {st['decided']}")
        self.assertEqual(st["pending"], 1, f"待判应为 1，实际 {st['pending']}")
        self.assertAlmostEqual(st["accept_rate"], 0.5, places=4,
                               msg=f"采纳率应为 2/4=0.5，实际 {st['accept_rate']}")

    # ---------------------------------------------------------------- M2
    def test_M2_empty_is_none_not_zero(self):
        """没推过时各率必须是 None——0 的含义是"推了没人理"，两件事不能混。"""
        st = fish.push_stats(self.conn, days=30)
        self.assertEqual(st["total"], 0)
        self.assertIsNone(st["accept_rate"], "没推过时采纳率必须是 None，不能是 0")
        self.assertIsNone(st["gap_clear_rate"], "没推过时盲区消除率必须是 None")
        self.assertIsNone(st["violation_rate"], "没推过工具后推送时违规率必须是 None")
        self.assertIsNone(st["session_stdev"], "单会话/无会话时方差不该是 0")

    # ---------------------------------------------------------------- M3
    def test_M3_violation_only_counts_tool_after(self):
        """违规率只统计 tool_after，且"5 分钟内无任何访问"才算违规。"""
        # 两个 tool_after：一个后有访问（不算违规），一个没有（算）
        self._push("TESTA-261025-01", channel="tool_after", session="s1")
        self._access("TESTA-261025-01", session="s1")
        self._push("TESTA-261025-02", channel="tool_after", session="s2")
        # 一堆 inject 不该进违规分母
        self._push("TESTA-261025-03", channel="inject", session="s3")
        self._push("TESTA-261025-04", channel="inject", session="s3")
        st = fish.push_stats(self.conn, days=30)
        self.assertEqual(st["tool_after_total"], 2,
                         f"tool_after 应为 2（不含 inject），实际 {st['tool_after_total']}")
        self.assertAlmostEqual(st["violation_rate"], 0.5, places=4,
                               msg=f"2 次里 1 次无后续访问 → 0.5，实际 {st['violation_rate']}")

    # ---------------------------------------------------------------- M4
    def test_M4_window_filters_old_rows(self):
        """超出窗口的推送不计入。变异：去掉 cutoff → 老数据混进来 → 红。"""
        old = "2026-01-01T00:00:00Z"
        self._push("TESTA-261025-01", reaction="accepted", pushed_at=old)
        self._push("TESTA-261025-02", reaction="ignored")
        st = fish.push_stats(self.conn, days=30)
        self.assertEqual(st["total"], 1, f"窗口内只该有 1 条，实际 {st['total']}")

    # ---------------------------------------------------------------- M5
    def test_M5_variance_needs_two_sessions(self):
        """方差：只有 1 个已判会话时算不出（None），不算成 0。"""
        self._push("TESTA-261025-01", reaction="accepted", session="s1")
        st1 = fish.push_stats(self.conn, days=30)
        self.assertIsNone(st1["session_stdev"], "单会话时方差应为 None")
        self._push("TESTA-261025-02", reaction="ignored", session="s2")
        st2 = fish.push_stats(self.conn, days=30)
        self.assertIsNotNone(st2["session_stdev"], "两会话时应能算出方差")
        self.assertAlmostEqual(st2["session_stdev"], 0.5, places=4,
                               msg=f"s1=100% / s2=0% → 总体标准差应 0.5，实际 {st2['session_stdev']}")

    # ---------------------------------------------------------------- M6
    def test_M6_no_composite_score(self):
        """**不许有总分**：返回结构里不能出现任何"综合分"字段。

        这条是 `16` §5.5 的纪律本身：一旦有总分，调用方就会只看总分，
        而总分会让"少说话"永远看起来更好（沉默没有误报成本）。
        """
        self._push("TESTA-261025-01", reaction="accepted")
        st = fish.push_stats(self.conn, days=30)
        banned = {"score", "total_score", "composite", "overall", "rating", "grade"}
        hit = banned & set(st.keys())
        self.assertFalse(hit, f"度量里不该有综合分字段，发现 {hit}")
        # 四个指标必须各自在场（漏一个就等于把它们合并了）
        for k in ("accept_rate", "gap_clear_rate", "violation_rate", "session_stdev"):
            self.assertIn(k, st, f"缺少指标 {k}——四个数字必须分开在场")

    # ---------------------------------------------------------------- 端到端
    def test_M7_cli_renders_four_sections(self):
        """CLI 渲染必须分四栏，且明写"不报总分"的提醒。"""
        self._push("TESTA-261025-01", reaction="accepted", latency=5)
        text = fish.format_push_stats(fish.push_stats(self.conn, days=30))
        self.assertIn("收益侧", text, f"应有收益侧栏。实际：\n{text}")
        self.assertIn("成本侧", text, "成本侧必须单独一栏（不能与收益混）")
        self.assertIn("每会话方差", text, "方差必须报（聚合值单独看会骗人）")
        self.assertIn("不报总分", text, "必须明写为什么没有总分")
        # 不能简单查 "总分：" ——文案里的「不报总分：」也包含它（本判据第一版就红在这里）。
        # 真正要禁止的是**数值化的总分**：`总分: 0.87` 这种形态。
        import re as _re
        self.assertIsNone(_re.search(r"总分\s*[:=]\s*[\d.]+", text),
                          f"不该真的给出一个数值总分。实际：\n{text}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
