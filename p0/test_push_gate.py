#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""⑤ 推送门槛判据（2026-10-28，BUG-2 收口）：G2 地板 / G3 元记忆降权 / G4 语料默认关。

P-1 地板：score 低于 `push.score_floor` 的候选被丢弃
P-2 元记忆：project 空 + 标题含 fish 的候选分乘 `push.meta_penalty`
P-3 语料：`kind=corpus` 默认不进；`push.corpus_enabled=1` 才进
P-4 不改入参：G3 的降权必须返回副本（原字典的 score 不变）
P-5 空是合法的：地板高到全滤掉时返回 []（不抛）
"""
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fish              # noqa: E402
import fish_common       # noqa: E402
import fish_push         # noqa: E402


def _cand(code, score, *, kind="mem", project="p", title=""):
    return {"code": code, "score": score, "kind": kind,
            "project": project, "title": title}


class PushGateTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-gate-"))
        self.conn = fish.connect(self.tmp / "data")
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _set(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                          (key, str(value)))
        self.conn.commit()

    def _gates(self, cands):
        return fish_push._apply_push_gates(self.conn, cands)

    # P-1
    def test_P1_score_floor_drops_low(self):
        self._set("push.score_floor", "0.25")
        out = self._gates([_cand("a", 0.9), _cand("b", 0.3), _cand("c", 0.1)])
        self.assertEqual([c["code"] for c in out], ["a", "b"])

    # P-2
    def test_P2_meta_penalty_halves(self):
        self._set("push.meta_penalty", "0.5")
        out = self._gates([_cand("m", 0.8, project="", title="fish 看板反馈")])
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0]["score"], 0.4, places=6)
        self.assertEqual(out[0].get("gated"), "meta_penalty")

    # P-2b：有项目归属的**不是**元记忆
    def test_P2b_project_candidate_not_penalized(self):
        self._set("push.meta_penalty", "0.5")
        out = self._gates([_cand("p", 0.8, project="anyi-studio",
                                 title="fish 项目部署（有项目）")])
        self.assertAlmostEqual(out[0]["score"], 0.8, places=6)

    # P-3
    def test_P3_corpus_off_by_default(self):
        out = self._gates([_cand("c", 0.9, kind="corpus")])
        self.assertEqual(out, [], "语料默认不进（N7：记忆与语料可信度不同级）")
        self._set("push.corpus_enabled", "1")
        out = self._gates([_cand("c", 0.9, kind="corpus")])
        self.assertEqual(len(out), 1)

    # P-4
    def test_P4_does_not_mutate_input(self):
        self._set("push.meta_penalty", "0.5")
        cand = _cand("m", 0.8, project="", title="fish 注入包")
        self._gates([cand])
        self.assertEqual(cand["score"], 0.8, "G3 必须返回副本，不许改入参")

    # P-5
    def test_P5_empty_result_is_legal(self):
        self._set("push.score_floor", "0.99")
        self.assertEqual(self._gates([_cand("a", 0.5)]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
