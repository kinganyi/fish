#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F-P2-6：token 会一直长，而没有命令能让它变小。

外部审查（2026-11-18）实测：377 token / 227 活；**默认永不过期**；无清理。
本项目 2026-11-19 复核：379 / 229 未撤销 / **349 永不过期** / 30 已过期未清理。

这不是"能不能"的问题，是**每天都在变大**的问题。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fish  # noqa: E402
import fish_token  # noqa: E402


class TokenPruneTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="fish-p26-")
        self.root = Path(self._tmp) / "data"
        self.root.mkdir(parents=True)
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        rows = [
            ("h-expired", "2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z"),   # 过期
            ("h-never", "2026-11-19T00:00:00Z", None),                        # 永不过期
            ("h-live", "2026-11-19T00:00:00Z", "2030-01-01T00:00:00Z"),       # 有效
            ("h-stale", "2020-01-01T00:00:00Z", "2030-01-01T00:00:00Z"),      # 陈旧但有效
        ]
        for h, issued, exp in rows:
            self.conn.execute(
                "INSERT INTO session_tokens (token_hash,citizen_id,session_key,"
                "evidence,channel,parent_token,issued_at,expires_at,revoked_at) "
                "VALUES (?,?,?,?,?,?,?,?,NULL)",
                (h, "C1", "k-" + h, "machine", "cli", None, issued, exp))
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:      # noqa: BLE001
            pass

    def test_stats_count_the_four_kinds(self):
        st = fish_token.token_stats(self.conn)
        self.assertEqual(st["total"], 4)
        self.assertEqual(st["expired"], 1, f"过期数不对：{st}")
        self.assertEqual(st["never_expires"], 1, f"永不过期数不对：{st}")
        self.assertEqual(st["stale"], 1, f"陈旧数不对：{st}")

    def test_prune_is_dry_run_by_default(self):
        """**缺省必须 dry-run**——删 token 是不可逆的。"""
        before = fish_token.token_stats(self.conn)["total"]
        res = fish_token.token_prune(self.conn)
        self.assertTrue(res["dry_run"], "缺省不是 dry-run")
        self.assertEqual(fish_token.token_stats(self.conn)["total"], before,
                         "dry-run 删了东西")

    def test_prune_deletes_expired_only(self):
        """只删**过期**；永不过期与有效的必须留着。"""
        fish_token.token_prune(self.conn, dry_run=False)
        left = {r[0] for r in self.conn.execute(
            "SELECT token_hash FROM session_tokens").fetchall()}
        self.assertNotIn("h-expired", left, "过期 token 没被删")
        self.assertIn("h-never", left, "永不过期的被误删——它没有过期时间")
        self.assertIn("h-live", left, "有效的被误删")

    def test_stale_is_revoked_not_deleted(self):
        """陈旧只**撤销**，不删——删了就看不出曾经有过。"""
        fish_token.token_prune(self.conn, stale=True, dry_run=False)
        row = self.conn.execute(
            "SELECT revoked_at FROM session_tokens WHERE token_hash='h-stale'"
        ).fetchone()
        self.assertIsNotNone(row, "陈旧 token 被删了——应当只撤销")
        self.assertIsNotNone(row[0], "陈旧 token 没被撤销")


if __name__ == "__main__":
    unittest.main(verbosity=2)
