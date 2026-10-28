#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-2 判据（行为谱系 / 审查 🟠-P、Q）：RRF 融合。

R-1 三路等分 → **每路第一名得分相同**（只看名次，不看量纲）
R-2 **量纲悬殊**（memory 0.9 / kb 0.1）→ 仍同分（这正是"分数直排"做不到的）
R-3 默认关闭（`push.rrf_enabled` 未设 → False，行为零变化）
R-4 单路时**顺序不变**（融合不该把唯一一路打乱）
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


class RrfFuseTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-rrf-"))
        self.conn = fish.connect(self.tmp)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cands(self):
        return [
            {"code": "M-1", "kind": "prompt_related", "score": 0.9},
            {"code": "M-2", "kind": "prompt_related", "score": 0.6},
            {"code": "kb:库:a.md", "kind": "kb", "score": 0.1},
            {"code": "kb:库:b.md", "kind": "kb", "score": 0.08},
            {"code": "conv:x#1", "kind": "conv_related", "score": 0.5},
        ]

    # R-1 / R-2
    def test_R1_R2_first_of_each_route_gets_same_score(self):
        fused = {c["code"]: c for c in fish_push._rrf_fuse(self._cands())}
        # 三路第一名：M-1 / kb:a.md / conv:x#1
        top = {fused["M-1"]["score"], fused["kb:库:a.md"]["score"],
               fused["conv:x#1"]["score"]}
        self.assertEqual(len(top), 1,
                         f"三路第一名必须同分（RRF 只看名次）：{fused}")
        self.assertAlmostEqual(max(top), 1.0, places=6)

    # R-3
    def test_R3_disabled_by_default(self):
        self.assertFalse(fish_push._rrf_enabled(self.conn),
                         "默认必须关（否则既有行为会被悄悄改掉）")

    # R-4
    def test_R4_single_route_keeps_order(self):
        only = [{"code": "M-1", "kind": "prompt_related", "score": 0.9},
                {"code": "M-2", "kind": "prompt_related", "score": 0.6},
                {"code": "M-3", "kind": "prompt_related", "score": 0.3}]
        fused = fish_push._rrf_fuse(only)
        order = [c["code"] for c in sorted(fused, key=lambda c: -c["score"])]
        self.assertEqual(order, ["M-1", "M-2", "M-3"], "单路融合不得打乱原顺序")


if __name__ == "__main__":
    unittest.main(verbosity=2)
