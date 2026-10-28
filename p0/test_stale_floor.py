#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-1 判据（行为谱系 G3）：分通道时效门。

S-1 陈旧的 `artifact`（强时效类型）在 tool_after/inject 通道被挡
S-2 同样陈旧的 `rule` / `lesson`（豁免类型）**保留**——"一条铁律不会因为写得久就变假"
S-3 没有 `mem_type` 的候选（kb / 看板）**不误杀**（本判据只管记忆类型）
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


class StaleFloorTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-stale-"))
        self.conn = fish.connect(self.tmp)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        for code, mtype in (("M-260101-01", "artifact"),
                            ("M-260101-02", "rule"),
                            ("M-260101-03", "lesson")):
            self.conn.execute(
                "INSERT INTO code_index (code,title,status,file_path,mem_type) "
                "VALUES (?,?,?,?,?)",
                (code, f"{mtype} 标题", "active", f"projects/p/{code}.md", mtype))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cand(self, code, factor):
        return {"code": code, "kind": "memory", "score": 0.8,
                "freshness": {"factor": factor}}

    # S-1
    def test_S1_stale_artifact_dropped(self):
        cands = [self._cand("M-260101-01", 0.1)]       # 很旧
        kept = fish_push._apply_stale_floor(self.conn, cands, 0.35)
        self.assertEqual(kept, [], "陈旧 artifact 必须被挡（旧实现只靠打分折扣，压不到 0）")

    # S-2
    def test_S2_stale_rule_and_lesson_kept(self):
        cands = [self._cand("M-260101-02", 0.1), self._cand("M-260101-03", 0.1)]
        kept = fish_push._apply_stale_floor(self.conn, cands, 0.35)
        self.assertEqual(len(kept), 2, "rule/lesson 不受年龄惩罚")

    # S-3
    def test_S3_candidates_without_mtype_pass(self):
        cands = [{"code": "kb:库:笔记.md", "kind": "kb", "score": 0.5},
                 {"code": "board:项目", "kind": "board", "score": 0.6}]
        kept = fish_push._apply_stale_floor(self.conn, cands, 0.35)
        self.assertEqual(len(kept), 2, "kb/看板候选不该被记忆时效门误杀")


if __name__ == "__main__":
    unittest.main(verbosity=2)
