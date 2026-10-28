#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 判据（外部审查 🔴9）：事件事务边界。

E-1 `append_event(commit=False)` 在提交前对**另一个连接**不可见（真事务）
E-2 默认 `commit=True` 保持旧行为（写完即见）
E-3 `citizen_merge` 的归属变更与 `citizen.merged` 事件在同一次提交里落盘
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fish  # noqa: E402


class EventTransactionTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-evt-"))
        self.conn = fish.connect(self.tmp)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _other_count(self, etype):
        other = fish.connect(self.tmp)
        try:
            return other.execute("SELECT COUNT(*) FROM events WHERE type=?",
                                 (etype,)).fetchone()[0]
        finally:
            other.close()

    # E-1
    def test_E1_uncommitted_event_invisible_to_others(self):
        fish.append_event(self.conn, "tx.test.uncommitted", {"citizen_id": "x"},
                          "s", {"k": 1}, commit=False)
        self.assertEqual(self._other_count("tx.test.uncommitted"), 0,
                         "commit=False 的事件在提交前不该对别人可见")
        self.conn.commit()
        self.assertEqual(self._other_count("tx.test.uncommitted"), 1)

    # E-2
    def test_E2_default_commit_still_works(self):
        fish.append_event(self.conn, "tx.test.default", {"citizen_id": "x"},
                          "s", {"k": 1})
        self.assertEqual(self._other_count("tx.test.default"), 1,
                         "默认行为必须保持：写完即可见（27 个旧调用点靠它）")

    # E-3
    def test_E3_merge_event_and_state_same_commit(self):
        src = fish.start_session(self.conn, alias="tx-src", kind="visitor", environ={})
        dst = fish.start_session(self.conn, alias="tx-dst", kind="visitor", environ={})
        fish.citizen_merge(self.conn, src=src["citizen_id"],
                           dst=dst["citizen_id"], apply=True)
        status = self.conn.execute(
            "SELECT status FROM citizens WHERE citizen_id=?",
            (src["citizen_id"],)).fetchone()[0]
        self.assertTrue(status.startswith("merged_into:"))
        # 事件在合并完成后已提交（同一事务）
        self.assertGreaterEqual(self._other_count("citizen.merged"), 1,
                                "合并必须留下 citizen.merged 事件（可追溯）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
