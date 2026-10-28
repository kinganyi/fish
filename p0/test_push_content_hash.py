#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-2 第二段判据（内容感知去重）：指纹的写入与读取。

U-5 结案 + 指纹未变 → 仍然挡（`_recent_pushed_codes` 含该 code）
U-6 `append_push_log(hash_by_code=…)` 把每条的指纹写进去
U-7 取不到指纹（库/关系不存在）时返回 None（退回旧行为，不抛错）
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


class ContentHashTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-hash-"))
        self.conn = fish.connect(self.tmp)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # U-5
    def test_U5_settled_with_same_hash_still_blocked(self):
        fish.append_push_log(self.conn, ["K1"], channel="prompt",
                             hash_by_code={"K1": "h1"}, session_key="s1",
                             pushed_at="2026-10-01T00:00:00Z")
        self.conn.execute("UPDATE push_log SET reaction='accepted' WHERE code='K1'")
        self.conn.commit()
        self.assertIn("K1", fish_push._recent_pushed_codes(self.conn, "s1", hours=24))

    # U-6
    def test_U6_append_writes_per_code_hash(self):
        fish.append_push_log(self.conn, ["K1", "K2"], channel="prompt",
                             hash_by_code={"K1": "h1", "K2": "h2"})
        got = dict(self.conn.execute(
            "SELECT code, content_hash FROM push_log").fetchall())
        self.assertEqual(got, {"K1": "h1", "K2": "h2"})

    # U-7
    def test_U7_unknown_hash_is_none(self):
        self.assertIsNone(fish_push._kb_content_hash(self.conn, "不存在的库", "x.md"))
        self.assertIsNone(fish_push._kb_content_hash(self.conn, None, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
