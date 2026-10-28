#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 阶段判据（Generative Agents 三因子）：recency × importance × relevance。

F-1 importance 高（access_count 多）的候选排在前面（其余因子相同）
F-2 权重可配：把 importance 权重压到 0 → 顺序按 relevance
F-3 默认关（`push.factors_enabled` 未设 → False）
F-4 recency 惩罚：freshness.factor 低的往下走（rule/lesson 恒 1.0 免疫）
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


class FactorScoreTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-factors-"))
        self.conn = fish.connect(self.tmp)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        rows = (("M-A", 40, "artifact"), ("M-B", 1, "artifact"),
                ("M-C", 1, "rule"))
        for code, acc, mtype in rows:
            self.conn.execute(
                "INSERT INTO code_index (code,title,status,file_path,mem_type,"
                "access_count) VALUES (?,?,?,?,?,?)",
                (code, code, "active", f"p/{code}.md", mtype, acc))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cands(self):
        return [{"code": "M-A", "score": 0.5, "relevance": 0.5,
                 "freshness": {"factor": 0.5}},
                {"code": "M-B", "score": 0.5, "relevance": 0.5,
                 "freshness": {"factor": 0.5}},
                {"code": "M-C", "score": 0.5, "relevance": 0.5,
                 "freshness": {"factor": 1.0}}]

    def _order(self, cands):
        out = fish_push._apply_factors(
            self.conn, cands,
            weights={"recency": 0.0, "importance": 1.0, "relevance": 0.0})
        return [c["code"] for c in sorted(out, key=lambda c: -c["score"])]

    # F-1
    def test_F1_importance_ranks_higher(self):
        self.assertEqual(self._order(self._cands())[0], "M-A",
                         "access_count 40 的候选该排第一")

    # F-2
    def test_F2_weights_are_configurable(self):
        cands = self._cands()
        w = {"recency": 0.0, "importance": 0.0, "relevance": 1.0}
        out = fish_push._apply_factors(self.conn, cands, weights=w)
        # relevance 全 0.5 → 三条同分（importance 不再起作用）
        scores = {round(c["score"], 6) for c in out}
        self.assertEqual(len(scores), 1, f"relevance 权重独占时三条应同分：{scores}")

    # F-3
    def test_F3_disabled_by_default(self):
        self.assertFalse(fish_push._factors_enabled(self.conn))

    # F-4
    def test_F4_recency_penalizes_stale(self):
        w = {"recency": 1.0, "importance": 0.0, "relevance": 0.0}
        out = fish_push._apply_factors(self.conn, self._cands(), weights=w)
        order = [c["code"] for c in sorted(out, key=lambda c: -c["score"])]
        self.assertEqual(order[0], "M-C", "freshness=1.0 的该排第一")


if __name__ == "__main__":
    unittest.main(verbosity=2)
